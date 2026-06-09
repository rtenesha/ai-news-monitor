#!/usr/bin/env python3
"""AI News Monitor — автоматический дайджест ИИ-новостей для контент-менеджеров."""

import json
import os
import re
import sys
import urllib.request
import feedparser
from groq import Groq
from datetime import datetime, timedelta, timezone
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
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

console = Console()


def fetch_articles(hours: int = 24) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles = []
    for feed_info in FEEDS:
        console.print(f"  Загружаю [cyan]{feed_info['name']}[/cyan]...", end=" ")
        try:
            feed = feedparser.parse(feed_info["url"])
            count = 0
            for entry in feed.entries:
                parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if parsed:
                    pub_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                summary = entry.get("summary", entry.get("description", ""))
                articles.append({
                    "title":     entry.get("title", "Без заголовка"),
                    "url":       entry.get("link", ""),
                    "summary":   summary[:600],
                    "source":    feed_info["name"],
                    "published": pub_dt if parsed else None,
                })
                count += 1
            console.print(f"[green]{count} статей[/green]")
        except Exception as e:
            console.print(f"[red]ошибка: {e}[/red]")
    return articles


def filter_by_keywords(articles: list[dict]) -> list[dict]:
    result = []
    for a in articles:
        text = (a["title"] + " " + a["summary"]).lower()
        if any(kw.lower() in text for kw in KEYWORDS):
            result.append(a)
    return result


def score_article(article: dict) -> int:
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in KEYWORDS:
        if kw.lower() in text:
            score += 2 if kw.lower() in HIGH_VALUE else 1
    return min(5, max(1, score))


def analyze_local(articles: list[dict]) -> list[dict]:
    """Add score field to each article using keyword scoring."""
    for a in articles:
        a["score"] = score_article(a)
        a["verdict"] = a["title"]
    return articles


def analyze_with_ai(articles: list[dict]) -> tuple[list[dict], bool]:
    """Rate articles with Groq/Llama 3; falls back to keyword scoring."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        console.print("[dim]Подсказка: добавь GROQ_API_KEY в .env (бесплатно на console.groq.com)[/dim]")
        return analyze_local(articles), False

    numbered = "\n\n".join(
        f"[{i+1}] {a['source']}: {a['title']}\n{a['summary']}"
        for i, a in enumerate(articles)
    )
    prompt = f"""Ты — опытный русскоязычный редактор Telegram-канала об ИИ и no-code.

Оцени статьи по релевантности для аудитории, которой интересны:
— новые ИИ-инструменты и нейросети
— no-code платформы и автоматизация
— практические кейсы применения ИИ

Для каждой статьи ответь строго в формате (одна строка):
[N] SCORE | SUMMARY

Правила:
- SCORE — целое число от 0 до 5, без звёздочек
- SUMMARY — одно законченное предложение на живом русском языке: не переводи дословно, передавай суть
- Никакого канцелярита: «запустил» вместо «осуществил запуск», конкретные глаголы вместо «является»
- Только русский язык, никакого английского в SUMMARY
- Пример: [1] 4 | OpenAI запустила ИИ-агента для деловой переписки прямо в приложении Messages.

