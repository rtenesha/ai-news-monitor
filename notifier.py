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
    {"name": "X: @aibreakfast",    "url": "https://nitter.net/aibreakfast/rss"},
    {"name": "X: @swyx",           "url": "https://nitter.net/swyx/rss"},
    {"name": "X: @levelsio",       "url": "https://nitter.net/levelsio/rss"},
    {"name": "X: @emollick",       "url": "https://nitter.net/emollick/rss"},
    {"name": "X: @huggingface",    "url": "https://nitter.net/huggingface/rss"},
    {"name": "X: @googledeepmind", "url": "https://nitter.net/googledeepmind/rss"},
    {"name": "X: @openai",         "url": "https://nitter.net/openai/rss"},
    {"name": "X: @anthropicai",    "url": "https://nitter.net/anthropicai/rss"},
    {"name": "X: @claudeai",       "url": "https://nitter.net/claudeai/rss"},
    {"name": "X: @deepseek_ai",    "url": "https://nitter.net/deepseek_ai/rss"},
    {"name": "X: @durov",          "url": "https://nitter.net/durov/rss"},
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
             "You Tube": "YouTube", "Git Hub": "GitHub", "Deep Seek": "DeepSeek",
             "Deep Mind": "DeepMind", "Hugging Face": "HuggingFace"}
    for wrong, right in fixes.items():
        text = text.replace(wrong, right)
    return re.sub(r" +", " ", text).strip()


# Правила естественного текста — убирают ИИ-штампы и канцелярит
_NATURAL_TEXT_RULES = """Правила естественного текста (обязательны для любого стиля):
- Простые слова, короткие предложения. Пиши так, будто говоришь другу.
- Нормально начинать предложение с «и», «но», «так что».
- Никакой воды: убирай лишние прилагательные и наречия, не используй абстракции — только конкретика.
- ЗАПРЕЩЕНО: «давайте погрузимся», «раскрыть потенциал», «игру-меняющий», «революционный», «трансформационный», «использовать потенциал», «оптимизировать», «разблокировать возможности», «инновационный», «лучший в классе», «прорывной».
- Вместо этого: «вот как это работает», «вот что я нашла», «смотри, какая штука», «но есть проблема», «вот почему это важно».
- Не притворяйся восторженным, не дружелюбничай искусственно — говори прямо, как в чате."""

# Формула подачи — из разбора того, что реально заходит аудитории
_CONTENT_FORMULA = """Формула подачи (проверено на реальных постах, что заходит аудитории):
ЗАХОДИТ: конкретный вау-результат «здесь и сейчас» (что уже работает, а не что обещают), эффект открытия
(«так можно было?!»), живая история с деталями, лёгкая подача.
НЕ ЗАХОДИТ: описательный пересказ без вывода («кто-то использует ИИ для...»), пересказ уже всем известного,
тяжёлые абзацы-лекции, пассивный призыв в духе «подписывайтесь» или «делитесь мнением» без конкретного повода."""

# Концовки — лёгкий живой вопрос, без нажима и искусственной поляризации
_ENGAGEMENT_STYLES = [
    "простой вопрос про личный опыт (например: «Кто-то уже пробовал?», «У вас так было?»)",
    "лёгкое сомнение без нажима (например: «Пока не уверена, взлетит ли это. А вы как думаете?»)",
    "вопрос-ставка на будущее (например: «Это станет нормой или так и останется хаком?»)",
    "вопрос про готовность рискнуть — ТОЛЬКО если статья про конкретный инструмент или эксперимент (например: «Вы бы так рискнули?», «Сами бы попробовали провернуть такое?»)",
]


def _engagement_labels(n: int = 3) -> list[str]:
    return random.sample(_ENGAGEMENT_STYLES, k=min(n, len(_ENGAGEMENT_STYLES)))


_SECTION_MARKERS = ["ЗАГОЛОВОК1", "ЗАГОЛОВОК2", "ЗАГОЛОВОК3", "ТЕЛО", "CTA1", "CTA2", "CTA3"]


