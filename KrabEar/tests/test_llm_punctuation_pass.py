"""Unit tests для fix_punctuation_only() — минимальный LLM punctuation pass.

Покрывает:
1. Успешное добавление запятых (mock ответ LM Studio)
2. Reject когда LLM изменил слово (word-set mismatch)
3. Reject когда количество слов изменилось (word-count guard)
4. LM Studio недоступен (ConnectionError) → None, graceful
5. Пустой input → возвращает оригинал без HTTP-запроса
6. Выбор locale prompt (Spanish / English)
7. CircuitBreaker open → немедленно возвращает None
8. Temperature 0.0 в теле запроса
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Настройка путей для запуска как standalone
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_rewriter():
    """Фабрика LLMRewriter с тестовыми параметрами."""
    from backend.llm_rewriter import LLMRewriter
    return LLMRewriter(
        base_url="http://localhost:1234/v1",
        api_key="test-key",
        model="test-model",
        timeout_sec=2.0,
        circuit_fail_threshold=3,
        circuit_initial_reset_sec=60,
    )


def _mock_response(content: str, status_code: int = 200):
    """Создаёт mock requests.Response с заданным содержимым."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return mock_resp


class TestFixPunctuationOnlySuccess(unittest.TestCase):
    """Тест 1: успешное добавление запятых к длинному предложению."""

    def test_adds_commas_to_long_sentence(self):
        rewriter = _make_rewriter()
        input_text = "я пошёл в магазин купил хлеб молоко и вернулся домой"
        expected = "я пошёл в магазин, купил хлеб, молоко и вернулся домой."

        with patch.object(rewriter._session, "post", return_value=_mock_response(expected)):
            result = rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertEqual(result, expected)

    def test_returns_string_on_success(self):
        rewriter = _make_rewriter()
        input_text = "привет как дела"
        output = "Привет, как дела?"

        with patch.object(rewriter._session, "post", return_value=_mock_response(output)):
            result = rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertIsInstance(result, str)
        self.assertIsNotNone(result)


class TestFixPunctuationOnlyWordSetGuard(unittest.TestCase):
    """Тест 2: reject когда LLM изменил слово (word-set mismatch)."""

    def test_rejects_word_substitution(self):
        rewriter = _make_rewriter()
        input_text = "я иду в магазин"
        # LLM поменял "иду" на "хожу" — слова не совпадают
        llm_output = "я хожу в магазин."

        with patch.object(rewriter._session, "post", return_value=_mock_response(llm_output)):
            result = rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertIsNone(result)

    def test_rejects_word_deletion(self):
        rewriter = _make_rewriter()
        input_text = "он пошёл в большой магазин"
        # LLM удалил "большой"
        llm_output = "Он пошёл в магазин."

        with patch.object(rewriter._session, "post", return_value=_mock_response(llm_output)):
            result = rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertIsNone(result)

    def test_rejects_word_addition(self):
        rewriter = _make_rewriter()
        input_text = "всё хорошо"
        # LLM добавил лишнее слово
        llm_output = "Всё очень хорошо!"

        with patch.object(rewriter._session, "post", return_value=_mock_response(llm_output)):
            result = rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertIsNone(result)


class TestFixPunctuationOnlyWordCountGuard(unittest.TestCase):
    """Тест 3: reject когда количество слов изменилось."""

    def test_rejects_when_word_count_differs(self):
        rewriter = _make_rewriter()
        input_text = "раз два три четыре"  # 4 слова
        # LLM вернул 3 слова
        llm_output = "раз, два, три."

        with patch.object(rewriter._session, "post", return_value=_mock_response(llm_output)):
            result = rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertIsNone(result)

    def test_accepts_when_word_count_matches(self):
        rewriter = _make_rewriter()
        input_text = "раз два три четыре"  # 4 слова
        # LLM вернул те же 4 слова с пунктуацией
        llm_output = "Раз, два, три, четыре."

        with patch.object(rewriter._session, "post", return_value=_mock_response(llm_output)):
            result = rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertIsNotNone(result)


class TestFixPunctuationOnlyUnreachable(unittest.TestCase):
    """Тест 4: LM Studio недоступен → возвращает None gracefully."""

    def test_connection_error_returns_none(self):
        import requests as req
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post", side_effect=req.ConnectionError("refused")):
            result = rewriter.fix_punctuation_only("текст без знаков", language="ru")

        self.assertIsNone(result)

    def test_timeout_returns_none(self):
        import requests as req
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post", side_effect=req.Timeout("timed out")):
            result = rewriter.fix_punctuation_only("текст без знаков", language="ru")

        self.assertIsNone(result)

    def test_http_500_returns_none(self):
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post",
                          return_value=_mock_response("", status_code=500)):
            result = rewriter.fix_punctuation_only("текст без знаков", language="ru")

        self.assertIsNone(result)


