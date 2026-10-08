#!/usr/bin/env python3
"""AI News Notifier — каждые 2 часа один пост-дайджест «к этому часу»:
черновик от AI → фактчекинг (сверка с текстами статей + веб-корроборация
через Google News) → один готовый пост в Telegram."""

import html
import json
import os
import random
import re
import socket
import urllib.request
import feedparser
from groq import Groq
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

import news_pipeline

load_dotenv()

# feedparser.parse() has no built-in timeout — a single unresponsive RSS
# source can hang the whole fetch for minutes. This caps every socket
# operation in the process (urllib, feedparser) at 10s.
socket.setdefaulttimeout(10)

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
    {"name": "OpenAI Blog",        "url": "https://openai.com/news/rss.xml"},
    {"name": "Google AI Blog",     "url": "https://blog.google/technology/ai/rss/"},
    {"name": "DeepMind Blog",      "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "HuggingFace Blog",   "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "Simon Willison",     "url": "https://simonwillison.net/atom/everything/"},
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
    "skill", "скил", "скилы", "agent skills",
    "prompt", "промпт", "промты",
    "mcp",
    "lifehack", "лайфхак", "лайфхаки",
]

HIGH_VALUE = {"chatgpt", "claude", "gpt", "llm", "gemini", "midjourney",
              "no-code", "nocode", "нейросеть", "нейросети", "автоматизация", "agent",
              "openai", "anthropic", "llama", "mistral", "grok", "sora", "copilot",
              "вайбкодинг", "vibe coding", "vibecoding",
              "skill", "скил", "скилы", "agent skills",
              "prompt", "промпт", "промты",
              "mcp",
              "lifehack", "лайфхак", "лайфхаки"}

GOOGLE_NEWS_QUERIES = ["вайбкодинг", "Claude Code", "AI coding agent"]


def fetch_recent(hours: int = 1) -> list[dict]:
    articles = []
    for feed_info in FEEDS:
        posts, _ok = news_pipeline.fetch_rss_feed(feed_info["name"], feed_info["url"], hours)
        articles += posts
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


def _is_hot(article: dict) -> bool:
    """Отправляем при обычном пороге score >= 3. Новость, о которой пишут
    2+ источника (buzz от dedup_semantic), — сигнал важности: отправляем
    даже при score 2. Но score 1 не спасает никакой buzz."""
    score = article.get("score", 0)
    if score >= 3:
        return True
    return article.get("buzz", 1) >= 2 and score >= 2


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


_SECTION_MARKERS = ["ЗАГОЛОВОК", "ТЕЛО", "CTA"]


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


# --- Дайджест «к этому часу»: вместо поста на каждую новость — один пост
# каждые 2 часа. Черновик от AI, затем фактчекинг (вердикты сверяются
# с текстом статей + веб-корроборация через Google News), затем отправка.
# Контракт зафиксирован тестами tests/test_notifier_digest.py. ---

DIGEST_MAX_ITEMS = 5


def _parse_digest_json(raw):
    """Extract a {"items": [...], "cta": str} plan from model output. Items
    reference articles by their 1-based n. Returns None on any parse failure."""
    if not raw:
        return None
    text = re.sub(r"```(?:json)?|```", "", str(raw)).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        clean.append(item)
    return {"items": clean, "cta": str(data.get("cta", "")).strip()}


