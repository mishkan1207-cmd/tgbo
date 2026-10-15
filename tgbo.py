#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tgbo.py — автопостинг в Telegram-канал через бесплатный ИИ-API.

Запускается из GitHub Actions (.github/workflows/autopost.yml).

Провайдеры (по умолчанию — Google Gemini, бесплатный тариф AI Studio):
  * gemini — https://generativelanguage.googleapis.com/v1beta/openai/chat/completions
  * orca   — https://api.orcarouter.ai/v1/chat/completions   (только если ключ реально работает)

Зависимости: только стандартная библиотека Python 3.10+ (в workflow — 3.12).

Переменные окружения:
  GEMINI_API_KEY       (или GOOGLE_API_KEY / ORCA_API_KEY) — ключ ИИ-провайдера
  TELEGRAM_BOT_TOKEN   — токен бота от @BotFather
  CHANNEL_ID           — @username канала или числовой id (например -1001234567890)

Необязательные:
  AI_PROVIDER          — gemini | orca | auto   (по умолчанию auto)
  AI_MODEL             — переопределить список моделей (через запятую)
  DRY_RUN              — 1: не публиковать в Telegram, только показать текст
  TELEGRAM_TEST_CHAT_ID— если задан, пост уйдёт в этот чат вместо канала
  SKIP_DEDUP           — 1: не проверять дубликаты
  STATE_PATH           — путь к файлу состояния (по умолчанию state/posted.json)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Настройки
# --------------------------------------------------------------------------

TELEGRAM_LIMIT = 4096          # жёсткий лимит Telegram на длину сообщения
MAX_POST_CHARS = 1200          # наш собственный лимит: пост должен быть коротким
MIN_POST_CHARS = 80            # меньше — считаем ответ мусором
HTTP_TIMEOUT = 60              # секунд на один HTTP-запрос
RETRY_DELAYS = (5, 20)         # паузы между повторами внутри одной модели
# Всё время на генерацию. Защита от того, что медленный провайдер съест
# таймаут job'а (в workflow стоит timeout-minutes: 10).
GENERATION_BUDGET_SECONDS = 300
MAX_GENERATION_ATTEMPTS = 3    # сколько раз просить пост у одного провайдера
MAX_RETRY_WAIT = 60            # максимум секунд ожидания по Retry-After
RECENT_KEEP = 20               # сколько последних постов хранить в state
DEDUP_THRESHOLD = 0.75         # схожесть шинглов, выше — дубликат

# --------------------------------------------------------------------------
# Провайдеры (роутеры бесплатных моделей)
# --------------------------------------------------------------------------
# Порядок: сначала самый стабильный и качественный, дальше — резервы.
# Все они OpenAI-совместимы, поэтому код запроса одинаковый.

# 1) LLM7.io — бесплатный роутер, работает вообще БЕЗ ключа.
#    Проверено: отвечает, понимает русский. Есть рейт-лимит, который API
#    сообщает сам («Retry after N seconds») — tgbo.py его уважает.
LLM7_URL = "https://api.llm7.io/v1/chat/completions"
LLM7_MODELS = (
    "DeepSeek-V4-Flash-0731",
    "GLM-5.3-Flash",
    "minimax-m2.7",
    "gpt-oss:20b",
    "mistral-Nemo-Instruct-2407",
)

# 2) Pollinations — тоже бесплатный и без ключа.
#    Проверено: генерирует хорошие русские посты. Изредка отдаёт HTTP 402
#    (перегрузка) — тогда просто переходим к следующему провайдеру.
POLLINATIONS_URL = "https://text.pollinations.ai/openai"
POLLINATIONS_MODELS = ("openai", "openai-fast")

# 3) OrcaRouter — необязательный резерв. Включается только если задан
#    ORCA_API_KEY и модели разрешены этому ключу. Роутер "orcarouter/free"
#    намеренно не используется: он отдаёт 403 free_quota_exhausted и требует
#    привязки «зрелого» GitHub-аккаунта (err_free_access_denied).
ORCA_URL = "https://api.orcarouter.ai/v1/chat/completions"
ORCA_MODELS = (
    "deepseek/deepseek-v4-flash-free",
    "z-ai/glm-5.3-flash-free",
    "tencent/hy3-free",
)

# 4) Google Gemini — необязательный резерв, если у вас есть ключ.
#    Внимание: консоль Google AI Studio недоступна из России.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
)

