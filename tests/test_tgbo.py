#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тесты логики tgbo.py. Сеть не используется, в Telegram ничего не отправляется.

Запуск:  python tests/test_tgbo.py
Работает и из корня репозитория, и из папки tests.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

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
        for name in ("AI_PROVIDER", "AI_MODEL", "MISTRAL_API_KEY", "ORCA_API_KEY",
                     "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def names(self):
        return [p.name for p in tgbo.provider_order()]

    def test_mistral_is_first_when_key_present(self):
        """Mistral со своим ключом — основной: он не тратит токены на reasoning."""
        os.environ["MISTRAL_API_KEY"] = "test-key"
        self.assertEqual(self.names()[0], "Mistral")

    def test_mistral_hidden_without_key(self):
        self.assertNotIn("Mistral", self.names())

    def test_keyless_routers_always_available(self):
        """LLM7 и Pollinations работают без ключа и присутствуют всегда."""
        names = self.names()
        self.assertIn("LLM7", names)
        self.assertIn("Pollinations", names)

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
        os.environ["MISTRAL_API_KEY"] = "test-key"
        os.environ["ORCA_API_KEY"] = "test-key"
        os.environ["GEMINI_API_KEY"] = "test-key"
        for requested, expected in (
            ("mistral", "Mistral"),
            ("llm7", "LLM7"),
            ("pollinations", "Pollinations"),
            ("gemini", "Gemini"),
            ("google", "Gemini"),      # алиас
            ("orca", "OrcaRouter"),    # алиас
        ):
            with self.subTest(requested=requested):
                os.environ["AI_PROVIDER"] = requested
                self.assertEqual(self.names(), [expected])

    def test_explicit_provider_without_key_falls_back_to_auto(self):
        """Выбрали Mistral, но ключа нет — работаем на том, что доступно."""
        os.environ["AI_PROVIDER"] = "mistral"
        names = self.names()
        self.assertNotIn("Mistral", names)
        self.assertEqual(names[0], "LLM7")

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
        """MISTRAL_MODEL не должен влиять на LLM7 и наоборот."""
        os.environ["MISTRAL_MODEL"] = "mistral-only-model"
        llm7 = next(p for p in tgbo.PROVIDERS if p.name == "LLM7")
        self.assertNotIn("mistral-only-model", llm7.model_list())

    def test_mistral_models_avoid_paid_only_ones_first(self):
        """На бесплатном тарифе mistral-small отдаёт 429 — он не первый."""
        mistral = next(p for p in tgbo.PROVIDERS if p.name == "Mistral")
        self.assertNotEqual(mistral.model_list()[0], "mistral-small-latest")


class TestPromptStyles(unittest.TestCase):
    """Промпт должен требовать абсурд и запрещать философию."""

    def test_full_prompt_forbids_philosophy(self):
        text = tgbo.system_prompt("full").lower()
        for word in ("философ", "мораль", "абсурд", "рассмешить"):
            with self.subTest(word=word):
                self.assertIn(word, text)

    def test_compact_prompt_is_short(self):
        """Короткий промпт нужен, чтобы reasoning-модели не тратили бюджет."""
        self.assertLess(len(tgbo.system_prompt("compact")), 400)

    def test_compact_prompt_still_forbids_philosophy(self):
        text = tgbo.system_prompt("compact").lower()
        self.assertIn("абсурд", text)
        self.assertIn("философ", text)

    def test_unknown_style_falls_back_to_full(self):
        self.assertEqual(tgbo.system_prompt("чепуха"), tgbo.SYSTEM_PROMPT_FULL)

    def test_payload_uses_requested_style(self):
        full = tgbo.build_payload("m", style="full")["messages"][0]["content"]
        compact = tgbo.build_payload("m", style="compact")["messages"][0]["content"]
        self.assertNotEqual(full, compact)
        self.assertLess(len(compact), len(full))

    def test_payload_has_enough_tokens_for_reasoning_models(self):
        """700 токенов не хватало: reasoning съедал бюджет и content был пуст."""
        self.assertGreaterEqual(tgbo.build_payload("m")["max_tokens"], 1200)


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

    def test_reads_json_field_first(self):
        """Поле retry_after в JSON — самый точный источник."""
        body = json.dumps({"error": {"code": "quota_exceeded", "retry_after": 62701}})
        self.assertEqual(tgbo._parse_retry_after(None, body), 62701.0)

    def test_long_quota_is_reported_uncapped(self):
        """Дневную квоту не обрезаем: по её величине решаем пропустить модель."""
        body = json.dumps({"error": {"retry_after": 62701}})
        value = tgbo._parse_retry_after(None, body)
        self.assertEqual(value, 62701.0)
        self.assertGreater(value, tgbo.MAX_RETRY_WAIT)

    def test_header_wins_when_larger(self):
        self.assertEqual(tgbo._parse_retry_after("30", "Retry after 5 seconds"), 30.0)

    def test_short_limit_is_below_threshold(self):
        """Короткий рейт-лимит должен быть меньше порога ожидания."""
        value = tgbo._parse_retry_after("14", "")
        self.assertLessEqual(value, tgbo.MAX_RETRY_WAIT)

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
        self.assertEqual(params, ["model", "extra_note", "style"])


class TestEmojiSequences(unittest.TestCase):
    """Эмодзи режутся целыми последовательностями, а не по символам."""

    def test_short_text_untouched(self):
        text = "Обычный текст без эмодзи."
        self.assertEqual(tgbo.limit_emoji(text), text)

    def test_single_emoji_kept(self):
        text = "Привет 👋"
        self.assertEqual(tgbo.limit_emoji(text), text)

    def test_extra_emoji_removed(self):
        result = tgbo.limit_emoji("Раз 😀 два 😀 три 😀")
        self.assertLessEqual(len(tgbo._EMOJI_SEQ_RE.findall(result)), tgbo.MAX_EMOJI)

    def test_zwj_sequence_not_torn_apart(self):
        """👨‍👩‍👧 — одна последовательность. Обрывков быть не должно."""
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        text = f"Семья {family} и всё."
        self.assertEqual(tgbo.limit_emoji(text), text)

    def test_flag_not_torn_apart(self):
        flag = "\U0001f1f7\U0001f1fa"          # 🇷🇺 — два code point'а
        text = f"Флаг {flag}"
        self.assertEqual(tgbo.limit_emoji(text), text)

    def test_skin_tone_not_torn_apart(self):
        wave = "\U0001f44b\U0001f3fd"          # 👋🏽
        text = f"Привет {wave}"
        self.assertEqual(tgbo.limit_emoji(text), text)

    def test_no_orphan_joiners_left(self):
        """Висячих ZWJ и модификаторов быть не должно.

        ZWJ ВНУТРИ сохранённого составного эмодзи — это нормально,
        он там обязателен. Плохо, если он остался от вырезанного эмодзи.
        """
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        wave = "\U0001f44b\U0001f3fd"
        text = f"А {family} б {wave} в"
        result = tgbo.limit_emoji(text)

        # Осталась ровно одна эмодзи-последовательность — семья, целиком.
        self.assertEqual(len(tgbo._EMOJI_SEQ_RE.findall(result)), 1)
        self.assertIn(family, result)
        self.assertNotIn(wave, result)

        # Ни один ZWJ или модификатор не должен «висеть» без соседа:
        # знаки эмодзи не должны примыкать к ZWJ справа.
        self.assertNotRegex(result, "\u200d(?![^\u200d\U0001f3fb-\U0001f3ff])")
        for modifier in ("\U0001f3fb", "\U0001f3fc", "\U0001f3fd", "\U0001f3fe", "\U0001f3ff"):
            with self.subTest(modifier=modifier):
                self.assertNotIn(modifier, result)

    def test_text_without_emoji_is_untouched(self):
        text = "Просто текст, ни одного смайлика здесь нет и быть не должно."
        self.assertEqual(tgbo.limit_emoji(text), text)

    def test_limit_zero_removes_all(self):
        result = tgbo.limit_emoji("Раз 😀 два", limit=0)
        self.assertNotIn("😀", result)


class TestPostLengthConsistency(unittest.TestCase):
    def test_prompt_and_code_limits_agree(self):
        """Промпт обещает 700 символов — код должен требовать то же."""
        self.assertIn("700", tgbo.SYSTEM_PROMPT_FULL)
        self.assertEqual(tgbo.MAX_POST_CHARS, 700)

    def test_over_limit_text_is_truncated_not_rejected(self):
        text = ("Это довольно длинное предложение для проверки обрезки. " * 20).strip()
        result = tgbo.validate_post(text)
        self.assertLessEqual(len(result), tgbo.MAX_POST_CHARS)


class _FakeResponse:
    """Подмена ответа urllib для тестов без сети."""

    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _chat_response(text, reasoning=""):
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": text, "reasoning": reasoning},
                "finish_reason": "stop",
            }
        ],
        "usage": {"total_tokens": 10},
    }