def generate_digest(hot, client=None):
    """Draft digest items from hot articles. With a Groq client — one AI call
    that returns JSON {items: [{n, headline, text}], cta}; items map to
    articles by n. Without a client (or on any failure) — bare titles.
    Returns (items, cta)."""
    fallback_items = [{"headline": a["title"], "text": "", "article_index": i}
                      for i, a in enumerate(hot[:DIGEST_MAX_ITEMS])]
    if not client:
        return fallback_items, ""

    numbered = "\n\n".join(
        f"[{i + 1}] {a['source']}: {a['title']}\n{a.get('summary', '')[:600]}"
        for i, a in enumerate(hot[:DIGEST_MAX_ITEMS])
    )
    prompt = (
        "Ты — редактор Telegram-канала Zerocoder об ИИ и вайбкодинге.\n"
        f"Собери дайджест из этих новостей — максимум {DIGEST_MAX_ITEMS} самых "
        "важных и интересных.\n\n"
        f"{_CONTENT_FORMULA}\n\n{_NATURAL_TEXT_RULES}\n\n"
        "Для каждого пункта: заголовок до 10 слов с эмодзи в конце (из: 💡 🚀 🔍 💻 📊 ⚡ 🛠 🌐 🎯 👀 🤯 — "
        "не используй 🤖 и 🧠) и суть в 1-2 коротких предложениях: конкретный факт "
        "(кто и что сделал) и практический вывод для человека, который занимается ИИ или вайбкодингом.\n"
        "В конце — одна лёгкая концовка-вопрос на одну строку, без нажима.\n\n"
        "Ответ дай СТРОГО одним JSON-объектом, без пояснений:\n"
        '{"items": [{"n": < номер новости из списка>, "headline": <заголовок>, "text": <1-2 предложения>}], '
        '"cta": <концовка-вопрос>}\n\n'
        f"{numbered}"
    )
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "Пиши исключительно на русском языке. Никогда не смешивай латиницу и кириллицу в одном слове. Отвечай СТРОГО валидным JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
            reasoning_effort="low",
            temperature=0.7,
        )
        plan = _parse_digest_json(response.choices[0].message.content)
    except Exception:
        plan = None

    if not plan or not plan["items"]:
        return fallback_items, ""

    items = []
    for item in plan["items"]:
        n = item.get("n")
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not (1 <= int(n) <= len(hot)):
            continue
        headline = clean_text(str(item.get("headline", "")))
        if not headline:
            continue
        items.append({
            "headline": headline,
            "text": clean_text(str(item.get("text", ""))),
            "article_index": int(n) - 1,
        })
    return items or fallback_items, plan["cta"]


def _parse_factcheck_json(raw):
    """Extract a fact-check list: [{n, verdict, text, query}]. Verdicts are
    confined to ok/uncertain/wrong. Returns None on any parse failure."""
    if not raw:
        return None
    text = re.sub(r"```(?:json)?|```", "", str(raw)).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    checks = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        n = entry.get("n")
        verdict = str(entry.get("verdict", "")).lower()
        if verdict not in {"ok", "uncertain", "wrong"} or not isinstance(n, (int, float)):
            continue
        checks.append({
            "n": int(n),
            "verdict": verdict,
            "text": str(entry.get("text", "")).strip(),
            "query": str(entry.get("query", "")).strip(),
        })
    return checks


def _apply_verdicts(items, checks):
    """Merge fact-check verdicts into digest items: drop wrong, replace text
    of uncertain with the corrected one, attach the web-verification query."""
    by_n = {c["n"]: c for c in checks}
    result = []
    for idx, item in enumerate(items):
        check = by_n.get(idx + 1)
        if check is None:
            result.append(item)
            continue
        if check["verdict"] == "wrong":
            continue
        kept = dict(item)
        if check["verdict"] == "uncertain" or (check["verdict"] == "ok" and check["text"]):
            kept["text"] = clean_text(check["text"])
        kept["query"] = check["query"]
        result.append(kept)
    return result


def _publishers_from_google_news(articles):
    """Publisher names from Google News titles: 'Article title - Publisher'.
    Returns the set of distinct publishers."""
    publishers = set()
    for a in articles:
        title = a.get("title", "")
        if " - " in title:
            publishers.add(title.rsplit(" - ", 1)[1].strip())
    return publishers


def _corroborate_digest(items):
    """Web corroboration: for each item with a search query, count distinct
    Google News publishers covering it in the last 24h. Silence is NOT a
    blocker — the flag is informational. Never raises."""
    for item in items:
        item["corroborated"] = False
        query = item.get("query")
        if not query:
            continue
        try:
            found = news_pipeline.fetch_google_news([query], hours=24)
            item["corroborated"] = len(_publishers_from_google_news(found)) >= 2
        except Exception:
            item["corroborated"] = False
    return items