Статьи:
{numbered}"""

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты пишешь исключительно на русском языке. Никаких китайских, японских или других иероглифов — только кириллица, латиница в именах собственных и цифры."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
        )
        raw = response.choices[0].message.content
        # Parse scores back into articles
        for line in raw.splitlines():
            line = line.strip()
            if not line or not line.startswith("["):
                continue
            try:
                idx = int(line[1:line.index("]")]) - 1
                rest = line[line.index("]") + 1:].strip().lstrip("|").strip()
                score_str, _, verdict = rest.partition("|")
                articles[idx]["score"] = min(5, max(0, int(score_str.strip())))
                articles[idx]["verdict"] = re.sub(r'[^ -Ѐ-ӿ -➿\U0001F000-\U0001FAFF]', '', verdict).strip()
            except (ValueError, IndexError):
                continue
        # Fill any articles that weren't parsed
        for a in articles:
            if "score" not in a:
                a["score"] = score_article(a)
                a["verdict"] = a["title"]
        return articles, True
    except Exception as e:
        console.print(f"[yellow]Groq недоступен ({e}), использую авто-скоринг[/yellow]")
        return analyze_local(articles), False


def format_telegram_message(articles: list[dict], hours: int) -> str:
    """Format digest as Telegram HTML message."""
    date_str = datetime.now().strftime("%d.%m.%Y")
    top = [a for a in articles if a.get("score", 0) >= 3]
    top = sorted(top, key=lambda x: x.get("score", 0), reverse=True)

    lines = [f"📰 <b>Дайджест ИИ-новостей — {date_str}</b>"]
    lines.append(f"<i>За последние {hours} ч. | Топ релевантных статей</i>\n")

    if not top:
        lines.append("Сегодня нет статей с оценкой 3+. Попробуй расширить период.")
    else:
        for a in top:
            stars = "⭐" * a["score"]
            verdict = a.get("verdict", a["title"])
            lines.append(f"{stars} <b>{verdict}</b>")
            lines.append(f'<i>{a["source"]}</i> · <a href="{a["url"]}">Читать →</a>\n')

    lines.append(f"<i>Всего найдено релевантных: {len(articles)}</i>")
    return "\n".join(lines)


def send_to_telegram(text: str) -> bool:
    """Send message via Telegram Bot API. Returns True on success."""
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
        console.print(f"[red]Telegram: ошибка отправки — {e}[/red]")
        return False


def print_digest(articles: list[dict], used_ai: bool) -> None:
    date_str = datetime.now().strftime("%d.%m.%Y")
    console.print()
    console.print(Panel(
        f"[bold white]📰 Дайджест ИИ-новостей — {date_str}[/bold white]",
        border_style="blue", expand=False,
    ))
    label = "Llama 3 via Groq — бесплатно" if used_ai else "авто-скоринг по ключевым словам"
    console.print(f"\n[bold yellow]Оценка релевантности ({label}):[/bold yellow]")
    for i, a in enumerate(articles, 1):
        stars = "⭐" * a.get("score", 0)
        verdict = a.get("verdict", a["title"])
        console.print(f"[{i}] {stars} {a.get('score', 0)}/5 | {verdict}")

    console.print("\n[bold yellow]Ссылки:[/bold yellow]")
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("#", width=3)
    table.add_column("Источник", width=14)
    table.add_column("Заголовок", width=50)
    table.add_column("Ссылка")
    for i, a in enumerate(articles, 1):
        table.add_row(str(i), a["source"], a["title"][:50], a["url"])
    console.print(table)


def main() -> None:
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24

    console.print(Panel.fit(
        "[bold blue]AI News Monitor[/bold blue]\n"
        f"Сбор новостей за последние [cyan]{hours}[/cyan] ч.",
        border_style="blue",
    ))

    console.print("\n[bold]1. Загружаю RSS-ленты...[/bold]")
    all_articles = fetch_articles(hours)
    console.print(f"\nВсего найдено: [bold]{len(all_articles)}[/bold] статей")

    console.print("\n[bold]2. Фильтрую по ключевым словам...[/bold]")
    relevant = filter_by_keywords(all_articles)
    console.print(f"Релевантных: [bold green]{len(relevant)}[/bold green]")

    if not relevant:
        console.print("[red]\nНет релевантных статей за указанный период.[/red]")
        console.print("Попробуй увеличить период: [cyan]python3 monitor.py 72[/cyan]")
        return

    console.print("\n[bold]3. Анализирую статьи с помощью AI...[/bold]")
    articles, used_ai = analyze_with_ai(relevant[:10])

    print_digest(articles, used_ai)

    # Send to Telegram if configured
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        console.print("\n[bold]4. Отправляю в Telegram...[/bold]", end=" ")
        msg = format_telegram_message(articles, hours)
        if send_to_telegram(msg):
            console.print("[green]отправлено![/green]")
    else:
        console.print("\n[dim]Telegram не настроен — добавь TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env[/dim]")

    console.print(
        f"\n[dim]Готово. Обработано {len(articles)} из {len(relevant)} релевантных статей.[/dim]"
    )


if __name__ == "__main__":
    main()