def _http_error(code, body="{}", retry_after=None):
    """Свежий HTTPError.

    Важно: HTTPError — файлоподобный объект, его тело читается один раз.
    Поэтому каждый вызов возвращает НОВЫЙ объект; для side_effect
    используйте _http_error_factory.
    """
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://example.test", code, "err", headers, io.BytesIO(body.encode("utf-8"))
    )


def _http_error_factory(code, body="{}", retry_after=None):
    """Функция, отдающая новый HTTPError на каждый вызов.

    Годится для подмены urllib.request.urlopen — то есть для проверки
    настоящего http_json и его классификации ошибок.
    """
    def build(*_args, **_kwargs):
        raise _http_error(code, body, retry_after)
    return build


def _provider_error_factory(code, body="{}", retry_after=None):
    """Функция, отдающая ProviderError — то, что http_json реально бросает.

    Нужна там, где подменяется сам http_json: он не должен пропускать
    HTTPError наружу, поэтому тест обязан эмулировать именно ProviderError.
    """
    retryable, reason = tgbo._classify_http_error(code)

    def build(*_args, **_kwargs):
        raise tgbo.ProviderError(
            f"HTTP {code} — {body}", retryable=retryable,
            retry_after=retry_after, reason=reason,
        )
    return build


# Тексты для тестов ДОЛЖНЫ проходить validate_post: минимум 80 символов.
POST_A = (
    "Абсурдный пост про стул, который ведёт совещание вместо начальника отдела, "
    "и никто в кабинете не решается ему возразить."
)
POST_B = (
    "Совсем другой текст про метро, дождь и человека с зонтом-тростью, который "
    "упорно отказывался входить в вагон без своего отражения."
)
POST_C = (
    "Мороз скрежетал под ботинками, и город казался сделанным из тонкого стекла, "
    "а троллейбус ехал молча, будто извиняясь за опоздание."
)
POST_D = (
    "Абсурдный пост про тележку, которая едет только влево, и молчащего "
    "грузчика, который знал об этом заранее и ничего не сказал."
)