def _parse_sections(text: str) -> dict[str, str]:
    pattern = "|".join(_SECTION_MARKERS)
    matches = list(re.finditer(rf"(?:{pattern}):", text))
    result = {}
    for i, m in enumerate(matches):
        key = m.group(0)[:-1]
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[key] = text[start:end].strip()
    return result


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

    cta_labels = _engagement_labels(3)

    prompt = (
        "Ты — редактор Telegram-канала Zerocoder об ИИ и вайбкодинге.\n\n"
        "Напиши пост для канала на основе статьи ниже.\n\n"
        f"{_CONTENT_FORMULA}\n\n"
        f"{_NATURAL_TEXT_RULES}\n\n"
        "Ответ дай СТРОГО в этом формате, с этими маркерами в начале строки, ничего от себя не добавляй:\n\n"
        "ЗАГОЛОВОК1: <вариант заголовка — до 10 слов, конкретный неожиданный результат или эффект открытия "
        "(«так можно было?!»), эмодзи в конце (выбери из: 💡 🚀 🔍 💻 📊 ⚡ 🛠 🌐 🎯 👀 🤯 — не используй 🤖 и 🧠)>\n"
        "ЗАГОЛОВОК2: <второй вариант, ДРУГАЯ интонация, не перефразировка первого>\n"
        "ЗАГОЛОВОК3: <третий вариант, ещё одна интонация>\n"
        "ТЕЛО: <3-4 коротких абзаца (1-3 строки каждый), разделённых пустой строкой:\n"
        "  - кто и что сделал, конкретно (имена, компании, инструменты) — обязателен факт, не только мнение\n"
        "  - если в статье есть проблема и то, как её решили — расскажи по шагам, обычными словами\n"
        "  - что это значит для человека, который занимается ИИ или вайбкодингом — конкретный практический вывод\n"
        "  - можно закончить лёгкой деталью или цитатой из статьи с эмодзи, если это к месту>\n"
        f"CTA1: <концовка в духе «{cta_labels[0]}», одна строка, без нажима>\n"
        f"CTA2: <концовка в духе «{cta_labels[1]}», ДРУГАЯ интонация, одна строка>\n"
        f"CTA3: <концовка в духе «{cta_labels[2]}», ещё одна интонация, одна строка>\n\n"
        "Заголовки между собой и CTA между собой должны различаться по интонации, а не быть перефразировкой "
        "друг друга. НЕ пиши ссылку и не пиши «Читать далее» — она добавится отдельно кодом.\n\n"
        f"Источник: {article['source']}\n"
        f"Заголовок статьи: {article['title']}\n"
        f"Содержание: {article['summary']}"
    )

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": "Пиши исключительно на русском языке. Никогда не смешивай латиницу и кириллицу в одном слове: 'specialistам' — грубая ошибка, пиши 'специалистам'. Латиница допустима только в именах собственных (OpenAI, Anthropic, ChatGPT) и аббревиатурах (AI, IPO). Пиши живым естественным языком, как для друга, без ИИ-штампов и канцелярита — избегай слов «революционный», «трансформационный», «раскрыть потенциал», «оптимизировать», «инновационный», «прорывной»."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=700,
            temperature=0.7,
        )
        raw = clean_text(response.choices[0].message.content)
        sections = _parse_sections(raw)
        headlines = [sections.get(f"ЗАГОЛОВОК{i}", "").strip() for i in (1, 2, 3)]
        headlines = [h for h in headlines if h]
        body = sections.get("ТЕЛО", "").strip()
        ctas = [sections.get(f"CTA{i}", "").strip() for i in (1, 2, 3)]
        ctas = [c for c in ctas if c]

        if not headlines or not ctas:
            return fallback

        is_social = article["source"].startswith("X:")
        link_label = "\U0001f4ce Оригинальный пост" if is_social else "\U0001f517 Источник"
        link_line = f'{link_label}: {article["url"]}'

        headline_block = "Варианты заголовка:\n" + "\n".join(
            f"{i}. {h}" for i, h in enumerate(headlines, 1)
        )
        cta_block = "Варианты CTA:\n" + "\n".join(
            f"{i}. {c}" for i, c in enumerate(ctas, 1)
        )

        parts = [headline_block, link_line]
        if body:
            parts.append(body)
        parts.append(cta_block)
        return "\n\n".join(parts)
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
