#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_chat.py — показать chat_id, доступные боту.

Нужен, чтобы отправить тестовый пост в личку, а не в канал.
Токен читается из переменной окружения TELEGRAM_BOT_TOKEN и не печатается.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def call(token: str, method: str, params: str = "") -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}{params}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "description": f"HTTP {exc.code} {body[:150]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("::error::Не задан TELEGRAM_BOT_TOKEN")
        return 1

    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"::error::Токен бота не работает: {me.get('description')}")
        return 1
    bot = me["result"]
    print(f"Бот: @{bot.get('username')} ({bot.get('first_name')})")

    updates = call(token, "getUpdates", "?limit=100")
    if not updates.get("ok"):
        print(f"getUpdates не сработал: {updates.get('description')}")
        return 1

    rows: list[tuple[int, str, str]] = []
    for update in updates.get("result") or []:
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            payload = update.get(key)
            if not isinstance(payload, dict):
                continue
            chat = payload.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            label = (
                chat.get("title")
                or chat.get("username")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or "(без названия)"
            )
            rows.append((int(chat_id), str(chat.get("type", "?")), str(label)))

    print()
    if not rows:
        info = call(token, "getWebhookInfo").get("result") or {}
        print("getUpdates пуст — вы ещё не написали боту в личку.")
        print(f"Вебхук настроен: {bool(info.get('url'))}; ожидает обновлений: {info.get('pending_update_count', 0)}")
        print()
        print("ЧТО СДЕЛАТЬ: откройте бота в Telegram, отправьте ему /start,")
        print("затем запустите этот workflow снова.")
        return 0

    print(f"{'chat_id':>16}  {'тип':<11} название")
    print("-" * 62)
    seen: set[int] = set()
    for chat_id, kind, label in rows:
        if chat_id in seen:
            continue
        seen.add(chat_id)
        hint = ""
        if chat_id > 0:
            hint = "   <- личный чат, годится для теста"
        elif str(chat_id).startswith("-100"):
            hint = "   <- канал / супергруппа"
        print(f"{chat_id:>16}  {kind:<11} {label}{hint}")

    print()
    print("Используйте chat_id личного чата как параметр test_chat_id.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