class TestGenerateFromRetries(unittest.TestCase):
    """Пункт 3 ревью: временные ошибки отделены от постоянных."""

    def setUp(self):
        self.provider = tgbo.Endpoint("Тест", "https://example.test/v1/chat", ("m1", "m2"))
        self.saved = os.environ.copy()
        os.environ["AI_MODEL"] = "m1,m2"
        self.slept: list[float] = []
        patcher = mock.patch.object(tgbo.time, "sleep", side_effect=self.slept.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Паузы между повторами обнуляем: иначе тесты реально спят.
        delay_patcher = mock.patch.object(tgbo, "RETRY_DELAYS", ())
        delay_patcher.start()
        self.addCleanup(delay_patcher.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_success_on_first_try(self):
        with mock.patch.object(tgbo, "http_json", return_value=_chat_response(POST_A)) as call:
            text, model = tgbo.generate_from(self.provider, "")
        self.assertEqual(text, POST_A)
        self.assertEqual(model, "m1")
        self.assertEqual(call.call_count, 1)

    def test_retries_after_rate_limit_then_succeeds(self):
        """429 с коротким Retry-After -> ждём и повторяем, а не сдаёмся.

        Подменяется сам http_json, поэтому он обязан бросать ProviderError —
        именно это он и делает в реальности.
        """
        with mock.patch.object(
            tgbo, "http_json",
            side_effect=_provider_error_factory(429, '{"error":{"retry_after":3}}', 3),
        ) as call:
            with self.assertRaises(tgbo.ProviderError):
                tgbo.generate_from(self.provider, "")
        # После лимита была ПАУЗА и повтор, а не мгновенный отказ.
        self.assertIn(3.0, self.slept)
        self.assertGreaterEqual(call.call_count, 2)
        # И повторов НЕ бесконечно: модель, которая вечно просит подождать,
        # отбрасывается после MAX_QUOTA_WAITS ожиданий. Без этого ограничения
        # бот долбил бы API весь бюджет роутера (наблюдалось на практике).
        # Ожиданий не больше, чем MAX_QUOTA_WAITS на каждую модель и на каждый
        # из двух вариантов промпта.
        expected_max = tgbo.MAX_QUOTA_WAITS * len(self.provider.models) * len(tgbo.PROMPT_STYLES)
        self.assertLessEqual(self.slept.count(3.0), expected_max)
        self.assertLessEqual(call.call_count, expected_max + len(tgbo.PROMPT_STYLES) * 2 + 2)

    def test_no_access_skips_whole_provider_immediately(self):
        """401/403 — постоянная ошибка: не долбим остальные модели."""
        with mock.patch.object(tgbo, "http_json",
                               side_effect=_provider_error_factory(403)) as call:
            with self.assertRaises(tgbo.ProviderError) as ctx:
                tgbo.generate_from(self.provider, "")
        self.assertEqual(call.call_count, 1)          # вторая модель не пробовалась
        self.assertEqual(ctx.exception.reason, "no_access")
        self.assertEqual(self.slept, [])              # и не спали зря

    def test_empty_content_moves_to_next_model_without_sleeping(self):
        """Пустой ответ детерминирован — повторять ту же модель бессмысленно."""
        responses = [_chat_response("", reasoning="долго думал"), _chat_response(POST_A)]
        with mock.patch.object(tgbo, "http_json", side_effect=responses) as call:
            text, model = tgbo.generate_from(self.provider, "")
        self.assertEqual(text, POST_A)
        self.assertEqual(model, "m2")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.slept, [])

    def test_all_models_fail_raises(self):
        with mock.patch.object(tgbo, "http_json", side_effect=_provider_error_factory(500)):
            with self.assertRaises(tgbo.ProviderError):
                tgbo.generate_from(self.provider, "")

    def test_long_quota_is_not_waited_for(self):
        """Дневная квота в часах — не ждём её, сразу другая модель."""
        with mock.patch.object(tgbo, "http_json",
                               side_effect=_provider_error_factory(429, '', 88000)) as call:
            with self.assertRaises(tgbo.ProviderError):
                tgbo.generate_from(self.provider, "")
        self.assertEqual(self.slept, [])
        self.assertGreaterEqual(call.call_count, 1)

    def test_deadline_stops_further_attempts(self):
        """Пункт 2 ревью: общий дедлайн реально ограничивает работу."""
        deadline = time.monotonic() - 1   # уже просрочен
        with mock.patch.object(tgbo, "http_json") as call:
            with self.assertRaises(tgbo.ProviderError):
                tgbo.generate_from(self.provider, "", deadline=deadline)
        self.assertEqual(call.call_count, 0)   # ни одного запроса

    def test_classification_helper(self):
        self.assertEqual(tgbo._classify_http_error(403), (False, "no_access"))
        self.assertEqual(tgbo._classify_http_error(404), (False, "no_access"))
        self.assertEqual(tgbo._classify_http_error(401), (False, "no_access"))
        self.assertEqual(tgbo._classify_http_error(400), (False, "bad_request"))
        self.assertEqual(tgbo._classify_http_error(429), (True, "rate_limit"))
        self.assertEqual(tgbo._classify_http_error(402), (True, "rate_limit"))
        self.assertEqual(tgbo._classify_http_error(503), (True, "server"))


class TestHttpJsonErrors(unittest.TestCase):
    """Проверка настоящего http_json: подменяем urlopen, а не http_json."""

    URL = "https://example.test/v1/chat"

    def _call(self, side_effect):
        with mock.patch.object(tgbo.urllib.request, "urlopen", side_effect=side_effect):
            return tgbo.http_json(self.URL, {"model": "m"}, {})

    def test_success_returns_parsed_json(self):
        response = _FakeResponse({"choices": [{"message": {"content": "текст"}}]})
        with mock.patch.object(tgbo.urllib.request, "urlopen", return_value=response):
            data = tgbo.http_json(self.URL, {"model": "m"}, {})
        self.assertIn("choices", data)

    def test_403_becomes_no_access_and_is_not_retryable(self):
        with self.assertRaises(tgbo.ProviderError) as ctx:
            self._call(_http_error_factory(403, '{"error":{"code":"model_access_denied"}}'))
        self.assertEqual(ctx.exception.reason, "no_access")
        self.assertFalse(ctx.exception.retryable)

    def test_401_becomes_no_access(self):
        with self.assertRaises(tgbo.ProviderError) as ctx:
            self._call(_http_error_factory(401, '{"error":"Invalid API key"}'))
        self.assertEqual(ctx.exception.reason, "no_access")
        self.assertFalse(ctx.exception.retryable)

    def test_429_is_retryable_and_carries_retry_after(self):
        with self.assertRaises(tgbo.ProviderError) as ctx:
            self._call(_http_error_factory(429, '{"error":{"retry_after":7}}', retry_after=7))
        self.assertEqual(ctx.exception.reason, "rate_limit")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_402_is_treated_as_rate_limit(self):
        """Pollinations отдаёт 402 в смысле «лимит», а не «оплатите»."""
        with self.assertRaises(tgbo.ProviderError) as ctx:
            self._call(_http_error_factory(402, '{"error":{"code":"FREE_LIMIT"}}'))
        self.assertEqual(ctx.exception.reason, "rate_limit")
        self.assertTrue(ctx.exception.retryable)

    def test_500_is_retryable_server_error(self):
        with self.assertRaises(tgbo.ProviderError) as ctx:
            self._call(_http_error_factory(503))
        self.assertEqual(ctx.exception.reason, "server")
        self.assertTrue(ctx.exception.retryable)

    def test_400_is_not_retryable(self):
        with self.assertRaises(tgbo.ProviderError) as ctx:
            self._call(_http_error_factory(400, '{"error":"bad"}'))
        self.assertEqual(ctx.exception.reason, "bad_request")
        self.assertFalse(ctx.exception.retryable)

    def test_network_error_is_marked(self):
        with self.assertRaises(tgbo.ProviderError) as ctx:
            self._call(urllib.error.URLError("нет сети"))
        self.assertEqual(ctx.exception.reason, "network")

    def test_non_json_body_is_bad_response(self):
        response = mock.MagicMock()
        response.read.return_value = b"<html>not json</html>"
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        with mock.patch.object(tgbo.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(tgbo.ProviderError) as ctx:
                tgbo.http_json(self.URL, {"model": "m"}, {})
        self.assertEqual(ctx.exception.reason, "bad_response")

    def test_key_never_lands_in_request_url(self):
        """Ключ передаётся заголовком, а не в URL."""
        request = urllib.request.Request(
            "https://example.test/v1/x", data=b"{}", method="POST"
        )
        request.add_header("Authorization", "Bearer secret-key")
        self.assertNotIn("secret-key", request.full_url)


class TestCreatePostDedup(unittest.TestCase):
    """Пункт 7: дедупликация внутри цикла генерации."""

    def setUp(self):
        self.saved = os.environ.copy()
        os.environ["AI_PROVIDER"] = "llm7"
        os.environ["AI_MODEL"] = "m1"
        os.environ.pop("CUSTOM_PROMPT", None)
        patcher = mock.patch.object(tgbo.time, "sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_duplicate_is_rejected_and_regenerated(self):
        existing = POST_D
        recent = [{"hash": tgbo.post_hash(existing), "text": existing,
                   "posted_at": "2026-01-01T00:00:00+00:00"}]
        with mock.patch.object(tgbo, "http_json",
                               side_effect=[_chat_response(existing), _chat_response(POST_C)]) as call:
            text, _provider, _model = tgbo.create_post(recent)
        self.assertEqual(text, POST_C)
        self.assertEqual(call.call_count, 2)

    def test_dedup_can_be_disabled(self):
        existing = POST_D
        recent = [{"hash": tgbo.post_hash(existing), "text": existing}]
        with mock.patch.object(tgbo, "http_json", return_value=_chat_response(existing)) as call:
            text, _p, _m = tgbo.create_post(recent, enforce_dedup=False)
        self.assertEqual(text, existing)
        self.assertEqual(call.call_count, 1)

    def test_recent_posts_are_given_to_the_model(self):
        """Модель должна видеть недавние посты, чтобы не повторяться."""
        recent = [{"hash": "x", "text": "Прошлый пост про кофе и утро в понедельник."}]
        with mock.patch.object(tgbo, "http_json",
                               return_value=_chat_response(POST_B)) as call:
            tgbo.create_post(recent)
        payload = call.call_args[0][1]
        user_message = payload["messages"][1]["content"]
        self.assertIn("Прошлый пост про кофе", user_message)


class TestMainBehaviour(unittest.TestCase):
    """Пункт 1 ревью: DRY_RUN не должен трогать историю публикаций."""

    def setUp(self):
        self.saved = os.environ.copy()
        self.tmp = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmp, "posted.json")
        os.environ["STATE_PATH"] = self.state_path
        os.environ["AI_PROVIDER"] = "llm7"
        os.environ["AI_MODEL"] = "m1"
        # Паузы между повторами в этих тестах не нужны — только замедляют.
        delay_patcher = mock.patch.object(tgbo, "RETRY_DELAYS", ())
        delay_patcher.start()
        self.addCleanup(delay_patcher.stop)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_write_state(self):
        os.environ["DRY_RUN"] = "1"
        with mock.patch.object(tgbo, "http_json", return_value=_chat_response(POST_A)):
            code = tgbo.main()
        self.assertEqual(code, 0)
        self.assertFalse(
            os.path.exists(self.state_path),
            "DRY_RUN не должен создавать файл истории",
        )

    def test_dry_run_does_not_touch_existing_state(self):
        os.environ["DRY_RUN"] = "1"
        original = {"recent": [{"hash": "oldhash", "text": "Старый пост",
                                "posted_at": "2026-01-01T00:00:00+00:00"}]}
        tgbo.save_state(self.state_path, original)
        with open(self.state_path, encoding="utf-8") as handle:
            before = handle.read()
        with mock.patch.object(tgbo, "http_json", return_value=_chat_response(POST_B)):
            code = tgbo.main()
        self.assertEqual(code, 0)
        with open(self.state_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_real_publish_does_write_state(self):
        os.environ["DRY_RUN"] = "0"
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:TEST"
        os.environ["CHANNEL_ID"] = "@test_channel"
        # Порядок вызовов: getChat (проверка канала) -> отправка -> генерация идёт
        # первой, поэтому список начинается с ответа модели.
        chat_info = {"ok": True, "result": {"id": 1, "type": "channel", "title": "Тест"}}
        sent = {"ok": True, "result": {"message_id": 42}}
        with mock.patch.object(tgbo, "http_json",
                               side_effect=[_chat_response(POST_A), chat_info, sent]) as call:
            code = tgbo.main()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.state_path))
        state = tgbo.load_state(self.state_path)
        self.assertEqual(len(state["recent"]), 1)
        self.assertEqual(state["recent"][0]["hash"], tgbo.post_hash(POST_A))
        self.assertEqual(call.call_count, 3)
        # Последний вызов — именно отправка сообщения.
        self.assertIn("sendMessage", call.call_args_list[-1][0][0])
        # В истории помечен как опубликованный, а не тестовый.
        self.assertEqual(state["recent"][0]["provider"], "LLM7")

    def test_dry_run_failure_returns_nonzero(self):
        os.environ["DRY_RUN"] = "1"
        with mock.patch.object(tgbo, "http_json", side_effect=_provider_error_factory(500)):
            code = tgbo.main()
        self.assertEqual(code, 1)

    def test_missing_bot_token_fails_before_generating(self):
        os.environ.pop("DRY_RUN", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        with mock.patch.object(tgbo, "http_json",
                               return_value=_chat_response(POST_A)) as call:
            code = tgbo.main()
        self.assertEqual(code, 1)
        self.assertEqual(call.call_count, 1)   # генерация была, отправки нет


class TestTelegramLayer(unittest.TestCase):
    """Пункт 7: ошибки Telegram без реальных запросов."""

    def test_http_error_is_raised(self):
        with mock.patch.object(tgbo, "http_json",
                               side_effect=_provider_error_factory(401, '{"ok":false}')):
            with self.assertRaises(tgbo.ProviderError):
                tgbo.telegram_call("123:TEST", "sendMessage", {"chat_id": "@c", "text": "t"})

    def test_api_level_error_becomes_bot_error(self):
        response = {"ok": False, "error_code": 400, "description": "chat not found"}
        with mock.patch.object(tgbo, "http_json", return_value=response):
            with self.assertRaises(tgbo.BotError) as ctx:
                tgbo.telegram_call("123:TEST", "sendMessage", {"chat_id": "@c", "text": "t"})
        self.assertIn("chat not found", str(ctx.exception))
        self.assertIn("400", str(ctx.exception))

    def test_send_omits_parse_mode_and_disables_preview(self):
        with mock.patch.object(tgbo, "http_json",
                               return_value={"ok": True, "result": {"message_id": 1}}) as call:
            tgbo.send_to_telegram("123:TEST", "@chan", "Текст *со* спецсимволами _и_ [скобками]")
        payload = call.call_args[0][1]
        self.assertNotIn("parse_mode", payload)
        self.assertNotIn("link_preview_options", payload)
        self.assertTrue(payload["disable_web_page_preview"])
        self.assertEqual(payload["text"], "Текст *со* спецсимволами _и_ [скобками]")

    def test_test_chat_overrides_channel(self):
        os.environ["TELEGRAM_TEST_CHAT_ID"] = "555"
        os.environ["CHANNEL_ID"] = "@real_channel"
        self.addCleanup(lambda: os.environ.pop("TELEGRAM_TEST_CHAT_ID", None))
        with mock.patch.object(tgbo, "http_json") as call:
            chat_id = tgbo.resolve_target_chat("123:TEST")
        self.assertEqual(chat_id, "555")
        self.assertEqual(call.call_count, 0)   # канал не проверялся

    def test_channel_without_access_gives_clear_error(self):
        os.environ.pop("TELEGRAM_TEST_CHAT_ID", None)
        os.environ["CHANNEL_ID"] = "@nope"
        with mock.patch.object(tgbo, "http_json",
                               return_value={"ok": False, "error_code": 403,
                                             "description": "bot is not a member"}):
            with self.assertRaises(tgbo.BotError) as ctx:
                tgbo.resolve_target_chat("123:TEST")
        self.assertIn("администратор", str(ctx.exception))

    def test_missing_channel_id_is_reported(self):
        os.environ.pop("TELEGRAM_TEST_CHAT_ID", None)
        os.environ.pop("CHANNEL_ID", None)
        with self.assertRaises(tgbo.BotError):
            tgbo.resolve_target_chat("123:TEST")


class TestStateDurability(unittest.TestCase):
    """Пункт 5: запись состояния должна быть атомарной."""

    def test_save_is_atomic_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "posted.json")
            state = {"recent": []}
            tgbo.remember(state, "Текст поста для проверки атомарной записи.", "p", "m")
            tgbo.save_state(path, state)
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(f"{path}.tmp"))

    def test_save_overwrites_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "posted.json")
            tgbo.save_state(path, {"recent": [{"text": "первый"}]})
            tgbo.save_state(path, {"recent": [{"text": "второй"}]})
            loaded = tgbo.load_state(path)
            self.assertEqual(loaded["recent"][0]["text"], "второй")

    def test_corrupted_state_does_not_crash_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "posted.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"recent": [{"text": "обрыв')
            self.assertEqual(tgbo.load_state(path), {"recent": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
