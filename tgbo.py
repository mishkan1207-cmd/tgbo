
import os
import json
import urllib.request

def post_json(url, data, headers=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))

def main():
    api_key = os.environ["ORCA_API_KEY"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    channel = os.environ["CHANNEL_ID"]

    result = post_json(
        "https://api.orcarouter.ai/v1/chat/completions",
        {
            "model": os.getenv("ORCA_MODEL", "orcarouter/free"),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты ведёшь Telegram-канал без тематики. "
                        "Сам выбирай случайную тему: юмор, мысли, "
                        "абсурд, наблюдения или мини-истории. "
                        "Пиши естественно на русском, 2–5 предложений. "
                        "Верни только текст поста."
                    ),
                },
                {
                    "role": "user",
                    "content": "Придумай новый оригинальный пост.",
                },
            ],
            "temperature": 1,
            "max_tokens": 300,
        },
        {"Authorization": f"Bearer {api_key}"},
    )

    text = result["choices"][0]["message"]["content"].strip()
    if not text:
        raise RuntimeError("Модель вернула пустой текст")

    post_json(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        {"chat_id": channel, "text": text},
    )
    print("Пост опубликован!")

if __name__ == "__main__":
    main()
  