def fact_check_digest(items, hot, client=None):
    """Full fact-check pass: verdicts of every item's claims against the
    article's full text (AI), web corroboration via Google News (keyless).
    Wrong-fact items are dropped before sending."""
    if not items:
        return items
    if not client:
        return _corroborate_digest(
            [dict(item, query="") for item in items])

    numbered = []
    for idx, item in enumerate(items[:DIGEST_MAX_ITEMS]):
        article = hot[item["article_index"]] if 0 <= item["article_index"] < len(hot) else None
        full_text = (article or {}).get("_full_text") or (article or {}).get("summary", "")
        numbered.append(
            f"ПУНКТ {idx + 1}:\n{item['headline']}\n{item['text']}\n"
            f"Текст статьи: {full_text[:1800]}\n"
            f"(оригинал: {(article or {}).get('url', '')})"
        )
    prompt = (
        "Ты — фактчекер Telegram-канала. Для каждого пункта дайджеста сверь "
        "ВСЁ, что в нём утверждается, с текстом статьи:\n"
        "- вердикт «ok»: факты совпадают с текстом статьи.\n"
        "- «uncertain»: часть фактов не подтверждается текстом статьи — перепиши "
        "text так, чтобы остались ТОЛЬКО подтверждённые факты (без домыслов).\n"
        "- «wrong»: ключевой факт статьи противоречит пункту — пункт нельзя отправлять.\n"
        "Также предложи query: короткий поисковый запрос (2-4 слова, ключевые сущности "
        "пункта, имена на языке статьи) чтобы проверить, о том же ли пишут другие СМИ.\n"
        "Ответ дай СТРОГО JSON-массивом, без пояснений:\n"
        '[{"n": <номер пункта>, "verdict": "ok" | "uncertain" | "wrong", '
        '"text": <исправленный текст пункта или пусто>, "query": <поисковый запрос>}]\n\n'
        + "\n\n".join(numbered)
    )
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "Отвечай СТРОГО валидным JSON-массивом, без пояснений."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
            reasoning_effort="low",
        )
        checks = _parse_factcheck_json(response.choices[0].message.content)
    except Exception:
        checks = None

    if not checks:
        items = [dict(item, query="") for item in items]
    else:
        items = _apply_verdicts(items, checks)
    return _corroborate_digest(items)


def assemble_digest_post(items, cta):
    """Build the final Telegram HTML post from checked digest items."""
    parts = ["<b>\U0001f4ca Дайджест к этому часу</b>"]
    for item in items:
        url = item.get("url", "")
        entry = f"<b>{html.escape(item['headline'])}</b>"
        if item.get("text"):
            entry += f"\n{html.escape(item['text'])}"
        if url:
            label = "📎 Оригинальный пост" if item.get("source", "").startswith("X:") \
                else "🔗 Источник"
            entry += f'\n{label}: <a href="{html.escape(url, quote=True)}">{url}</a>'
        parts.append(entry)
    if cta:
        parts.append(cta)
    return "\n\n".join(parts)


