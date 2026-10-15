#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_key.py — проверить, какие бесплатные модели доступны с вашим ключом.

Скрипт НЕ печатает ключ и не публикует ничего в Telegram. Он только отправляет
запрос в API и показывает, какая модель ответила, а какая отвергнута.

Запуск:
    python check_key.py orca      # проверить OrcaRouter (ключ из ORCA_API_KEY)
    python check_key.py gemini    # проверить Gemini (ключ из GEMINI_API_KEY)

Переменные окружения:
    ORCA_API_KEY   / GEMINI_API_KEY  — ключ провайдера
    AI_MODEL                         — свой список моделей через запятую
"""

import json
import os
import sys
import urllib.error
import urllib.request

ORCA_URL = "https://api.orcarouter.ai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

ORCA_MODELS = [
    "deepseek/deepseek-v4-flash-free",
    "z-ai/glm-5.3-flash-free",
    "tencent/hy4-preview-free",
    "tencent/hy3-free",
    "orcarouter/free",
]

GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

PROMPT = "Напиши одну короткую фразу о погоде. Только фразу."


def mask(value: str) -> str:
    """Показать только длину ключа, никогда — сам ключ."""
    if not value:
        return "(не задан)"
    return f"(задан, {len(value)} символов)"


def describe_error(code: int, body: str) -> str:
    """Перевести ответ API в понятное объяснение."""
    text = body.lower()
    if code == 401:
        return "ключ неверный или отозван"
    if code == 403:
        if "free_quota_exhausted" in text:
            return "403: у этой бесплатной модели нет свободной ёмкости (это НЕ проблема ключа)"
        if "err_free_access_denied" in text:
            return "403: нужна привязка GitHub-аккаунта (или пополнение) — модель не подходит"
        if "insufficient_user_quota" in text or "quota" in text:
            return "403: исчерпан лимит/квота аккаунта"
        if "cycle spend limit" in text:
            return "403: у ключа исчерпан лимит расходов за период"
        if "no access to model" in text:
            return "403: модели нет в списке разрешённых для этого ключа"
        return "403: доступ запрещён (см. текст выше)"
    if code == 404:
        return "404: модели нет или id устарел"
    if code == 429:
        return "429: сработал лимит запросов, попробуйте позже"
    if code in (500, 502, 503, 504):
        return f"{code}: временная проблема на стороне провайдера"
    return f"{code}: неожиданный ответ"


def probe(url: str, model: str, headers: dict) -> tuple[bool, str]:
    """Один запрос к API. Возвращает (успех, пояснение)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": 0.7,
        "max_tokens": 60,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        return False, f"{describe_error(exc.code, body)} | {body[:220]}"
    except urllib.error.URLError as exc:
        return False, f"сеть недоступна: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 - нужен любой сбой
        return False, f"{type(exc).__name__}: {exc}"

    choices = data.get("choices") or []
    if not choices:
        return False, f"ответ без choices: {json.dumps(data, ensure_ascii=False)[:220]}"
    content = (choices[0].get("message") or {}).get("content") or ""
    content = content.strip().replace("\n", " ")
    if not content:
        return False, "модель вернула пустой текст"
    return True, content[:160]


def main() -> int:
    provider = (sys.argv[1] if len(sys.argv) > 1 else "orca").strip().lower()
    override = [m.strip() for m in (os.getenv("AI_MODEL") or "").split(",") if m.strip()]

    if provider in ("orca", "orcarouter"):
        key = (os.getenv("ORCA_API_KEY") or "").strip()
        url, models, label = ORCA_URL, (override or ORCA_MODELS), "OrcaRouter"
        headers = {"Authorization": f"Bearer {key}"}
    elif provider in ("gemini", "google"):
        key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        url, models, label = GEMINI_URL, (override or GEMINI_MODELS), "Gemini"
        headers = {"Authorization": f"Bearer {key}", "x-goog-api-key": key}
    else:
        print(f"Неизвестный провайдер: {provider!r}. Используйте orca или gemini.")
        return 2

    print(f"Провайдер : {label}")
    print(f"Ключ      : {mask(key)}")
    print(f"Эндпоинт  : {url}")
    print("-" * 70)

    if not key:
        print("Ключ не задан в переменных окружения — проверять нечего.")
        print(f"Задайте переменную {'ORCA_API_KEY' if provider != 'gemini' else 'GEMINI_API_KEY'} и повторите.")
        return 2

    working = []
    for model in models:
        ok, info = probe(url, model, headers)
        mark = "OK  " if ok else "НЕТ "
        print(f"[{mark}] {model}")
        print(f"        {info}")
        if ok:
            working.append(model)

    print("-" * 70)
    if working:
        print("Рабочие модели (в этом порядке их будет использовать бот):")
        for model in working:
            print(f"  - {model}")
        print()
        print("Если первая в списке отличается от желаемой, задайте переменную")
        print(f"AI_MODEL={','.join(working)}")
        return 0

    print("Ни одна модель не ответила.")
    print("Если везде 401 — ключ неверный. Если 403 free_quota_exhausted —")
    print("у бесплатных моделей сейчас нет ёмкости, попробуйте позже.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
