# AI News Digest

Run the AI news monitor to generate a daily digest of relevant AI and no-code news from configured RSS feeds.

## Usage

```
/digest [hours]
```

- `hours` — how many hours back to look (default: 24)

## What it does

1. Fetches articles from RSS feeds (Zerocoder, ZDNet, Forbes AI, TechCrunch)
2. Filters by AI/no-code keywords
3. Asks Claude to rate each article 1–5 for relevance to a tech/AI content manager
4. Displays a formatted digest with links

## Steps

1. Check that `.env` contains `ANTHROPIC_API_KEY`
2. Run the monitor script with the requested time window:

```bash
python3 monitor.py $ARGUMENTS
```

3. Review the output and highlight any articles rated 4–5 stars as top picks for the content plan.
4. If no articles are found, suggest expanding the time window: `python3 monitor.py 72`