SYSTEM_PROMPT = (
    "Ты — автор Telegram-канала без узкой тематики. Ты пишешь живые, "
    "интересные и оригинальные короткие посты на русском языке.\n"
    "\n"
    "Требования к ответу:\n"
    "1. Верни ТОЛЬКО текст поста — от первого сообщения до последнего.\n"
    "2. Никаких пояснений, вступлений, заголовков, подписей и комментариев "
    "от себя. Не пиши «Вот пост», «Конечно», «Надеюсь, понравилось».\n"
    "3. Не используй markdown: без символов #, *, _, `, без блоков кода.\n"
    "4. Объём — от 2 до 5 предложений, не больше 700 символов.\n"
    "5. Пиши естественным человеческим языком, без канцелярита и без "
    "шаблонных вступлений вроде «В современном мире».\n"
    "6. Не задавай вопросов читателю в конце и не призывай ставить лайки.\n"
    "7. Не упоминай, что текст написан ИИ.\n"
    "8. Можно использовать эмодзи, но не больше двух на весь пост.\n"
)

USER_PROMPT = (
    "Напиши один новый оригинальный пост. Сам выбери тему и форму: "
    "наблюдение из жизни, короткая история, неожиданный факт, мысль вслух, "
    "лёгкий юмор или абсурд. Только текст поста, без пояснений."
)


class BotError(RuntimeError):
    """Ошибка, из-за которой запуск считается неуспешным."""


class ProviderError(BotError):
    """Провайдер недоступен или отверг запрос."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        # Если API сам сказал, сколько ждать (HTTP 429 + Retry-After),
        # ждём ровно столько, а не наугад.
        self.retry_after = retry_after


def log(message: str) -> None:
    """Печать с отметкой времени. Секреты сюда попадать не должны."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_json(
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: int = HTTP_TIMEOUT,
) -> dict:
    """Выполнить HTTP-запрос и вернуть разобранный JSON."""
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"

    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", "tgbo-autopost/2.0 (+github-actions)")
    if payload is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - тело может быть недоступно
            pass

        retry_after = _parse_retry_after(exc.headers.get("Retry-After"), body)
        raise ProviderError(
            f"HTTP {exc.code} {exc.reason} — {_short(body, 600)}",
            retryable=exc.code in (408, 409, 425, 429, 500, 502, 503, 504),
            retry_after=retry_after,
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Сеть недоступна: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError("Превышено время ожидания ответа API") from exc

    if not raw.strip():
        raise ProviderError("API вернул пустой ответ", retryable=False)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"API вернул не JSON: {_short(raw, 300)}", retryable=False) from exc

    if not isinstance(parsed, dict):
        raise ProviderError("Неожиданный формат ответа API", retryable=False)
    return parsed


