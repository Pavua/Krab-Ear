"""Главное окно Krab Ear: запись, транскрибация, автовставка и история.

Файл связан с:
1) `core/engine.py` — распознавание и озвучка;
2) `core/storage.py` — хранение настроек и последних транскрибаций.
"""

from __future__ import annotations

from datetime import datetime
import logging
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import pyperclip
from pynput import keyboard

from core.engine import AudioEngine
from core.storage import AppState, AppStorage

try:
    from PIL import Image, ImageDraw
    import pystray
    from pystray import MenuItem as TrayItem

    TRAY_SUPPORTED = True
except Exception:
    TRAY_SUPPORTED = False


class App:
    """Основной контроллер интерфейса и пользовательских сценариев."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Krab Ear Standalone")
        self.root.geometry("560x760")
        self.root.minsize(470, 620)

        self.logger = logging.getLogger("KrabEar.UI")
        self.engine = AudioEngine()
        self.storage = AppStorage(max_history=5)
        self.state: AppState = self.storage.load()

        self.is_on_top = tk.BooleanVar(
            value=bool(self.state.settings.get("always_on_top", False))
        )
        self.auto_paste = tk.BooleanVar(
            value=bool(self.state.settings.get("auto_paste", True))
        )
        self.toggle_mode = tk.BooleanVar(
            value=bool(self.state.settings.get("toggle_mode", True))
        )
        self.play_start_sound = tk.BooleanVar(
            value=bool(self.state.settings.get("play_start_sound", True))
        )

        self.is_recording = False
        self.record_start_ts = 0.0
        self.last_transcribed_text = ""
        self.latest_level = 0.0
        self.record_thread: threading.Thread | None = None
        self.hotkey_pressed = False
        self.is_shutting_down = False
        self.tray_icon = None

        self._build_ui()
        self._render_history()
        self._apply_topmost()
        self._start_hotkey_thread()
        self._start_tray()

        # Если трей недоступен, закрытие окна должно завершать процесс, иначе окно пропадёт без возврата.
        if self.tray_icon is None:
            self.root.protocol("WM_DELETE_WINDOW", self.exit_app)
        else:
            self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

    def _build_ui(self) -> None:
        """Собирает виджеты и привязывает действия."""
        header = ttk.Frame(self.root, padding=12)
        header.pack(fill="x")

        ttk.Checkbutton(
            header,
            text="Поверх окон",
            variable=self.is_on_top,
            command=self._on_settings_changed,
        ).pack(side="left")
        ttk.Checkbutton(
            header,
            text="Автовставка",
            variable=self.auto_paste,
            command=self._on_settings_changed,
        ).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(
            header,
            text="Режим переключателя",
            variable=self.toggle_mode,
            command=self._on_settings_changed,
        ).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(
            header,
            text="Звук старта",
            variable=self.play_start_sound,
            command=self._on_settings_changed,
        ).pack(side="left", padx=(12, 0))

        info_frame = ttk.LabelFrame(self.root, text="Управление", padding=12)
        info_frame.pack(fill="x", padx=12, pady=(0, 8))

        ttk.Label(
            info_frame,
            text="Горячая клавиша: Right Option (Alt Right)\n"
            "Режим переключателя: нажал старт, нажал стоп\n"
            "Режим удержания: запись только пока держите клавишу",
            justify="left",
        ).pack(anchor="w")

        self.status_label = ttk.Label(
            info_frame,
            text="Готов к записи",
            font=("Helvetica", 13, "bold"),
        )
        self.status_label.pack(anchor="w", pady=(10, 0))

        self.live_label = ttk.Label(info_frame, text="Таймер: 00:00  Уровень: [..........]")
        self.live_label.pack(anchor="w", pady=(4, 0))

        self.record_button = ttk.Button(
            info_frame,
            text="Начать запись",
            command=self.toggle_recording,
        )
        self.record_button.pack(fill="x", pady=(12, 0))

        action_frame = ttk.Frame(info_frame)
        action_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(action_frame, text="Копировать последнее", command=self.copy_last).pack(
            side="left"
        )
        ttk.Button(action_frame, text="Очистить историю", command=self.clear_history).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(action_frame, text="Скрыть в фон", command=self.hide_window).pack(
            side="right"
        )

        history_frame = ttk.LabelFrame(
            self.root,
            text="Последние 5 транскрибаций",
            padding=12,
        )
        history_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.history_box = scrolledtext.ScrolledText(
            history_frame,
            wrap="word",
            font=("Menlo", 12),
            state="disabled",
        )
        self.history_box.pack(fill="both", expand=True)

        footer = ttk.Frame(self.root, padding=12)
        footer.pack(fill="x")
        ttk.Button(footer, text="Показать окно", command=self.show_window).pack(side="left")
        ttk.Button(footer, text="Выход", command=self.exit_app).pack(side="right")

    def _start_hotkey_thread(self) -> None:
        """Запускает слушатель глобальной горячей клавиши в фоне."""
        thread = threading.Thread(target=self._hotkey_listener, daemon=True)
        thread.start()

    def _hotkey_listener(self) -> None:
        """Обрабатывает Right Option для старт/стоп записи."""

        def on_press(key: keyboard.Key | keyboard.KeyCode) -> None:
            if key != keyboard.Key.alt_r:
                return

            if self.hotkey_pressed:
                return
            self.hotkey_pressed = True

            if self.toggle_mode.get():
                self.root.after(0, self.toggle_recording)
            else:
                self.root.after(0, self.start_recording)

        def on_release(key: keyboard.Key | keyboard.KeyCode) -> None:
            if key != keyboard.Key.alt_r:
                return

            self.hotkey_pressed = False
            if not self.toggle_mode.get():
                self.root.after(0, self.stop_recording)

        try:
            with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                listener.join()
        except Exception as exc:
            self.logger.warning("Слушатель горячей клавиши остановлен: %s", exc)

    def toggle_recording(self) -> None:
        """Переключает запись по кнопке или горячей клавише."""
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        """Запускает поток записи микрофона."""
        if self.is_recording:
            return

        self.is_recording = True
        self.record_start_ts = time.monotonic()
        self.latest_level = 0.0
        self.status_label.config(text="Идёт запись...")
        self.record_button.config(text="Остановить запись")

        if self.play_start_sound.get():
            threading.Thread(
                target=self._play_system_sound,
                args=("Glass",),
                daemon=True,
            ).start()

        self._tick_live_status()
        self.record_thread = threading.Thread(target=self._record_worker, daemon=True)
        self.record_thread.start()

    def stop_recording(self) -> None:
        """Останавливает запись и инициирует финальную обработку."""
        if not self.is_recording:
            return

        self.is_recording = False
        self.status_label.config(text="Завершаю запись и распознаю...")
        self.record_button.config(text="Подождите...", state="disabled")

    def _record_worker(self) -> None:
        """Фоновый цикл записи аудио и последующей транскрибации."""
        import numpy as np
        import sounddevice as sd

        sample_rate = 16000
        chunk_size = int(sample_rate * 0.1)
        audio_chunks: list[np.ndarray] = []

        try:
            with sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=chunk_size,
            ) as stream:
                while self.is_recording:
                    data, overflow = stream.read(chunk_size)
                    if overflow:
                        self.logger.warning("Переполнение аудиобуфера во время записи")
                    audio_chunks.append(data.copy())
                    self.latest_level = float(abs(data).mean())
        except Exception:
            self.root.after(0, lambda: self._finish_recording_error(str(exc)))
            return

        if not audio_chunks:
            self.root.after(0, self._finish_recording_empty)
            return

        audio = np.concatenate(audio_chunks, axis=0).reshape(-1).astype(np.float32)
        duration = len(audio) / sample_rate
        text = self.engine.transcribe(audio)
        self.root.after(0, lambda: self._finish_recording_success(text, duration))

    def _finish_recording_empty(self) -> None:
        """Обработка случая, когда запись оборвалась без данных."""
        self._reset_after_recording()
        self.status_label.config(text="Запись остановлена: аудио не получено")

    def _finish_recording_error(self, error_text: str) -> None:
        """Показывает ошибку записи и возвращает UI в исходное состояние."""
        self._reset_after_recording()
        self.status_label.config(text=f"Ошибка записи: {error_text}")
        self.logger.error("Ошибка записи: %s", error_text)

    def _finish_recording_success(self, text: str, duration: float) -> None:
        """Публикует результат распознавания и запускает автовставку."""
        self._reset_after_recording()

        clean_text = text.strip()
        if not clean_text:
            self.status_label.config(
                text=f"Речь не распознана (длительность {duration:.1f} с)"
            )
            return

        self.last_transcribed_text = clean_text
        self._append_history(clean_text)
        self.status_label.config(
            text=f"Готово: {duration:.1f} с, символов {len(clean_text)}"
        )

        if self.auto_paste.get():
            try:
                target_app = self._paste_to_active_app(clean_text)
                self.status_label.config(
                    text=f"Транскрибация вставлена в: {target_app}"
                )
            except Exception as exc:
                self.status_label.config(text="Транскрибация сохранена, вставка не удалась")
                self.logger.warning("Ошибка автовставки: %s", exc)

    def _reset_after_recording(self) -> None:
        """Возвращает элементы UI в состояние ожидания."""
        self.is_recording = False
        self.latest_level = 0.0
        self.record_button.config(text="Начать запись", state="normal")

    def _tick_live_status(self) -> None:
        """Обновляет таймер и приблизительный индикатор уровня входного сигнала."""
        if not self.is_recording:
            self.live_label.config(text="Таймер: 00:00  Уровень: [..........]")
            return

        elapsed = int(time.monotonic() - self.record_start_ts)
        minutes, seconds = divmod(elapsed, 60)
        meter_value = max(0, min(10, int(self.latest_level * 120)))
        meter = "#" * meter_value + "." * (10 - meter_value)
        self.live_label.config(
            text=f"Таймер: {minutes:02d}:{seconds:02d}  Уровень: [{meter}]"
        )
        self.root.after(150, self._tick_live_status)

    def _append_history(self, text: str) -> None:
        """Добавляет запись в историю и сохраняет состояние на диск."""
        self.state = self.storage.push_history(self.state, text)
        self._save_state()
        self._render_history()

    def _render_history(self) -> None:
        """Перерисовывает список последних транскрибаций в текстовом окне."""
        self.history_box.config(state="normal")
        self.history_box.delete("1.0", tk.END)

        for item in reversed(self.state.history):
            shown_time = self._format_time(item.timestamp)
            self.history_box.insert(
                tk.END,
                f"[{shown_time}]\n{item.text}\n\n",
            )

        self.history_box.config(state="disabled")
        self.history_box.see("1.0")

    @staticmethod
    def _format_time(raw_timestamp: str) -> str:
        """Нормализует ISO-дату в короткий формат для UI."""
        try:
            value = datetime.fromisoformat(raw_timestamp)
            return value.strftime("%H:%M:%S")
        except Exception:
            return raw_timestamp

    def _paste_to_active_app(self, text: str) -> str:
        """Копирует текст в буфер и отправляет Cmd+V в текущее активное приложение."""
        pyperclip.copy(text)
        time.sleep(0.08)

        was_visible = self.root.state() != "withdrawn"
        if was_visible:
            self.root.withdraw()
            self.root.update_idletasks()
            time.sleep(0.2)

        script = """
        tell application "System Events"
            set frontApp to name of first application process whose frontmost is true
            tell process frontApp
                key code 9 using command down
            end tell
            return frontApp
        end tell
        """

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                text=True,
            )
            target_app = result.stdout.strip() or "неизвестное приложение"
            return target_app
        finally:
            if was_visible and not self.is_shutting_down:
                self.root.deiconify()
                self._apply_topmost()

    def _play_system_sound(self, sound_name: str) -> None:
        """Проигрывает короткий системный звук, подтверждающий старт записи."""
        sound_file = f"/System/Library/Sounds/{sound_name}.aiff"
        try:
            subprocess.run(["afplay", sound_file], check=False)
        except Exception as exc:
            self.logger.warning("Не удалось проиграть звук %s: %s", sound_name, exc)

    def _on_settings_changed(self) -> None:
        """Сохраняет переключатели сразу после изменения."""
        self._apply_topmost()
        self._save_state()

    def _apply_topmost(self) -> None:
        """Применяет режим "поверх окон" к главному окну."""
        self.root.attributes("-topmost", self.is_on_top.get())

    def _save_state(self) -> None:
        """Сохраняет текущие настройки и историю."""
        self.state.settings = {
            "always_on_top": self.is_on_top.get(),
            "auto_paste": self.auto_paste.get(),
            "toggle_mode": self.toggle_mode.get(),
            "play_start_sound": self.play_start_sound.get(),
        }
        self.storage.save(self.state)

    def copy_last(self) -> None:
        """Копирует последнюю транскрибацию в буфер обмена."""
        if not self.state.history:
            self.status_label.config(text="История пуста")
            return
        last_text = self.state.history[-1].text
        pyperclip.copy(last_text)
        self.status_label.config(text="Последняя транскрибация скопирована")

    def clear_history(self) -> None:
        """Очищает историю после подтверждения пользователя."""
        if not self.state.history:
            return

        should_clear = messagebox.askyesno(
            "Очистка истории",
            "Удалить все сохранённые транскрибации?",
        )
        if not should_clear:
            return

        self.state.history = []
        self._save_state()
        self._render_history()
        self.status_label.config(text="История очищена")

    def _start_tray(self) -> None:
        """Поднимает иконку в статус-баре для работы приложения в фоне."""
        if not TRAY_SUPPORTED:
            self.logger.warning("pystray/Pillow не установлены: режим трея отключён")
            return

        icon = self._build_tray_icon_image()
        menu = pystray.Menu(
            TrayItem("Показать окно", self._tray_show),
            TrayItem("Старт/стоп запись", self._tray_toggle_recording),
            TrayItem("Выход", self._tray_exit),
        )
        self.tray_icon = pystray.Icon("krab-ear", icon, "Krab Ear", menu)

        # pystray имеет собственный цикл событий, поэтому запускается в отдельном потоке.
        thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        thread.start()

    @staticmethod
    def _build_tray_icon_image() -> Image.Image:
        """Создаёт простую иконку трея без внешних ассетов."""
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse([5, 5, 59, 59], fill=(220, 50, 47, 255))
        draw.text((24, 17), "K", fill=(255, 255, 255, 255))
        return image

    def _tray_show(self, icon: pystray.Icon, _: TrayItem) -> None:
        self.root.after(0, self.show_window)

    def _tray_toggle_recording(self, icon: pystray.Icon, _: TrayItem) -> None:
        self.root.after(0, self.toggle_recording)

    def _tray_exit(self, icon: pystray.Icon, _: TrayItem) -> None:
        self.root.after(0, self.exit_app)

    def hide_window(self) -> None:
        """Скрывает окно, не завершая процесс."""
        self._save_state()
        self.root.withdraw()

    def show_window(self) -> None:
        """Возвращает окно из фонового режима."""
        self.root.deiconify()
        self.root.lift()
        self._apply_topmost()

    def exit_app(self) -> None:
        """Полностью завершает приложение и останавливает трей."""
        if self.is_shutting_down:
            return

        self.is_shutting_down = True
        self.is_recording = False
        self._save_state()

        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        self.root.destroy()
