#!/usr/bin/env python3
"""AI News Monitor — автоматический дайджест ИИ-новостей для контент-менеджеров."""

import os
import sys
import feedparser
from google import genai
from datetime import datetime, timedelta, timezone
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from dotenv import load_dotenv

load_dotenv()

FEEDS = [
    {"name": "Zerocoder",  "url": "https://ya.zerocoder.ru/feed/"},
    {"name": "ZDNet",      "url": "https://www.zdnet.com/news/rss.xml"},
    {"name": "Forbes AI",  "url": "https://www.forbes.com/ai/feed2.xml"},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
]

KEYWORDS = [
    "ИИ", "нейросеть", "нейросети", "no-code", "nocode", "автоматизация",
    "ChatGPT", "Claude", "Midjourney", "Gemini", "GPT", "LLM",
    "AI", "artificial intelligence", "machine learning", "automation",
    "агент", "agent", "workflow",
]

# High-value keywords bump the score by 2; others by 1
HIGH_VALUE = {"chatgpt", "claude", "gpt", "llm", "gemini", "midjourney",
              "no-code", "nocode", "нейросеть", "нейросети", "автоматизация", "agent"}

console = Console()


def fetch_articles(hours: int = 24) -> list[dict]:
    """Fetch articles from all feeds published in the last N hours."""
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
    """Keep only articles that contain at least one keyword."""
    result = []
    for a in articles:
        text = (a["title"] + " " + a["summary"]).lower()
        if any(kw.lower() in text for kw in KEYWORDS):
            result.append(a)
    return result


def score_article(article: dict) -> int:
    """Score 1–5 based on keyword frequency; used as fallback without API key."""
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0
    for kw in KEYWORDS:
        if kw.lower() in text:
            score += 2 if kw.lower() in HIGH_VALUE else 1
    return min(5, max(1, score))


def smart_summary(title: str) -> str:
    """Return first 85 chars of title as a short description."""
    return title[:85]


def analyze_local(articles: list[dict]) -> str:
    """Keyword-based scoring — instant fallback, no network needed."""
    lines = []
    for i, a in enumerate(articles, 1):
        stars = score_article(a)
        lines.append(f"[{i}] {'⭐' * stars} {stars}/5 | О чём: {smart_summary(a['title'])}")
    return "\n".join(lines)


def analyze_with_ai(articles: list[dict]) -> tuple[str, bool]:
    """Rate articles using free Google Gemini API; falls back to keyword scoring."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        console.print("[dim]Подсказка: добавь GEMINI_API_KEY в .env для AI-анализа (бесплатно на aistudio.google.com)[/dim]")
        return analyze_local(articles), False

    numbered = "\n\n".join(
        f"[{i+1}] {a['source']}: {a['title']}\n{a['summary']}"
        for i, a in enumerate(articles)
    )
    prompt = f"""Ты — ассистент контент-менеджера Telegram-канала об ИИ и no-code.

Оцени статьи по релевантности для аудитории, которой интересны:
— новые ИИ-инструменты и нейросети
— no-code платформы и автоматизация
— практические кейсы применения ИИ

Для каждой статьи ответь строго в формате:
[N] ⭐ X/5 | О чём: <одна строка, максимум 90 символов>

Статьи:
{numbered}"""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text, True
    except Exception as e:
        console.print(f"[yellow]Gemini недоступен ({e}), использую авто-скоринг[/yellow]")
        return analyze_local(articles), False


def print_digest(articles: list[dict], analysis: str, used_claude: bool) -> None:
    """Print the final digest to the terminal."""
    date_str = datetime.now().strftime("%d.%m.%Y")
    console.print()
    console.print(Panel(
        f"[bold white]📰 Дайджест ИИ-новостей — {date_str}[/bold white]",
        border_style="blue",
        expand=False,
    ))

    label = "[bold yellow]Оценка релевантности (Gemini AI — бесплатно):[/bold yellow]" if used_claude \
        else "[bold yellow]Оценка релевантности (авто-скоринг по ключевым словам):[/bold yellow]"
    console.print(f"\n{label}")
    console.print(analysis)

    console.print("\n[bold yellow]Ссылки на статьи:[/bold yellow]")
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
    table.add_column("#", width=3)
    table.add_column("Источник", width=14)
    table.add_column("Заголовок", width=55)
    table.add_column("Ссылка")
    for i, a in enumerate(articles, 1):
        table.add_row(str(i), a["source"], a["title"][:55], a["url"])
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
    analysis, used_claude = analyze_with_ai(relevant[:10])

    print_digest(relevant[:10], analysis, used_claude)

    console.print(
        f"\n[dim]Готово. Обработано {min(len(relevant), 10)} из {len(relevant)} релевантных статей.[/dim]"
    )


if __name__ == "__main__":
    main()
