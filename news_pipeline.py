#!/usr/bin/env python3
"""Shared pipeline helpers for monitor.py, notifier.py, and bot.py: extra
keyless sources, full-text extraction, structured AI scoring, and
deduplication. See docs/superpowers/specs/2026-07-22-news-pipeline-design.md."""

from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

_TRACKING_QUERY_PARAMS = {
    "fbclid", "gclid", "dclid", "igshid", "mc_cid", "mc_eid", "msclkid",
    "ttclid", "twclid", "vero_id", "li_fat_id", "_ga",
}


def normalize_url(url: str) -> str:
    """Normalized identity key for cross-source dedup: lowercase host,
    no 'www.', no trailing slash, no tracking query params, scheme ignored."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    kept_params = sorted(
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_QUERY_PARAMS
    )
    query = urlencode(kept_params)
    return urlunsplit(("https", host, path, query, ""))


def dedup_cross_source(articles: list[dict]) -> list[dict]:
    """Merge articles that point at the same normalized URL. Keeps the
    richer (longer) summary as primary and combines distinct source names
    with ' + '."""
    groups: dict[str, list[dict]] = {}
    for a in articles:
        groups.setdefault(normalize_url(a["url"]), []).append(a)

    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = dict(max(group, key=lambda a: len(a.get("summary", ""))))
        other_sources = [
            a["source"] for a in group
            if a["source"] != primary["source"]
        ]
        if other_sources:
            names = list(dict.fromkeys([primary["source"], *other_sources]))
            primary["source"] = " + ".join(sorted(names))
        merged.append(primary)
    return merged
