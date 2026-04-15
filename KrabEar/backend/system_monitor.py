"""Монитор системных ресурсов для Krab Ear.

Предоставляет сведения о CPU, памяти, диске, GPU и версии macOS
без внешних зависимостей (только stdlib).
"""

from __future__ import annotations

import os
import platform
import resource
import subprocess
import time
import logging

logger = logging.getLogger("KrabEar.Backend.SystemMonitor")


class SystemMonitor:
    """Сборщик информации о системных ресурсах."""

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _sysctl(key: str) -> str:
        """Возвращает значение sysctl-ключа (macOS)."""
        try:
            result = subprocess.run(
                ["sysctl", "-n", key],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip()
        except Exception:
            return ""

    @staticmethod
    def _vm_stat() -> dict[str, int]:
        """Разбирает вывод `vm_stat` и возвращает словарь счётчиков (в страницах)."""
        data: dict[str, int] = {}
        try:
            result = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    val = val.strip().rstrip(".")
                    try:
                        data[key.strip()] = int(val)
                    except ValueError:
                        pass
        except Exception:
            pass
        return data

    @staticmethod
    def _page_size() -> int:
        """Размер страницы памяти в байтах."""
        try:
            out = subprocess.run(
                ["pagesize"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            return int(out)
        except Exception:
            return 4096

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get_system_info(self) -> dict:
        """Возвращает словарь с ресурсными показателями системы.

        Ключи:
            cpu_percent         — использование CPU (общее, %)
            memory_used_gb      — использованная RAM (ГБ)
            memory_total_gb     — суммарная RAM (ГБ)
            memory_percent      — использование RAM (%)
            disk_free_gb        — свободное место на / (ГБ)
            disk_total_gb       — объём диска / (ГБ)
            python_memory_mb    — RSS текущего процесса (МБ)
            process_cpu_percent — CPU текущего процесса (приблизительно)
            gpu_name            — название GPU / чипа Apple Silicon
            macos_version       — версия macOS (строка)
            uptime_hours        — время работы системы (часы)
            load_average        — средняя нагрузка [1m, 5m, 15m]
        """
        info: dict = {}

        # ---- CPU load average ----------------------------------------
        try:
            la = os.getloadavg()
            info["load_average"] = [round(x, 2) for x in la]
        except OSError:
            info["load_average"] = [0.0, 0.0, 0.0]

        # ---- CPU percent (грубая оценка через sysctl cpu stats) -------
        try:
            cpu_count = os.cpu_count() or 1
            # Используем load_average[0] / cpu_count как прокси, ограничено 100%
            raw = (info["load_average"][0] / cpu_count) * 100
            info["cpu_percent"] = round(min(raw, 100.0), 1)
        except Exception:
            info["cpu_percent"] = 0.0

        # ---- Memory (через vm_stat) -----------------------------------
        page_size = self._page_size()
        vm = self._vm_stat()
        try:
            total_raw = self._sysctl("hw.memsize")
            memory_total_bytes = int(total_raw) if total_raw else 0
        except (ValueError, TypeError):
            memory_total_bytes = 0

        try:
            free_pages = vm.get("Pages free", 0) + vm.get("Pages inactive", 0)
            free_bytes = free_pages * page_size
            used_bytes = memory_total_bytes - free_bytes
            info["memory_total_gb"] = round(memory_total_bytes / (1024 ** 3), 1)
            info["memory_used_gb"] = round(max(used_bytes, 0) / (1024 ** 3), 1)
            if memory_total_bytes > 0:
                info["memory_percent"] = round(
                    max(used_bytes, 0) / memory_total_bytes * 100, 1
                )
            else:
                info["memory_percent"] = 0.0
        except Exception:
            info["memory_total_gb"] = 0.0
            info["memory_used_gb"] = 0.0
            info["memory_percent"] = 0.0

        # ---- Disk (корневой раздел) -----------------------------------
        try:
            st = os.statvfs("/")
            disk_total = st.f_blocks * st.f_frsize
            disk_free = st.f_bavail * st.f_frsize
            info["disk_free_gb"] = round(disk_free / (1024 ** 3), 1)
            info["disk_total_gb"] = round(disk_total / (1024 ** 3), 1)
        except Exception:
            info["disk_free_gb"] = 0.0
            info["disk_total_gb"] = 0.0

        # ---- Текущий процесс (RSS) -----------------------------------
        try:
            rusage = resource.getrusage(resource.RUSAGE_SELF)
            # На macOS maxrss — в байтах
            rss_bytes = rusage.ru_maxrss
            info["python_memory_mb"] = round(rss_bytes / (1024 ** 2), 1)
        except Exception:
            info["python_memory_mb"] = 0.0

        # ---- Process CPU (utime + stime через resource) --------------
        try:
            utime = rusage.ru_utime  # type: ignore[union-attr]
            stime = rusage.ru_stime  # type: ignore[union-attr]
            total_cpu_s = utime + stime
            uptime_s = self._get_uptime_seconds()
            if uptime_s > 0:
                info["process_cpu_percent"] = round(
                    min(total_cpu_s / uptime_s * 100, 100.0), 1
                )
            else:
                info["process_cpu_percent"] = 0.0
        except Exception:
            info["process_cpu_percent"] = 0.0

        # ---- GPU / Apple Silicon chip --------------------------------
        try:
            gpu_name = self._sysctl("machdep.cpu.brand_string")
            if not gpu_name:
                # Для Apple Silicon используем hw.chip_model (Ventura+)
                gpu_name = self._sysctl("hw.chip_model") or platform.processor() or "Unknown"
            info["gpu_name"] = gpu_name
        except Exception:
            info["gpu_name"] = "Unknown"

        # ---- macOS version -------------------------------------------
        try:
            info["macos_version"] = platform.mac_ver()[0] or platform.release()
        except Exception:
            info["macos_version"] = "Unknown"

        # ---- System uptime -------------------------------------------
        try:
            uptime_s = self._get_uptime_seconds()
            info["uptime_hours"] = round(uptime_s / 3600, 1)
        except Exception:
            info["uptime_hours"] = 0.0

        return info

    def _get_uptime_seconds(self) -> float:
        """Возвращает время работы системы в секундах."""
        try:
            raw = self._sysctl("kern.boottime")
            # Формат: { sec = 1234567890, usec = 123456 }
            if "sec = " in raw:
                sec_part = raw.split("sec = ")[1].split(",")[0].strip()
                boot_ts = float(sec_part)
                return time.time() - boot_ts
        except Exception:
            pass
        return 0.0

    def is_resource_constrained(self) -> bool:
        """Возвращает True, если память > 80% или свободного диска < 1 ГБ."""
        try:
            info = self.get_system_info()
            return (
                info.get("memory_percent", 0.0) > 80.0
                or info.get("disk_free_gb", 999.0) < 1.0
            )
        except Exception:
            return False
