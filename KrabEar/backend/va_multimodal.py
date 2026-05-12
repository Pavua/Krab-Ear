"""Voice Assistant Phase 2A — Multimodal (vision) клиент для Krab Ear backend.

Skeleton file. Phase 2A — vision injection only.
Barge-in (Phase 2.4) и language switch (Phase 2.5) — отдельные PR.

СТАТУС: НЕ подключён к IPC dispatch. НЕ подключён к ConversationViewController.
Готово для Wave 56+.

Архитектура (spec: docs/superpowers/specs/2026-05-12-va-phase2-design.md §3.1–3.3):
  - Dual-model routing: text-only → gemma-4-26b baseline (1587ms p50)
                        image turn  → supergemma4-26b-abliterated-multimodal-mlx (~4800ms p50)
  - LM Studio "second slot" swap на первый image turn (~5–10s cold load).
  - Image кодируется как base64 PNG/JPEG и передаётся в messages[].content
    в формате OpenAI Vision API: {"type": "image_url", "image_url": {"url": "data:..."}}.
  - НЕ требует mlx_lock — HTTP request к LM Studio, не прямой MLX-вызов.

Переменные окружения / settings (добавить в DEFAULT_SETTINGS когда проводить в IPC):
  va_vision_enabled    : bool — включает vision path (дефолт False, opt-in)
  va_vision_model      : str  — имя модели в LM Studio для vision turn
  lm_studio_url        : str  — базовый URL LM Studio (shared с LLMRewriter)
  lm_studio_api_key    : str  — Bearer token (shared с LLMRewriter)
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger("KrabEar.Backend.VAMultimodal")

# Имя multimodal-модели по умолчанию (R22 winner, spec §3.1)
_DEFAULT_VISION_MODEL = "supergemma4-26b-abliterated-multimodal-mlx"

# Таймаут для vision turn (модель cold-load + vision encoder = до 30s на M-series).
# Baseline llm_rewriter использует 45s; vision turn ещё медленнее при cold start.
_VISION_TIMEOUT_SEC = 60.0

# Максимальный размер изображения для base64-инъекции.
# LM Studio/llama.cpp ограничивают context — большие изображения → OOM.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB


@dataclass
class VAMultimodalResult:
    """Результат vision-turn запроса. Всегда возвращается, никогда не raises.

    Повторяет контракт LLMRewriteResult для однородности в pipeline.
    """

    ok: bool
    text: Optional[str]
    model_used: Optional[str]
    latency_ms: Optional[int]
    fallback_reason: Optional[str] = field(default=None)

    def text_or_fallback(self, fallback: str) -> str:
        if self.ok and self.text:
            return self.text
        return fallback


class MultimodalVAClient:
    """HTTP-клиент для vision-turn запросов к LM Studio (OpenAI-compatible API).

    Используется ТОЛЬКО когда к разговору прикреплён скриншот.
    Для text-only turns используется обычный LLMRewriter через основной pipeline.

    Пример использования (Phase 2A, не в IPC ещё):

        client = MultimodalVAClient(
            base_url="http://localhost:1234",
            api_key="lm-studio-key",
            vision_model="supergemma4-26b-abliterated-multimodal-mlx",
        )
        result = client.send_with_image(
            text="Что на этом скриншоте?",
            image_path=Path("/tmp/screenshot.png"),
            conversation_history=[
                {"role": "user", "content": "Привет"},
                {"role": "assistant", "content": "Привет!"},
            ],
        )
        if result.ok:
            print(result.text)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        vision_model: str = _DEFAULT_VISION_MODEL,
        timeout_sec: float = _VISION_TIMEOUT_SEC,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._vision_model = vision_model
        self._timeout_sec = timeout_sec
        self._session = requests.Session()
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._session.headers["Content-Type"] = "application/json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_with_image(
        self,
        text: str,
        image_path: Path,
        conversation_history: Optional[list[dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> VAMultimodalResult:
        """Отправить vision-turn запрос с изображением в LM Studio.

        Args:
            text: Текстовый запрос пользователя (поверх изображения).
            image_path: Путь к PNG/JPEG скриншоту.
            conversation_history: Предшествующий диалог (list of role/content dicts).
                Каждый элемент: {"role": "user"|"assistant", "content": "..."}.
                Прикрепляется ДО текущего vision turn.
            system_prompt: Опциональный system message. Если None — стандартный
                голосовой ассистент промпт.

        Returns:
            VAMultimodalResult — всегда (никогда не raises).

        NOTE (Phase 2A): image_path должен быть локальным файлом.
        FSEvents watcher в ConversationViewController+Vision.swift будет передавать
        абсолютный путь к последнему ~/Desktop/Screenshot *.png.
        """
        t0 = time.monotonic()
        try:
            image_b64, mime_type = self._encode_image(image_path)
        except (OSError, ValueError) as exc:
            logger.warning("va_multimodal: encode error %s → %s", image_path, exc)
            return VAMultimodalResult(
                ok=False,
                text=None,
                model_used=None,
                latency_ms=None,
                fallback_reason=f"image_encode_error:{exc}",
            )

        messages = self._build_messages(
            text=text,
            image_b64=image_b64,
            mime_type=mime_type,
            conversation_history=conversation_history or [],
            system_prompt=system_prompt,
        )

        payload: dict[str, Any] = {
            "model": self._vision_model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 512,
            "stream": False,
        }

        try:
            resp = self._session.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
                timeout=self._timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            reply_text = data["choices"][0]["message"]["content"]
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "va_multimodal: vision turn ok model=%s latency_ms=%d",
                self._vision_model,
                latency_ms,
            )
            return VAMultimodalResult(
                ok=True,
                text=reply_text,
                model_used=self._vision_model,
                latency_ms=latency_ms,
            )
        except requests.exceptions.ConnectionError:
            reason = "lm_studio_not_running"
        except requests.exceptions.Timeout:
            reason = f"timeout_{self._timeout_sec}s"
        except requests.exceptions.HTTPError as exc:
            reason = f"http_{exc.response.status_code}"
        except (KeyError, IndexError, ValueError) as exc:
            reason = f"parse_error:{exc}"
        except Exception as exc:  # pragma: no cover — safety net
            reason = f"unexpected:{type(exc).__name__}:{exc}"

        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("va_multimodal: vision turn failed reason=%s", reason)
        return VAMultimodalResult(
            ok=False,
            text=None,
            model_used=self._vision_model,
            latency_ms=latency_ms,
            fallback_reason=reason,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_image(image_path: Path) -> tuple[str, str]:
        """Считать файл, вернуть (base64_string, mime_type).

        Raises:
            OSError: файл не найден / нет доступа.
            ValueError: размер превышает _MAX_IMAGE_BYTES.
        """
        data = image_path.read_bytes()
        if len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(
                f"image too large: {len(data)} bytes > {_MAX_IMAGE_BYTES} limit"
            )
        suffix = image_path.suffix.lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        mime_type = mime_map.get(suffix, "image/png")
        b64 = base64.b64encode(data).decode("ascii")
        return b64, mime_type

    @staticmethod
    def _build_messages(
        text: str,
        image_b64: str,
        mime_type: str,
        conversation_history: list[dict[str, Any]],
        system_prompt: Optional[str],
    ) -> list[dict[str, Any]]:
        """Собрать messages[] в формате OpenAI Vision API.

        Структура:
          [system?] + conversation_history + [user: image + text]

        Формат vision content (spec §3.2):
          messages[-1]["content"] = [
              {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
              {"type": "text", "text": "<user question>"},
          ]
        """
        messages: list[dict[str, Any]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        else:
            messages.append({
                "role": "system",
                "content": (
                    "Ты — голосовой ассистент Краб. Отвечай кратко и по делу. "
                    "Пользователь поделился скриншотом. Опиши что видишь и ответь на вопрос."
                ),
            })

        # Prior turns (plain text — prior images ephemeral per spec §8)
        for turn in conversation_history:
            messages.append({"role": turn["role"], "content": str(turn.get("content", ""))})

        # Current vision turn
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                },
                {"type": "text", "text": text},
            ],
        })

        return messages
