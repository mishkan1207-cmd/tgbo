#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты логики tgbo.py. Сеть не используется, в Telegram ничего не отправляется.

Запуск:  python tests/test_tgbo.py
Работает и из корня репозитория, и из папки tests.
"""

import json
import os
import re
import sys
import tempfile
import unittest

# Добавляем корень репозитория в путь импорта.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tgbo  # noqa: E402


class TestCleanPost(unittest.TestCase):
    def test_removes_code_fence(self):
        raw = "```\nСегодня заметил странную вещь: люди читают инструкции только когда всё сломалось.\n```"
        cleaned = tgbo.clean_post(raw)
        self.assertNotIn("```", cleaned)
        self.assertTrue(cleaned.startswith("Сегодня заметил"))

    def test_removes_language_fence(self):
        raw = "```markdown\n# Заголовок\n\nПросто текст поста, который должен остаться.\n```"
        cleaned = tgbo.clean_post(raw)
        self.assertNotIn("```", cleaned)
        self.assertNotIn("#", cleaned)
        self.assertIn("Просто текст поста", cleaned)

    def test_removes_lead_in(self):
        raw = "Вот пост:\n\nИногда полезно ничего не делать. Мозг сам находит решение."
        cleaned = tgbo.clean_post(raw)
        self.assertFalse(cleaned.lower().startswith("вот пост"))
        self.assertTrue(cleaned.startswith("Иногда полезно"))

    def test_removes_english_lead_in(self):
        raw = "Here's a post:\n\nThe city is loudest at 4 am, when nobody is performing."
        cleaned = tgbo.clean_post(raw)
        self.assertTrue(cleaned.startswith("The city is loudest"))

    def test_removes_bold_markdown(self):
        cleaned = tgbo.clean_post("**Важно**: вода закипает быстрее, если крышка закрыта.")
        self.assertNotIn("**", cleaned)
        self.assertIn("Важно", cleaned)

    def test_strips_wrapping_quotes(self):
        cleaned = tgbo.clean_post("«Просто мысль в кавычках, которая должна остаться.»")
        self.assertFalse(cleaned.startswith("«"))
        self.assertFalse(cleaned.endswith("»"))

    def test_collapses_blank_lines(self):
        cleaned = tgbo.clean_post("Первая строка.\n\n\n\n\nВторая строка.")
        self.assertNotIn("\n\n\n", cleaned)

    def test_keeps_clean_text_unchanged(self):
        raw = "Обычный текст без разметки. Вторая фраза. Третья."
        self.assertEqual(tgbo.clean_post(raw), raw)

    def test_empty_input(self):
        self.assertEqual(tgbo.clean_post(""), "")
        self.assertEqual(tgbo.clean_post("   \n  "), "")


class TestValidatePost(unittest.TestCase):
    def test_accepts_normal_post(self):
        text = "Иногда самый полезный совет — ничего не менять. " * 2
        self.assertEqual(tgbo.validate_post(text), text)

    def test_rejects_empty(self):
        with self.assertRaises(tgbo.BotError):
            tgbo.validate_post("")

    def test_rejects_too_short(self):
        with self.assertRaises(tgbo.BotError):
            tgbo.validate_post("Коротко.")

    def test_rejects_text_without_letters(self):
        with self.assertRaises(tgbo.BotError):
            tgbo.validate_post("1234567890 " * 10)

    def test_rejects_over_telegram_limit(self):
        with self.assertRaises(tgbo.BotError):
            tgbo.validate_post("слово " * 1000)

    def test_truncates_long_post_at_sentence(self):
        sentence = "Это достаточно длинное предложение для проверки обрезки текста. "
        text = sentence * 40  # заметно больше MAX_POST_CHARS
        result = tgbo.validate_post(text)
        self.assertLessEqual(len(result), tgbo.MAX_POST_CHARS)
        self.assertGreaterEqual(len(result), tgbo.MIN_POST_CHARS)
        self.assertTrue(result.endswith("."))


class TestDeduplication(unittest.TestCase):
    def test_post_hash_is_stable(self):
        self.assertEqual(tgbo.post_hash("Привет  мир"), tgbo.post_hash("привет мир"))

    def test_identical_text_is_duplicate(self):
        text = "Сегодня понял простую вещь: спешка почти всегда стоит дороже ожидания."
        recent = [{"hash": tgbo.post_hash(text), "text": text}]
        self.assertIsNotNone(tgbo.find_duplicate(text, recent))

    def test_near_identical_text_is_duplicate(self):
        original = "Сегодня понял простую вещь: спешка почти всегда стоит дороже ожидания."
        minor = "Сегодня понял простую вещь: спешка почти всегда стоит дороже ожидания!"
        recent = [{"hash": tgbo.post_hash(original), "text": original}]
        self.assertIsNotNone(tgbo.find_duplicate(minor, recent))

    def test_different_text_is_not_duplicate(self):
        recent = [{
            "hash": tgbo.post_hash("Разговор о кофе и утренних привычках."),
            "text": "Разговор о кофе и утренних привычках.",
        }]
        other = "Мороз скрипел под ботинками, и город казался сделанным из стекла."
        self.assertIsNone(tgbo.find_duplicate(other, recent))

    def test_similarity_bounds(self):
        self.assertAlmostEqual(tgbo.similarity("одно и то же", "одно и то же"), 1.0)
        self.assertEqual(tgbo.similarity("", "текст"), 0.0)

    def test_tolerates_malformed_state(self):
        self.assertIsNone(tgbo.find_duplicate("любой текст здесь", [None, "мусор", {}]))


class TestStateHandling(unittest.TestCase):
    def test_remember_keeps_newest_first_and_limits(self):
        state = {"recent": []}
        for index in range(tgbo.RECENT_KEEP + 5):
            tgbo.remember(state, f"Текст поста номер {index} с достаточной длиной.", "gemini", "m")
        self.assertEqual(len(state["recent"]), tgbo.RECENT_KEEP)
        self.assertIn("номер 24", state["recent"][0]["text"])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "posted.json")
            state = {"recent": []}
            tgbo.remember(state, "Текст для проверки сохранения состояния.", "gemini", "m")
            tgbo.save_state(path, state)
            loaded = tgbo.load_state(path)
            self.assertEqual(loaded["recent"][0]["hash"], state["recent"][0]["hash"])

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(tgbo.load_state("нет-такого-файла.json"), {"recent": []})

    def test_load_corrupted_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{не json")
            self.assertEqual(tgbo.load_state(path), {"recent": []})


class TestPayloadAndParsing(unittest.TestCase):
    def test_payload_has_required_fields(self):
        payload = tgbo.build_payload("test-model")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("только текст поста", payload["messages"][0]["content"].lower())
        self.assertIn("max_tokens", payload)

    def test_extract_text_from_normal_response(self):
        response = {"choices": [{"message": {"role": "assistant", "content": "Привет"}}]}
        self.assertEqual(tgbo.extract_text(response), "Привет")

    def test_extract_text_from_parts_list(self):
        response = {"choices": [{"message": {"content": [{"text": "Часть один "}, {"text": "часть два"}]}}]}
        self.assertEqual(tgbo.extract_text(response), "Часть один часть два")

    def test_extract_text_raises_on_error_field(self):
        response = {"error": {"code": "free_quota_exhausted", "message": "no free model"}}
        with self.assertRaises(tgbo.ProviderError) as ctx:
            tgbo.extract_text(response)
        self.assertIn("free_quota_exhausted", str(ctx.exception))

    def test_extract_text_raises_on_empty_choices(self):
        with self.assertRaises(tgbo.ProviderError):
            tgbo.extract_text({"choices": []})

    def test_extract_text_raises_on_empty_content(self):
        response = {"choices": [{"message": {"content": "   "}, "finish_reason": "length"}]}
        with self.assertRaises(tgbo.ProviderError) as ctx:
            tgbo.extract_text(response)
        self.assertIn("length", str(ctx.exception))

    def test_extract_text_raises_on_missing_choices(self):
        with self.assertRaises(tgbo.ProviderError):
            tgbo.extract_text({"usage": {}})


class TestResponseSanitising(unittest.TestCase):
    """Главная проверка: в канал уходит текст поста, а не служебное сообщение."""

    def test_simulated_model_answer_becomes_clean_post(self):
        raw = (
            "Конечно! Вот пост:\n\n"
            "```\n"
            "**Наблюдение:** в лифте все смотрят в пол. Не потому что неловко, "
            "а потому что зеркало на потолке — единственный способ увидеть, "
            "как выглядит терпение.\n"
            "```\n\n"
            "Надеюсь, вам понравилось!"
        )
        cleaned = tgbo.clean_post(raw)
        text = tgbo.validate_post(cleaned)
        self.assertNotIn("```", text)
        self.assertNotIn("**", text)
        self.assertNotIn("Конечно", text)
        self.assertNotIn("Вот пост", text)
        self.assertNotIn("Надеюсь", text)
        self.assertTrue(text.startswith("Наблюдение:"))


class TestProviderSelection(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        for name in ("AI_PROVIDER", "AI_MODEL", "ORCA_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def names(self):
        return [p.name for p in tgbo.provider_order()]

    def test_auto_starts_with_llm7(self):
        """LLM7 работает без ключа, поэтому всегда первый."""
        self.assertEqual(self.names()[0], "LLM7")

    def test_auto_includes_pollinations(self):
        self.assertIn("Pollinations", self.names())

    def test_optional_providers_hidden_without_key(self):
        names = self.names()
        self.assertNotIn("OrcaRouter", names)
        self.assertNotIn("Gemini", names)

    def test_optional_providers_appear_with_key(self):
        os.environ["ORCA_API_KEY"] = "test-key"
        os.environ["GEMINI_API_KEY"] = "test-key"
        names = self.names()
        self.assertIn("OrcaRouter", names)
        self.assertIn("Gemini", names)

    def test_explicit_provider_selection(self):
        os.environ["AI_PROVIDER"] = "gemini"
        self.assertEqual(self.names(), ["Gemini"])
        os.environ["AI_PROVIDER"] = "llm7"
        self.assertEqual(self.names(), ["LLM7"])
        os.environ["AI_PROVIDER"] = "orca"
        self.assertEqual(self.names(), ["OrcaRouter"])

    def test_unknown_provider_falls_back_to_auto(self):
        os.environ["AI_PROVIDER"] = "чепуха"
        self.assertEqual(self.names()[0], "LLM7")

    def test_keyless_endpoints_send_no_auth_header(self):
        for provider in tgbo.PROVIDERS:
            if not provider.key_env:
                with self.subTest(provider=provider.name):
                    self.assertEqual(provider.headers(), {})

    def test_ai_model_override_applies_to_llm7(self):
        os.environ["AI_MODEL"] = "model-one, model-two"
        llm7 = next(p for p in tgbo.PROVIDERS if p.name == "LLM7")
        self.assertEqual(llm7.model_list(), ["model-one", "model-two"])

    def test_model_env_is_not_leaked_between_providers(self):
        """ORCA_MODEL не должен влиять на LLM7 и наоборот."""
        os.environ["ORCA_MODEL"] = "orca-only-model"
        llm7 = next(p for p in tgbo.PROVIDERS if p.name == "LLM7")
        self.assertNotIn("orca-only-model", llm7.model_list())


class TestRetryAfter(unittest.TestCase):
    def test_reads_header(self):
        self.assertEqual(tgbo._parse_retry_after("14", ""), 14.0)

    def test_reads_from_body_text(self):
        body = '{"error":{"message":"Rate limit exceeded. Retry after 14 seconds."}}'
        self.assertEqual(tgbo._parse_retry_after(None, body), 14.0)

    def test_body_variants(self):
        for text in ("retry_after: 7", "Retry After 7", '"retry_after": 7'):
            with self.subTest(text=text):
                self.assertEqual(tgbo._parse_retry_after(None, text), 7.0)

    def test_capped_at_maximum(self):
        self.assertEqual(tgbo._parse_retry_after("9999", ""), float(tgbo.MAX_RETRY_WAIT))

    def test_none_when_absent(self):
        self.assertIsNone(tgbo._parse_retry_after(None, "no hint here"))
        self.assertIsNone(tgbo._parse_retry_after("", ""))

    def test_garbage_header_ignored(self):
        self.assertIsNone(tgbo._parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", ""))

    def test_provider_error_carries_retry_after(self):
        error = tgbo.ProviderError("boom", retryable=True, retry_after=12.0)
        self.assertEqual(error.retry_after, 12.0)
        self.assertTrue(error.retryable)

    def test_provider_error_defaults_to_none(self):
        self.assertIsNone(tgbo.ProviderError("boom").retry_after)


class TestModuleSafety(unittest.TestCase):
    def test_no_hardcoded_secrets_in_source(self):
        """В исходнике не должно быть строк, похожих на настоящие ключи.

        Проверяем именно форматы секретов, а не имена переменных: имена вида
        `bot_token` — это обычные параметры функций и они допустимы.
        """
        with open(os.path.join(ROOT, "tgbo.py"), "r", encoding="utf-8") as handle:
            source = handle.read()

        patterns = {
            "Google API key": r"AIza[0-9A-Za-z_\-]{10,}",
            "Telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}",
            "OpenAI-style key": r"\bsk-[A-Za-z0-9_\-]{16,}",
            "GitHub token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}",
        }
        for label, pattern in patterns.items():
            with self.subTest(secret=label):
                self.assertIsNone(
                    re.search(pattern, source),
                    f"В tgbo.py найден похожий на секрет фрагмент: {label}",
                )

    def test_secrets_come_only_from_environment(self):
        """Ключи читаются из окружения, а не из констант в коде."""
        with open(os.path.join(ROOT, "tgbo.py"), "r", encoding="utf-8") as handle:
            source = handle.read()

        # Все провайдеры, которым нужен ключ, должны объявлять имя переменной
        # окружения, а не хранить сам ключ.
        for provider in tgbo.PROVIDERS:
            if provider.key_env:
                with self.subTest(provider=provider.name):
                    for env_name in provider.key_env:
                        self.assertTrue(
                            env_name.isupper(),
                            f"{provider.name}: имя переменной окружения должно быть в верхнем регистре",
                        )

        self.assertIn("TELEGRAM_BOT_TOKEN", source)
        self.assertIn("os.getenv", source)

        # Ни одна ключевая переменная не должна присваиваться из литерала.
        for name in ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "ORCA_API_KEY"):
            with self.subTest(name=name):
                self.assertNotRegex(source, rf'{name}\s*=\s*["\'][^"\']+["\']')

    def test_provider_urls_are_https(self):
        for provider in tgbo.PROVIDERS:
            with self.subTest(provider=provider.name):
                self.assertTrue(provider.url.startswith("https://"))

    def test_payload_builder_has_no_key_parameter(self):
        """Тело запроса собирается без ключа — ключ идёт только в заголовок."""
        import inspect

        params = list(inspect.signature(tgbo.build_payload).parameters)
        self.assertEqual(params, ["model", "extra_note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