class TestFixPunctuationOnlyEmptyInput(unittest.TestCase):
    """Тест 5: пустой input → возвращает оригинал без HTTP-запроса."""

    def test_empty_string_returns_unchanged(self):
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post") as mock_post:
            result = rewriter.fix_punctuation_only("", language="ru")
            mock_post.assert_not_called()

        self.assertEqual(result, "")

    def test_whitespace_only_returns_unchanged(self):
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post") as mock_post:
            result = rewriter.fix_punctuation_only("   ", language="ru")
            mock_post.assert_not_called()

        # Returns original text (whitespace), not None
        self.assertIsNotNone(result)
        self.assertEqual(result, "   ")

    def test_none_input_returns_empty(self):
        rewriter = _make_rewriter()

        with patch.object(rewriter._session, "post") as mock_post:
            result = rewriter.fix_punctuation_only(None, language="ru")  # type: ignore
            mock_post.assert_not_called()

        self.assertEqual(result, None)


class TestFixPunctuationOnlyLocale(unittest.TestCase):
    """Тест 6: правильный locale prompt для Spanish / English."""

    def _capture_system_prompt(self, rewriter, language: str, input_text: str) -> str:
        """Вспомогательная функция: перехватывает system prompt из запроса."""
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["messages"] = json.get("messages", [])
            output = " ".join(input_text.split())  # same words
            return _mock_response(output)

        with patch.object(rewriter._session, "post", side_effect=fake_post):
            rewriter.fix_punctuation_only(input_text, language=language)

        system_msgs = [m for m in captured.get("messages", []) if m.get("role") == "system"]
        return system_msgs[0]["content"] if system_msgs else ""

    def test_spanish_prompt_used_for_es(self):
        rewriter = _make_rewriter()
        prompt = self._capture_system_prompt(rewriter, "es", "hola como estas bien")
        self.assertIn("puntuación", prompt)
        self.assertIn("PROHIBIDO", prompt)

    def test_english_prompt_used_for_en(self):
        rewriter = _make_rewriter()
        prompt = self._capture_system_prompt(rewriter, "en", "hello how are you today")
        self.assertIn("punctuation editor", prompt)
        self.assertIn("FORBIDDEN", prompt)

    def test_russian_prompt_fallback_for_unknown_lang(self):
        rewriter = _make_rewriter()
        prompt = self._capture_system_prompt(rewriter, "uk", "привіт як справи")
        # Falls back to Russian prompt
        self.assertIn("ЗАПРЕЩЕНО", prompt)

    def test_russian_prompt_used_for_ru(self):
        rewriter = _make_rewriter()
        prompt = self._capture_system_prompt(rewriter, "ru", "привет как дела")
        self.assertIn("ЗАПРЕЩЕНО", prompt)


class TestFixPunctuationOnlyCircuitBreaker(unittest.TestCase):
    """Тест 7: CircuitBreaker open → немедленно возвращает None."""

    def test_circuit_open_returns_none(self):
        rewriter = _make_rewriter()
        import requests as req

        # Открываем circuit breaker через 3 провала
        for _ in range(3):
            with patch.object(rewriter._session, "post",
                              side_effect=req.ConnectionError("refused")):
                rewriter.fix_punctuation_only("тест", language="ru")

        self.assertEqual(rewriter._circuit.state, "open")

        # Следующий вызов должен вернуть None без HTTP
        with patch.object(rewriter._session, "post") as mock_post:
            result = rewriter.fix_punctuation_only("ещё один тест", language="ru")
            mock_post.assert_not_called()

        self.assertIsNone(result)


class TestFixPunctuationOnlyTemperature(unittest.TestCase):
    """Тест 8: temperature=0.0 передаётся в тело запроса."""

    def test_temperature_is_zero(self):
        rewriter = _make_rewriter()
        input_text = "проверка температуры запроса"
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _mock_response(" ".join(input_text.split()))

        with patch.object(rewriter._session, "post", side_effect=fake_post):
            rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertIn("payload", captured)
        self.assertEqual(captured["payload"]["temperature"], 0.0)

    def test_model_in_payload(self):
        rewriter = _make_rewriter()
        input_text = "ещё одна проверка"
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _mock_response(" ".join(input_text.split()))

        with patch.object(rewriter._session, "post", side_effect=fake_post):
            rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertEqual(captured["payload"]["model"], "test-model")

    def test_stream_is_false(self):
        rewriter = _make_rewriter()
        input_text = "и ещё одна"
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return _mock_response(" ".join(input_text.split()))

        with patch.object(rewriter._session, "post", side_effect=fake_post):
            rewriter.fix_punctuation_only(input_text, language="ru")

        self.assertFalse(captured["payload"]["stream"])


if __name__ == "__main__":
    unittest.main()