def generate_post(article: dict) -> str:
    """Generate a ready-to-post Zerocoder channel message."""
    api_key = os.getenv("GROQ_API_KEY")
    fallback = (
        f'<b>{html.escape(article["title"])}</b>\n\n'
        f'<i>Источник: {html.escape(article["source"])}</i>\n\n'
        f'\U0001f517 <a href="{article["url"]}">Читать →</a>'
    )
    if not api_key:
        return fallback

    cta_label = _engagement_labels(1)[0]

    prompt = (
        "Ты — редактор Telegram-канала Zerocoder об ИИ и вайбкодинге.\n\n"
        "Напиши пост для канала на основе статьи ниже.\n\n"
        f"{_CONTENT_FORMULA}\n\n"
        f"{_NATURAL_TEXT_RULES}\n\n"
        "Ответ дай СТРОГО в этом формате, с этими маркерами в начале строки, ничего от себя не добавляй:\n\n"
        "ЗАГОЛОВОК: <заголовок — до 10 слов, конкретный неожиданный результат или эффект открытия "
        "(«так можно было?!»), эмодзи в конце (выбери из: 💡 🚀 🔍 💻 📊 ⚡ 🛠 🌐 🎯 👀 🤯 — не используй 🤖 и 🧠)>\n"
        "ТЕЛО: <3-4 коротких абзаца (1-3 строки каждый), разделённых пустой строкой:\n"
        "  - кто и что сделал, конкретно (имена, компании, инструменты) — обязателен факт, не только мнение\n"
        "  - если в статье есть проблема и то, как её решили — расскажи по шагам, обычными словами\n"
        "  - что это значит для человека, который занимается ИИ или вайбкодингом — конкретный практический вывод\n"
        "  - можно закончить лёгкой деталью или цитатой из статьи с эмодзи, если это к месту>\n"
        f"CTA: <концовка в духе «{cta_label}», одна строка, без нажима>\n\n"
        "НЕ пиши ссылку и не пиши «Читать далее» — она добавится отдельно кодом.\n\n"
        f"Источник: {article['source']}\n"
        f"Заголовок статьи: {article['title']}\n"
        f"Содержание: {article['summary']}"
    )

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "Пиши исключительно на русском языке. Никогда не смешивай латиницу и кириллицу в одном слове: 'specialistам' — грубая ошибка, пиши 'специалистам'. Латиница допустима только в именах собственных (OpenAI, Anthropic, ChatGPT) и аббревиатурах (AI, IPO). Пиши живым естественным языком, как для друга, без ИИ-штампов и канцелярита — избегай слов «революционный», «трансформационный», «раскрыть потенциал», «оптимизировать», «инновационный», «прорывной»."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
            reasoning_effort="low",
            temperature=0.7,
        )
        raw = clean_text(response.choices[0].message.content)
        sections = _parse_sections(raw)
        headline = sections.get("ЗАГОЛОВОК", "").strip()
        body = sections.get("ТЕЛО", "").strip()
        cta = sections.get("CTA", "").strip()

        if not headline or not cta:
            return fallback

        is_social = article["source"].startswith("X:")
        link_label = "\U0001f4ce Оригинальный пост" if is_social else "\U0001f517 Источник"
        link_line = f'{link_label}: {article["url"]}'

        parts = [f"<b>{html.escape(headline)}</b>", link_line]
        if body:
            parts.append(body)
        parts.append(cta)
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
    articles += news_pipeline.fetch_hackernews(hours=24)
    articles += news_pipeline.fetch_github_trending(hours=24)
    articles += news_pipeline.fetch_google_news(GOOGLE_NEWS_QUERIES, hours=24)
    articles += news_pipeline.fetch_reddit(hours=24)
    articles += news_pipeline.fetch_telegram_channels(hours=24)
    articles = news_pipeline.dedup_cross_source(articles)

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
    # Cap the expensive full-text+AI pass to the top keyword-scored candidates.
    candidates = sorted(relevant, key=score_article, reverse=True)[:15]

    api_key = os.getenv("GROQ_API_KEY")
    if api_key:
        client = Groq(api_key=api_key)
        for a in candidates:
            full_text = news_pipeline.extract_full_text(a["url"])
            # Полный текст пригодится фактчекеру — оставляем на статье.
            a["_full_text"] = full_text
            result = news_pipeline.score_article_ai(a, client, full_text=full_text)
            if result:
                a["score"] = result.score
                a["verdict"] = result.summary
            else:
                a["score"] = score_article(a)
        candidates = news_pipeline.dedup_semantic(candidates, client)
    else:
        for a in candidates:
            a["score"] = score_article(a)

    hot = sorted(
        [a for a in candidates if _is_hot(a)],
        key=lambda a: (a.get("buzz", 1), a["score"]),
        reverse=True,
    )[:DIGEST_MAX_ITEMS]

    print(f"Новых за 24ч: {len(unique)}, релевантных: {len(relevant)}, горячих: {len(hot)}")

    if not hot:
        print("Новых важных статей нет.")
        return

    client = Groq(api_key=api_key) if api_key else None
    items, cta = generate_digest(hot, client)
    items = fact_check_digest(items, hot, client)

    dropped = len(hot) - len(items)
    if dropped:
        print(f"Фактчекинг отбросил пунктов: {dropped}")
    for item in items:
        mark = "✓" if item.get("corroborated") else "?"
        print(f" [{mark}] {item['headline']} → {hot[item['article_index']]['url']}")

    if not items:
        print("Дайджест пуст — после фактчекинга не осталось подтверждённых пунктов.")
        return

    # Прикрепляем к пунктам источник и ссылку перед сборкой поста.
    for item in items:
        a = hot[item["article_index"]]
        item["url"] = a["url"]
        item["source"] = a["source"]

    post = assemble_digest_post(items, cta=cta or None)
    if send_to_telegram(post):
        for item in items:
            save_sent_url(hot[item["article_index"]]["url"], sent_urls)
        print(f"Дайджест отправлен ({len(items)} пункта(ов)).")
    else:
        print("Telegram не принял дайджест — см. ошибку выше.")


if __name__ == "__main__":
    main()
