#!/usr/bin/env python3
"""AI News Notifier — hourly check, sends ready-to-post Zerocoder messages to Telegram."""

import json
import os
import random
import re
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
    "OpenAI", "Anthropic", "Siri", "Apple Intelligence", "Google AI",
    "Microsoft AI", "Llama", "Mistral", "Grok", "xAI",
    "WWDC", "GPT-4", "GPT-5", "Copilot", "neural network",
]

HIGH_VALUE = {"chatgpt", "claude", "gpt", "llm", "gemini", "midjourney",
              "no-code", "nocode", "нейросеть", "нейросети", "автоматизация", "agent",
              "openai", "anthropic", "llama", "mistral", "grok", "sora", "copilot"}


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


def clean_text(text: str) -> str:
    # Keep only chars in these Unicode blocks: ASCII, Cyrillic, punctuation, emoji
    def is_allowed(c: str) -> bool:
        cp = ord(c)
        return (
            0x0020 <= cp <= 0x007E  # ASCII printable
            or 0x0400 <= cp <= 0x04FF  # Cyrillic
            or 0x2000 <= cp <= 0x27BF  # General punctuation + symbols
            or 0x1F000 <= cp <= 0x1FAFF  # Emoji
            or c in "\n\r\t—–«»…"  # em-dash, quotes, ellipsis
        )
    text = "".join(c for c in text if is_allowed(c))
    # Fix fused uppercase: "использоватьИИ" -> "использовать ИИ"
    text = re.sub(r"([a-zа-яё])([A-ZА-ЯЁ])", r"\1 \2", text)
    # Remove mixed-script words: "specialistам", "developerов" etc.
    text = re.sub(r"\b[a-zA-Z]{3,}[а-яёА-ЯЁ]+\b", "", text)
    text = re.sub(r"\b[а-яёА-ЯЁ]+[a-zA-Z]{3,}\b", "", text)
    # Fix brand names the model splits
    fixes = {"Open AI": "OpenAI", "Chat GPT": "ChatGPT", "Mid Journey": "Midjourney",
             "You Tube": "YouTube", "Git Hub": "GitHub"}
    for wrong, right in fixes.items():
        text = text.replace(wrong, right)
    return re.sub(r" +", " ", text).strip()


_ENGAGEMENT_STYLES = [
    "конкретный вопрос про последствия или мнение — подходит для любых новостей (например: «как думаете, это изменит расстановку сил?», «стоит ли ждать от них чего-то большего?»)",
    "провокационный тезис без вопроса — подходит для громких анонсов и запусков (например: «мне кажется, это меняет правила игры» или «похоже, эра X только начинается»)",
    "личное сомнение + приглашение поспорить — подходит для неоднозначных решений (например: «честно говоря, не уверена, что зайдёт массово — или я ошибаюсь?»)",
    "вопрос про личный опыт с инструментом — только если статья про конкретный инструмент или фичу, не про бизнес-новость (например: «вы уже пробовали?», «как это меняет ваш процесс?»)",
]


def _engagement_label() -> str:
    return random.choice(_ENGAGEMENT_STYLES)


def generate_post(article: dict) -> str:
    """Generate a ready-to-post Zerocoder channel message."""
    api_key = os.getenv("GROQ_API_KEY")
    fallback = (
        f'<b>{article["title"]}</b>\n\n'
        f'<i>Источник: {article["source"]}</i>\n\n'
        f'\U0001f517 <a href="{article["url"]}">Читать →</a>'
    )
    if not api_key:
        return fallback

    prompt = (
        "Ты — редактор Telegram-канала Zerocoder об ИИ и no-code. "
        "Пишешь от первого лица.\n\n"
        "Напиши пост для канала на основе статьи ниже.\n\n"
        "Стиль:\n"
        "- От первого лица: «я», «нашла», «мне кажется», «обратила внимание»\n"
        "- Тёплый и живой — как будто делишься находкой с коллегой, которому доверяешь\n"
        "- Умеренный энтузиазм: не «я просто в восторге!!!!», а «это действительно интересно, потому что...»\n"
        "- Ровно 2 предложения о сути — одна конкретная мысль, зачем это знать специалисту по ИИ или no-code\n"
        "- Начни с эмодзи по теме: \U0001f4a1 \U0001f680 \U0001f50d \U0001f4bb \U0001f9e0 \U0001f4ca ⚡ \U0001f6e0 \U0001f310 — "
        "не используй \U0001f916\n"
        f"- Последняя строка — приём вовлечения «{_engagement_label()}». "
        "Одна строка, строго один приём, не комбинировать.\n"
        "- Итого: 3 строки максимум. Никакой воды.\n\n"
        "Только текст поста. Никаких заголовков, подписей, пояснений.\n\n"
        f"Источник: {article['source']}\n"
        f"Заголовок: {article['title']}\n"
        f"Содержание: {article['summary']}"
    )

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": "Пиши исключительно на русском языке. Никогда не смешивай латиницу и кириллицу в одном слове: 'specialistам' — грубая ошибка, пиши 'специалистам'. Латиница допустима только в именах собственных (OpenAI, Anthropic, ChatGPT) и аббревиатурах (AI, IPO)."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=350,
            temperature=0.7,
        )
        post_text = clean_text(response.choices[0].message.content)
        return f'{post_text}\n\n\U0001f517 <a href="{article["url"]}">Читать →</a>'
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
    articles = fetch_recent(hours=2)
    relevant = [
        a for a in articles
        if any(kw.lower() in (a["title"] + " " + a["summary"]).lower() for kw in KEYWORDS)
    ]
    hot = [a for a in relevant if score_article(a) >= 2]

    print(f"Новых статей за 2ч: {len(articles)}, релевантных: {len(relevant)}, горячих (2+): {len(hot)}")

    for article in hot:
        post = generate_post(article)
        if send_to_telegram(post):
            print(f"Отправлено: {article['title']}")

    if not hot:
        print("Новых важных статей нет.")


if __name__ == "__main__":
    main()
