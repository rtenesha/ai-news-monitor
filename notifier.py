#!/usr/bin/env python3
"""AI News Notifier — hourly check, sends ready-to-post Zerocoder messages to Telegram."""

import json
import os
import urllib.request
import feedparser
from groq import Groq
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

FEEDS = [
    {"name": "Zerocoder",        "url": "https://ya.zerocoder.ru/feed/"},
    {"name": "ZDNet",            "url": "https://www.zdnet.com/news/rss.xml"},
    {"name": "Forbes AI",        "url": "https://www.forbes.com/ai/feed2.xml"},
    {"name": "TechCrunch AI",    "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "OpenAI Blog",      "url": "https://openai.com/blog/rss.xml"},
    {"name": "HuggingFace Blog", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "DeepMind Blog",    "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "Karpathy",         "url": "https://karpathy.substack.com/feed"},
    {"name": "One Useful Thing", "url": "https://www.oneusefulthing.org/feed"},
    {"name": "DAIR AI",          "url": "https://medium.com/feed/dair-ai"},
]

KEYWORDS = [
    "ИИ", "нейросеть", "нейросети", "no-code", "nocode", "автоматизация",
    "ChatGPT", "Claude", "Midjourney", "Gemini", "GPT", "LLM",
    "AI", "artificial intelligence", "machine learning", "automation",
    "агент", "agent", "workflow",
]

HIGH_VALUE = {"chatgpt", "claude", "gpt", "llm", "gemini", "midjourney",
              "no-code", "nocode", "нейросеть", "нейросети", "автоматизация", "agent"}


def fetch_recent(hours: int = 1) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles = []
    for feed_info in FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries:
                parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if not parsed:
                    continue
                pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
                summary = entry.get("summary", entry.get("description", ""))
                articles.append({
                    "title":   entry.get("title", ""),
                    "url":     entry.get("link", ""),
                    "summary": summary[:600],
                    "source":  feed_info["name"],
                })
        except Exception:
            pass
    return articles


def score_article(article: dict) -> int:
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in KEYWORDS:
        if kw.lower() in text:
            score += 2 if kw.lower() in HIGH_VALUE else 1
    return min(5, max(1, score))


def generate_post(article: dict) -> str:
    """Generate a ready-to-post Zerocoder channel message."""
    api_key = os.getenv("GROQ_API_KEY")
    fallback = (
        f'🤖 <b>{article["title"]}</b>\n\n'
        f'<i>Источник: {article["source"]}</i>\n\n'
        f'🔗 <a href="{article["url"]}">Читать →</a>'
    )
    if not api_key:
        return fallback

    prompt = f"""Ты — контент-менеджер Telegram-канала Zerocoder об ИИ и no-code.

Напиши короткий пост для Telegram-канала на основе этой статьи. Требования:
- 2–3 предложения на русском языке
- Объясни суть новости и почему она важна для специалистов в no-code и автоматизации
- Начни с подходящего эмодзи (не 🤖)
- Только текст поста, без заголовков и пояснений

Источник: {article["source"]}
Заголовок: {article["title"]}
Краткое содержание: {article["summary"]}"""

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
        )
        post_text = response.choices[0].message.content.strip()
        return f'{post_text}\n\n🔗 <a href="{article["url"]}">Читать →</a>'
    except Exception:
        return fallback


def send_to_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def main():
    articles = fetch_recent(hours=1)
    relevant = [
        a for a in articles
        if any(kw.lower() in (a["title"] + " " + a["summary"]).lower() for kw in KEYWORDS)
    ]
    hot = [a for a in relevant if score_article(a) >= 3]

    print(f"Новых статей за час: {len(articles)}, релевантных: {len(relevant)}, горячих (3+): {len(hot)}")

    for article in hot:
        post = generate_post(article)
        if send_to_telegram(post):
            print(f"Отправлено: {article['title']}")

    if not hot:
        print("Новых важных статей нет.")


if __name__ == "__main__":
    main()
