#!/usr/bin/env python3
"""Показывает твой Telegram chat_id — запусти после того как написал боту любое сообщение."""

import json
import urllib.request
import os
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    print("Ошибка: добавь TELEGRAM_BOT_TOKEN в .env")
    exit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
with urllib.request.urlopen(url, timeout=10) as r:
    data = json.loads(r.read())

if not data.get("result"):
    print("Нет сообщений. Напиши своему боту в Telegram любое сообщение и запусти снова.")
else:
    msg = data["result"][-1]
    chat = msg["message"]["chat"]
    print(f"\nТвой chat_id: {chat['id']}")
    print(f"Имя: {chat.get('first_name', '')} {chat.get('last_name', '')}\n")
    print(f"Добавь в .env:")
    print(f"TELEGRAM_CHAT_ID={chat['id']}")