def _short(text: str, limit: int) -> str:
    """Обрезать длинный текст для логов и сообщений об ошибке."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_retry_after(header: str | None, body: str) -> float | None:
    """Сколько секунд просит подождать API.

    Берём из заголовка Retry-After, а если его нет — из текста ошибки вида
    «Rate limit exceeded. Retry after 14 seconds». Больше MAX_RETRY_WAIT не ждём.
    """
    candidates: list[float] = []

    if header:
        try:
            candidates.append(float(str(header).strip()))
        except (TypeError, ValueError):
            pass

    if body:
        match = re.search(r"retry[_ -]?after[\"'\s:]*(\d+(?:\.\d+)?)", body, re.IGNORECASE)
        if match:
            try:
                candidates.append(float(match.group(1)))
            except ValueError:
                pass

    if not candidates:
        return None
    return max(0.0, min(max(candidates), MAX_RETRY_WAIT))


# --------------------------------------------------------------------------
# Очистка ответа модели
# --------------------------------------------------------------------------

# Обрамляющие блоки кода: убираем саму ограду, содержимое оставляем.
_FENCE_EDGE_RE = re.compile(
    r"\A\s*```[a-zA-Z0-9_+-]*[ \t]*\n?(?P<body>.*?)\n?[ \t]*```\s*\Z",
    re.DOTALL,
)
_FENCE_ANY_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n?")
# Типичные «служебные» начала, которые модель иногда добавляет от себя.
_LEAD_RE = re.compile(
    r"^(?:"
    r"вот\s+(?:вариант|пост|текст)[^\n:：]*[:：]?"
    r"|конечно[,!.]?"
    r"|хорошо[,!.]?"
    r"|пост\s*[:：]"
    r"|текст\s+поста\s*[:：]"
    r"|заголовок\s*[:：][^\n]*\n"
    r"|here(?:'s| is)[^\n:]*[:：]"
    r")\s*",
    re.IGNORECASE,
)
# Хвосты-подписи вида «Надеюсь, вам понравилось!».
# Ограничиваем длину и запрещаем перевод строки, чтобы не съесть конец поста.
_TAIL_RE = re.compile(
    r"\n*\s*(?:надеюсь\b[^\n]{0,60}|если\s+хотите\b[^\n]{0,60}|"
    r"как\s+вам\b[^\n]{0,60}\?|что\s+думаете\b[^\n]{0,60}\?)\s*$",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002700-\U000027bf\U0001f1e6-\U0001f1ff\U00002600-\U000026ff]"
)
MAX_EMOJI = 2                  # больше — выглядит как спам


def limit_emoji(text: str, limit: int = MAX_EMOJI) -> str:
    """Оставить не больше `limit` эмодзи, лишние убрать."""
    found = list(_EMOJI_RE.finditer(text))
    if len(found) <= limit:
        return text
    keep = {match.start() for match in found[:limit]}
    return "".join(ch for i, ch in enumerate(text) if i in keep or not _EMOJI_RE.match(ch))


def clean_post(raw: str) -> str:
    """Превратить ответ модели в чистый текст поста.

    Убирает markdown-обёртку, кавычки, служебные вступления и лишние пустые
    строки. Возвращает текст без изменений, если он и так чистый.
    """
    if not raw:
        return ""

    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Снять обрамляющий блок кода ``` ... ``` (содержимое сохраняем).
    fenced = _FENCE_EDGE_RE.match(text)
    if fenced:
        text = fenced.group("body").strip()
    # Убрать оставшиеся ограды (например, если блоков было несколько).
    text = _FENCE_ANY_RE.sub("", text).strip()

    # Убрать markdown-выделение, но не трогать обычный текст.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = text.replace("`", "")

    # Убрать обрамляющие кавычки-«ёлочки» и обычные кавычки целиком вокруг текста.
    for left, right in (("\u00ab", "\u00bb"), ('"', '"'), ("\u201c", "\u201d")):
        if len(text) > 2 and text.startswith(left) and text.endswith(right):
            text = text[1:-1].strip()

    # Снять служебные вступления и хвосты (несколько раз, если наложились).
    for _ in range(3):
        new_text = _LEAD_RE.sub("", text, count=1).strip()
        new_text = _TAIL_RE.sub("", new_text, count=1).strip()
        if new_text == text:
            break
        text = new_text

    # Схлопнуть тройные переводы строк и убрать пробелы в конце строк.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = limit_emoji(text)
    return text.strip()


def validate_post(text: str) -> str:
    """Проверить, что пост пригоден для публикации. Вернуть текст."""
    if not text:
        raise BotError("Модель вернула пустой текст")
    if len(text) < MIN_POST_CHARS:
        raise BotError(f"Текст слишком короткий ({len(text)} символов) — похоже на мусор")

    letters = sum(1 for ch in text if ch.isalpha())
    if letters < 30:
        raise BotError("В тексте почти нет букв — публиковать нельзя")

    if len(text) > TELEGRAM_LIMIT:
        raise BotError(
            f"Текст длиннее лимита Telegram ({len(text)} > {TELEGRAM_LIMIT})"
        )

    if len(text) > MAX_POST_CHARS:
        log(f"Текст длиннее желаемого ({len(text)}), аккуратно обрежу до предложения")
        text = _truncate_to_sentence(text, MAX_POST_CHARS).strip()
        if len(text) < MIN_POST_CHARS:
            raise BotError("После обрезки текст стал слишком коротким")

    if not text:
        raise BotError("После очистки текст пуст")
    return text


def _truncate_to_sentence(text: str, limit: int) -> str:
    """Обрезать текст по границе предложения, не разрывая слово."""
    window = text[:limit]
    for separator in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        index = window.rfind(separator)
        if index >= MIN_POST_CHARS:
            return window[: index + 1]
    cut = window.rfind(" ")
    return window[:cut] if cut >= MIN_POST_CHARS else window


# --------------------------------------------------------------------------
# Дедупликация
# --------------------------------------------------------------------------

def normalize_for_hash(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def post_hash(text: str) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()[:32]


def shingles(text: str, size: int = 4) -> set[str]:
    """Набор словесных n-грамм для оценки схожести двух постов."""
    words = re.findall(r"[a-zа-яё0-9]+", normalize_for_hash(text))
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def similarity(left: str, right: str) -> float:
    """Коэффициент Жаккара по словесным шинглам."""
    a, b = shingles(left), shingles(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicate(text: str, recent: list[dict]) -> dict | None:
    """Вернуть запись о похожем недавнем посте, если он есть."""
    digest = post_hash(text)
    for entry in recent:
        if not isinstance(entry, dict):
            continue
        if entry.get("hash") == digest:
            return entry
        previous = entry.get("text")
        if isinstance(previous, str) and similarity(text, previous) >= DEDUP_THRESHOLD:
            return entry
    return None


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"recent": []}
    except (OSError, json.JSONDecodeError) as exc:
        log(f"Не удалось прочитать состояние ({exc}); начинаю с пустой истории")
        return {"recent": []}

    if not isinstance(data, dict) or not isinstance(data.get("recent"), list):
        return {"recent": []}
    return data


def save_state(path: str, state: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def remember(state: dict, text: str, provider: str, model: str) -> None:
    entry = {
        "hash": post_hash(text),
        "text": text[:500],
        "provider": provider,
        "model": model,
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    recent = [item for item in state.get("recent", []) if isinstance(item, dict)]
    recent.insert(0, entry)
    state["recent"] = recent[:RECENT_KEEP]


# --------------------------------------------------------------------------
# Провайдеры ИИ
# --------------------------------------------------------------------------

def build_payload(model: str, extra_note: str = "") -> dict:
    user_prompt = USER_PROMPT + extra_note
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 1.0,
        "max_tokens": 700,
    }


def extract_text(response: dict) -> str:
    """Достать текст из ответа в формате OpenAI chat.completions."""
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or "error"
        raise ProviderError(f"API вернул ошибку {code}: {_short(error.get('message', ''), 300)}")

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        keys = ", ".join(sorted(response.keys())) or "нет полей"
        raise ProviderError(f"В ответе нет choices (поля: {keys})", retryable=False)

    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError("Некорректный элемент choices", retryable=False)
    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderError("В ответе нет message", retryable=False)

    content = message.get("content")
    if isinstance(content, list):
        # Некоторые провайдеры отдают content как список частей.
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        finish = first.get("finish_reason") or message.get("finish_reason") or "?"
        raise ProviderError(
            f"Модель вернула пустой текст (finish_reason={finish})", retryable=False
        )
    return content


class Endpoint:
    """Один роутер бесплатных моделей.

    `key_env` — какие переменные окружения искать для ключа. Пустой кортеж
    означает, что роутер работает без ключа (LLM7, Pollinations).
    """

    def __init__(
        self,
        name: str,
        url: str,
        models: tuple[str, ...],
        key_env: tuple[str, ...] = (),
        model_env: str = "",
        optional: bool = False,
    ) -> None:
        self.name = name
        self.url = url
        self.models = models
        self.key_env = key_env
        self.model_env = model_env
        self.optional = optional

    def api_key(self) -> str:
        for name in self.key_env:
            value = (os.getenv(name) or "").strip()
            if value:
                return value
        return ""

    def available(self) -> bool:
        """Можно ли пробовать этот роутер.

        Резервные (optional) роутеры требуют ключ. Основные бесплатные
        роутеры работают всегда.
        """
        if not self.key_env:
            return True
        return bool(self.api_key())

    def model_list(self) -> list[str]:
        if self.model_env:
            override = (os.getenv(self.model_env) or "").strip()
            if override:
                return [item.strip() for item in override.split(",") if item.strip()]
        return list(self.models)

    def headers(self) -> dict[str, str]:
        key = self.api_key()
        if not key:
            return {}
        headers = {"Authorization": f"Bearer {key}"}
        if "generativelanguage.googleapis.com" in self.url:
            # Ключ заголовком, а не в ?key= — иначе он попадёт в логи URL.
            headers["x-goog-api-key"] = key
        return headers


PROVIDERS = (
    Endpoint("LLM7", LLM7_URL, LLM7_MODELS, model_env="AI_MODEL"),
    Endpoint("Pollinations", POLLINATIONS_URL, POLLINATIONS_MODELS),
    Endpoint("OrcaRouter", ORCA_URL, ORCA_MODELS, key_env=("ORCA_API_KEY",),
             model_env="ORCA_MODEL", optional=True),
    Endpoint("Gemini", GEMINI_URL, GEMINI_MODELS,
             key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"), optional=True),
)


def _sleep_plan(started: float) -> list[int]:
    """Сколько секунд ждать между попытками, укладываясь в бюджет генерации."""
    left = GENERATION_BUDGET_SECONDS - (time.monotonic() - started)
    if left <= 1:
        return []
    return [delay for delay in RETRY_DELAYS if delay <= left]


def _remaining(started: float) -> float:
    return GENERATION_BUDGET_SECONDS - (time.monotonic() - started)


def generate_from(provider: Endpoint, note: str) -> tuple[str, str]:
    """Перебрать модели одного роутера. Вернуть (текст, имя модели)."""
    headers = provider.headers()
    last_error: Exception | None = None
    started = time.monotonic()

    for model in provider.model_list():
        # Ждать по Retry-After разрешено не дольше бюджета.
        waits = _sleep_plan(started)
        attempt = 0
        while True:
            try:
                response = http_json(provider.url, build_payload(model, note), headers)
                return extract_text(response), model
            except ProviderError as exc:
                last_error = exc
                log(f"{provider.name}/{model}: ошибка — {exc}")

                if not exc.retryable:
                    break  # модель недоступна — сразу берём следующую

                # API может сам сказать, сколько ждать (HTTP 429).
                if exc.retry_after is not None:
                    if exc.retry_after > _remaining(started):
                        log(
                            f"{provider.name}/{model}: просят подождать "
                            f"{exc.retry_after:.0f} с — это дольше бюджета, беру другую модель"
                        )
                        break
                    log(f"{provider.name}/{model}: жду {exc.retry_after:.0f} с (просит API)")
                    time.sleep(exc.retry_after)
                    continue

                if attempt < len(waits):
                    delay = waits[attempt]
                    attempt += 1
                    log(f"{provider.name}/{model}: пауза {delay} с перед повтором {attempt}")
                    time.sleep(delay)
                    continue
                break

    raise ProviderError(f"{provider.name}: ни одна модель не ответила. Последняя ошибка: {last_error}")


def provider_order() -> list[Endpoint]:
    """Какие роутеры пробовать и в каком порядке."""
    requested = (os.getenv("AI_PROVIDER") or "auto").strip().lower()
    named = {p.name.lower(): p for p in PROVIDERS}

    if requested in ("llm7", "pollinations", "orcarouter", "orca", "gemini", "google"):
        alias = {"orca": "orcarouter", "google": "gemini"}.get(requested, requested)
        chosen = named[alias]
        return [chosen]

    if requested not in ("auto", ""):
        log(f"Неизвестный AI_PROVIDER={requested!r}; использую auto")

    # auto: все доступные роутеры по порядку приоритета.
    return [p for p in PROVIDERS if p.available()]


def create_post(recent: list[dict], enforce_dedup: bool = True) -> tuple[str, str, str]:
    """Сгенерировать пост. Вернуть (текст, провайдер, модель).

    `recent` всегда передаётся в промпт, чтобы модель не повторялась.
    Если `enforce_dedup` = False, готовый пост не отбраковывается как
    дубликат (режим "force" из ручного запуска).
    """
    base_note = ""
    custom = (os.getenv("CUSTOM_PROMPT") or "").strip()
    if custom:
        base_note += f"\n\nДополнительное пожелание от владельца канала: {custom}"
    examples = [
        str(item.get("text", ""))[:200]
        for item in recent[:3]
        if isinstance(item, dict) and item.get("text")
    ]
    if examples:
        base_note += (
            "\n\nНе повторяй темы и формулировки этих недавних постов:\n- "
            + "\n- ".join(examples)
        )

    errors: list[str] = []
    for provider in provider_order():
        note = base_note
        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            try:
                log(f"Генерирую пост через {provider.name} (попытка {attempt})")
                raw, model = generate_from(provider, note)
            except ProviderError as exc:
                errors.append(f"{provider.name}: {exc}")
                break  # этот роутер исчерпан — переходим к следующему

            try:
                text = validate_post(clean_post(raw))
            except BotError as exc:
                log(f"Ответ отклонён: {exc}")
                errors.append(f"{provider.name}: {exc}")
                note = base_note + (
                    "\n\nПредыдущий ответ не подошёл: нужен обычный текст поста "
                    "без markdown, пояснений и служебных фраз."
                )
                continue

            if enforce_dedup:
                duplicate = find_duplicate(text, recent)
                if duplicate:
                    log(
                        "Пост похож на опубликованный "
                        f"{duplicate.get('posted_at', '?')} — прошу другой"
                    )
                    errors.append(f"{provider.name}: дубликат недавнего поста")
                    note = base_note + (
                        "\n\nЭтот вариант повторяет прошлый пост. "
                        "Напиши на совершенно другую тему."
                    )
                    continue

            return text, provider.name, model

    raise BotError("Не удалось получить пост. Причины: " + "; ".join(errors[-6:]))


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram_call(bot_token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    response = http_json(url, payload)
    if not response.get("ok"):
        description = _short(response.get("description", "неизвестная ошибка"), 300)
        code = response.get("error_code", "?")
        raise BotError(f"Telegram API отказал ({code}): {description}")
    return response


def send_to_telegram(bot_token: str, chat_id: str, text: str) -> dict:
    """Отправить пост. Без parse_mode — чтобы спецсимволы не ломали отправку."""
    # Используем только disable_web_page_preview: link_preview_options появился
    # позже и в одном запросе с ним вызывает ошибку "can't use both".
    return telegram_call(
        bot_token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
    )


def resolve_target_chat(bot_token: str) -> str:
    """Определить чат для публикации.

    Если задан TELEGRAM_TEST_CHAT_ID — используется он (безопасная проверка
    без публикации в реальном канале).
    Иначе берём CHANNEL_ID и проверяем доступ через getChat, чтобы поймать
    опечатку или отсутствие прав бота до отправки поста.
    """
    test_chat = (os.getenv("TELEGRAM_TEST_CHAT_ID") or "").strip()
    if test_chat:
        log("Включён режим тестового чата: публикация пойдёт в TELEGRAM_TEST_CHAT_ID")
        return test_chat

    channel = (os.getenv("CHANNEL_ID") or "").strip()
    if not channel:
        raise BotError("Не задан CHANNEL_ID")

    try:
        info = telegram_call(bot_token, "getChat", {"chat_id": channel})
    except BotError as exc:
        log(f"getChat не удался: {exc}")
        raise BotError(
            "Не удалось получить доступ к CHANNEL_ID. Проверьте, что бот добавлен "
            "в канал как администратор и что CHANNEL_ID указан верно "
            "(например @my_channel или -1001234567890)."
        ) from exc

    result = info.get("result") or {}
    title = result.get("title") or result.get("username") or channel
    log(f"Канал найден: {title} (тип: {result.get('type', '?')})")
    return channel


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    dry_run = _env_flag("DRY_RUN")
    force = _env_flag("SKIP_DEDUP")
    state_path = (os.getenv("STATE_PATH") or "state/posted.json").strip()

    # Историю читаем всегда: она нужна и для промпта, и для проверки дублей.
    state = load_state(state_path)
    recent = [item for item in state.get("recent", []) if isinstance(item, dict)]
    log(f"Загружено недавних постов: {len(recent)}")
    if force:
        log("Режим force: проверка на дубликаты отключена")

    try:
        text, provider, model = create_post(recent, enforce_dedup=not force)
    except BotError as exc:
        log(f"ОШИБКА: {exc}")
        return 1

    log(f"Пост получен от {provider}/{model} ({len(text)} символов)")
    print("-" * 60, flush=True)
    print(text, flush=True)
    print("-" * 60, flush=True)

    if dry_run:
        log("DRY_RUN=1 — в Telegram ничего не отправлено")
        remember(state, text, provider, model)
        save_state(state_path, state)
        log(f"Состояние обновлено: {state_path}")
        return 0

    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not bot_token:
        log("ОШИБКА: не задан TELEGRAM_BOT_TOKEN")
        return 1

    try:
        chat_id = resolve_target_chat(bot_token)
        response = send_to_telegram(bot_token, chat_id, text)
    except BotError as exc:
        log(f"ОШИБКА: {exc}")
        return 1

    message_id = (response.get("result") or {}).get("message_id", "?")
    log(f"Пост опубликован (message_id={message_id})")

    remember(state, text, provider, model)
    save_state(state_path, state)
    log(f"Состояние обновлено: {state_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # pragma: no cover
        log("Прервано пользователем")
        sys.exit(130)
