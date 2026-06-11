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

SENT_URLS_FILE = os.getenv("SENT_URLS_FILE", "sent_urls.txt")


def load_sent_urls() -> set[str]:
    if not os.path.exists(SENT_URLS_FILE):
        return set()
    with open(SENT_URLS_FILE) as f:
        return {line.strip() for line in f if line.strip()}


def save_sent_url(url: str, all_sent: set[str]) -> None:
    all_sent.add(url)
    lines = sorted(all_sent)
    # Обрезаем до 3000 записей чтобы файл не рос бесконечно
    if len(lines) > 3000:
        lines = lines[-3000:]
    with open(SENT_URLS_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


FEEDS = [
    {"name": "Zerocoder",  "url": "https://ya.zerocoder.ru/feed/"},
    {"name": "ZDNet",      "url": "https://www.zdnet.com/news/rss.xml"},
    {"name": "Хабр / ИИ", "url": "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/"},
    {"name": "Хабр / ML", "url": "https://habr.com/ru/rss/hubs/machine_learning/articles/"},
    {"name": "Нейродвиж", "url": "https://rss.app/feeds/uu56qVqY4k9879l4.xml"},
    {"name": "PushEnter", "url": "https://rss.app/feeds/bprrq7ZPdeYnAxa4.xml"},
    {"name": "AI Central","url": "https://rss.app/feeds/FC7W2u2sNL1Qtx0X.xml"},
    {"name": "ИИволюция", "url": "https://rss.app/feeds/avVuy9apZYjuiARE.xml"},
]

KEYWORDS = [
    "ИИ", "нейросеть", "нейросети", "no-code", "nocode", "автоматизация",
    "ChatGPT", "Claude", "Midjourney", "Gemini", "GPT", "LLM",
    "AI", "artificial intelligence", "machine learning", "automation",
    "агент", "agent", "workflow",
    "OpenAI", "Anthropic", "Siri", "Apple Intelligence", "Google AI",
    "Microsoft AI", "Llama", "Mistral", "Grok", "xAI",
    "WWDC", "GPT-4", "GPT-5", "Copilot", "neural network",
    "вайбкодинг", "vibe coding", "vibecoding",
]

HIGH_VALUE = {"chatgpt", "claude", "gpt", "llm", "gemini", "midjourney",
              "no-code", "nocode", "нейросеть", "нейросети", "автоматизация", "agent",
              "openai", "anthropic", "llama", "mistral", "grok", "sora", "copilot",
              "вайбкодинг", "vibe coding", "vibecoding"}


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


def _kw_matches(kw: str, text: str) -> bool:
    return bool(re.search(r'\b' + re.escape(kw.lower()) + r'\b', text))


def score_article(article: dict) -> int:
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in KEYWORDS:
        if _kw_matches(kw, text):
            score += 2 if kw.lower() in HIGH_VALUE else 1
    return min(5, score)


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


# Стиль А — аналитический: факт + инсайт для профессионала
_STYLE_A = """Стиль: аналитический, по делу.
- Открытие: сразу конкретный факт или действие — «OpenAI запустила...», «Вышел новый...», «Anthropic обновила...»
- 2 предложения: что произошло + почему это важно для специалиста по ИИ или вайбкодингу (конкретная польза, не абстрактно)
- Тон: профессиональный, но живой — без канцелярита, с глаголами действия"""

# Стиль Г — личный: от первого лица, тёплый, делишься находкой
_STYLE_G = """Стиль: личный, от первого лица.
- Открытие: личное наблюдение или реакция — «Обратила внимание на...», «Интересный ход от...», «Вот это поворот.», «Пока все обсуждают X, тихо вышло...»
- НЕЛЬЗЯ начинать с «я нашла», «наткнулась на», «нашла интересную статью»
- 2 предложения: делишься мыслью с коллегой которому доверяешь — умеренный энтузиазм, не «я в восторге!!!», а «это интересно потому что...»
- Тон: тёплый и живой, первое лицо («я», «мне кажется», «обратила внимание»)"""

# Концовки — провоцируют комментарий, не просто вопрос ради вопроса
_ENGAGEMENT_STYLES = [
    "острый вопрос с поляризацией — читатель должен захотеть занять позицию (например: «Это прорыв или очередной хайп — как думаете?», «Вы бы доверили это ИИ или пока нет?», «Оправданный риск или нет?»)",
    "провокационный тезис + вызов на спор (например: «Мне кажется, через год это изменит всё — или я преувеличиваю?», «Похоже, старый подход окончательно устарел — согласны?»)",
    "личное сомнение — приглашает успокоить или поспорить (например: «Честно, я ещё не решила, радоваться или беспокоиться. А вы?», «Не уверена, что это взлетит массово — или ошибаюсь?»)",
    "конкретный вопрос про личный опыт — ТОЛЬКО если статья про инструмент или фичу, не про бизнес-новость (например: «Уже пробовали? Как в реальной работе?», «Это меняет ваш процесс или пока нет?»)",
]


def _post_style() -> str:
    return random.choice([_STYLE_A, _STYLE_G])


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
        "Ты — редактор Telegram-канала Zerocoder об ИИ и вайбкодинге.\n\n"
        "Напиши пост для канала на основе статьи ниже.\n\n"
        f"{_post_style()}\n\n"
        "Структура поста:\n"
        "1. Эмодзи по теме (выбери из: 💡 🚀 🔍 💻 📊 ⚡ 🛠 🌐 🎯 👀) — не используй 🤖 и 🧠\n"
        "2. Ровно 2 предложения по сути\n"
        f"3. Концовка — «{_engagement_label()}». "
        "Одна строка, строго один приём, не комбинировать.\n\n"
        "Итого: 3 строки. Никакой воды, никаких заголовков и пояснений — только текст поста.\n\n"
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
    sent_urls = load_sent_urls()

    articles = fetch_recent(hours=24)
    # Дедупликация по URL + фильтр уже отправленных
    seen: set[str] = set()
    unique = []
    for a in articles:
        if a["url"] not in seen and a["url"] not in sent_urls:
            seen.add(a["url"])
            unique.append(a)

    relevant = [
        a for a in unique
        if any(_kw_matches(kw, (a["title"] + " " + a["summary"]).lower()) for kw in KEYWORDS)
    ]
    hot = [a for a in relevant if score_article(a) >= 2]

    # Максимум 7 постов за один запуск чтобы не спамить
    hot = sorted(hot, key=lambda a: score_article(a), reverse=True)[:7]

    print(f"Новых за 24ч: {len(unique)}, релевантных: {len(relevant)}, горячих (2+): {len(hot)}")

    for article in hot:
        post = generate_post(article)
        if send_to_telegram(post):
            save_sent_url(article["url"], sent_urls)
            print(f"Отправлено: {article['title']}")

    if not hot:
        print("Новых важных статей нет.")


if __name__ == "__main__":
    main()
