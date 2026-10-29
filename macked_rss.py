#!/usr/bin/env python3
"""
Generate an RSS 2.0 feed from https://macked.app, with a short summary for
every post so you don't have to open each link.
"""
import argparse
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime

import requests
from bs4 import BeautifulSoup

BASE = "https://macked.app"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
SKIP_PREFIXES = ("/programs", "/tag/", "/topics/", "/page/", "/faq", "/wishlist",
                 "/app-store-share", "/advertise", "/user-sign", "/oauth", "/cdn-cgi")
NOISE = ("下载", "破解", "激活", "登录", "注册", "评论", "版权", "免责", "Telegram",
         "解决办法", "常见问题", "点击")
SITE_DESC_START = "macked每天更新大量精品"
SUMMARY_MAX = 400
REQUEST_DELAY = 1.0


def fetch(url):
    """Fetch a page. Tries plain requests, then a Chrome-like client, then a reader proxy."""
    last = None
    # 0) optional relay (e.g. a Cloudflare Worker) set via FETCH_PROXY
    proxy = os.environ.get("FETCH_PROXY")
    if proxy:
        try:
            r = requests.get(proxy, params={"url": url}, timeout=60)
            r.encoding = "utf-8"
            if r.status_code == 200 and "<html" in r.text.lower():
                return r.text
            print(f"  relay returned {r.status_code} for {url}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  relay error: {e}", file=sys.stderr)
    # 1) plain request (works from home connections)
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except requests.RequestException as e:
        last = e
    # 2) Chrome-like TLS fingerprint (helps against Cloudflare bot checks)
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome", timeout=30, headers={"Accept-Language": HEADERS["Accept-Language"]})
        if r.status_code == 200 and "<html" in r.text.lower():
            return r.text
    except Exception:
        pass
    # 3) reader proxy that loads the page in a real browser and returns HTML
    try:
        r = requests.get(
            "https://r.jina.ai/" + url,
            headers={"X-Return-Format": "html", "Accept": "text/html"},
            timeout=90,
        )
        r.encoding = "utf-8"
        if r.status_code == 200 and "<h2" in r.text:
            return r.text
    except requests.RequestException:
        pass
    raise last or requests.RequestException("all fetch methods failed")


_translator = None


_cache = {}
_blocked = False


def _gt():
    global _translator
    if _translator is None:
        from deep_translator import GoogleTranslator
        _translator = GoogleTranslator(source="auto", target="en")
    return _translator


def _batch(items):
    """Translate several lines in ONE request (far fewer requests -> no rate limit)."""
    global _blocked
    if _blocked or os.environ.get("NO_GOOGLE"):
        return
    for attempt in range(3):
        try:
            res = _gt().translate("\n".join(items))
            break
        except Exception:
            wait = 30 * (attempt + 1)
            print(f"  rate limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
    else:
        _blocked = True
        print("  Google is still blocking you - stop and try again later.", file=sys.stderr)
        return
    parts = [x.strip() for x in (res or "").split("\n")]
    if len(parts) == len(items):
        _cache.update(zip(items, parts))
    elif len(items) > 1:  # lines got merged: retry in smaller pieces
        mid = len(items) // 2
        _batch(items[:mid])
        _batch(items[mid:])
    time.sleep(3)


def prefetch(texts, batch_chars=3000):
    texts = [t for t in dict.fromkeys(texts) if t and t not in _cache]
    batches, cur, size = [], [], 0
    for t in texts:
        if cur and size + len(t) > batch_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t) + 1
    if cur:
        batches.append(cur)
    for i, b in enumerate(batches, 1):
        _batch(b)
        ok = all(t in _cache for t in b)
        print(f"  batch {i}/{len(batches)}: {'done' if ok else 'FAILED'}")


def translate(text):
    if not text:
        return text
    if text in _cache:
        return _cache[text]
    if _blocked or os.environ.get("NO_GOOGLE"):
        return None
    _batch([text])
    return _cache.get(text)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def trim(text, limit=SUMMARY_MAX):
    text = clean(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind("。"), cut.rfind(". "), cut.rfind("！"), cut.rfind("；"))
    if m > limit * 0.5:
        return cut[: m + 1]
    return cut.rstrip() + "…"


def parse_posts(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    posts, seen = [], set()
    for h2 in soup.find_all("h2"):
        a = h2.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        if href.startswith("/"):
            href = BASE + href
        if not href.startswith(BASE):
            continue
        path = href[len(BASE):]
        if not path or path == "/" or path.startswith(SKIP_PREFIXES) or href in seen:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue

        tagline = ""
        for sib in h2.find_next_siblings():
            txt = sib.get_text(" ", strip=True)
            if txt:
                tagline = txt
                break
        if not tagline and h2.parent:
            tagline = h2.parent.get_text(" ", strip=True).replace(title, "", 1).strip()

        img = None
        container = h2.parent
        for _ in range(3):
            if container is None:
                break
            tag = container.find("img")
            if tag:
                src = tag.get("data-src") or tag.get("src") or ""
                if src and "thumbnail" not in src and "null.svg" not in src:
                    img = src
                break
            container = container.parent

        seen.add(href)
        posts.append({"title": title, "link": href, "tagline": tagline, "img": img})
    return posts


def extract_summary(post_html):
    soup = BeautifulSoup(post_html, "html.parser")

    body = None
    for sel in (".wp-posts-content", ".article-content", "article.article", "article", ".entry-content"):
        body = soup.select_one(sel)
        if body:
            break
    paras = []
    if body:
        for p in body.find_all(["p", "li"]):
            t = clean(p.get_text(" ", strip=True))
            if len(t) < 25:
                continue
            if any(n in t for n in NOISE) and len(t) < 80:
                continue
            paras.append(t)
            if sum(len(x) for x in paras) >= SUMMARY_MAX:
                break
    body_summary = " ".join(paras)

    meta = ""
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            meta = clean(tag["content"])
            if meta and not meta.startswith(SITE_DESC_START):
                break
            meta = ""

    best = body_summary if len(body_summary) >= 60 else (meta or body_summary)
    return trim(best)


def load_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    state = {}
    for link, val in raw.items():
        state[link] = val if isinstance(val, dict) else {"pubDate": val, "summary": None}
    return state


def build_description(it):
    parts = []
    if it.get("img"):
        parts.append(f'<img src="{htmllib.escape(it["img"], quote=True)}" />')
    if it.get("tagline"):
        parts.append(f"<p><b>{htmllib.escape(it['tagline'])}</b></p>")
    if it.get("summary"):
        parts.append(f"<p>{htmllib.escape(it['summary'])}</p>")
    html_str = "".join(parts).replace("]]>", "]]&gt;")
    return f"<![CDATA[{html_str}]]>"


def build_rss(items):
    now = format_datetime(datetime.now(timezone.utc))
    esc = htmllib.escape
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>MacKed - Mac软件分享与下载</title>",
        f"<link>{BASE}/</link>",
        "<description>MacKed 最新发布 (unofficial scraped feed)</description>",
        "<language>zh-CN</language>",
        f"<lastBuildDate>{now}</lastBuildDate>",
        "<generator>macked_rss.py</generator>",
    ]
    for it in items:
        out += [
            "<item>",
            f"<title>{esc(it['title'], quote=False)}</title>",
            f"<link>{esc(it['link'], quote=False)}</link>",
            f'<guid isPermaLink="true">{esc(it["link"], quote=False)}</guid>',
            f"<pubDate>{it['pubDate']}</pubDate>",
            f"<description>{build_description(it)}</description>",
            "</item>",
        ]
    out += ["</channel>", "</rss>"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="feed.xml")
    ap.add_argument("--state", default="macked_state.json")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--max-items", type=int, default=50)
    ap.add_argument("--no-translate", action="store_true", help="keep Chinese text")
    ap.add_argument("--no-summary", action="store_true")
    args = ap.parse_args()

    posts = []
    for p in range(1, args.pages + 1):
        url = BASE + "/" if p == 1 else f"{BASE}/page/{p}"
        try:
            posts += parse_posts(fetch(url))
        except requests.RequestException as e:
            print(f"Failed to fetch {url}: {e}", file=sys.stderr)
            if p == 1:
                sys.exit(1)

    if not posts:
        print("No posts found - layout changed or request blocked.", file=sys.stderr)
        sys.exit(1)

    posts = posts[: args.max_items]
    state = load_state(args.state)
    now = format_datetime(datetime.now(timezone.utc))

    if not args.no_translate:
        todo = []
        for post in posts:
            e = state.get(post["link"], {})
            if e.get("summary") and not e.get("summary_en"):
                todo.append(e["summary"])
            if post["tagline"] and not e.get("tagline_en"):
                todo.append(post["tagline"])
        if todo:
            print(f"Translating {len(todo)} texts in batches...")
            prefetch(todo)

    fetched = 0
    for post in posts:
        entry = state.setdefault(post["link"], {"pubDate": now, "summary": None})
        if not entry.get("pubDate"):
            entry["pubDate"] = now

        if not args.no_summary and not entry.get("summary"):
            try:
                entry["summary"] = extract_summary(fetch(post["link"]))
                fetched += 1
                print(f"  summary: {post['title']}")
            except requests.RequestException as e:
                print(f"  could not fetch {post['link']}: {e}", file=sys.stderr)
            time.sleep(REQUEST_DELAY)

        if not args.no_translate:
            if entry.get("summary") and not entry.get("summary_en"):
                entry["summary_en"] = translate(entry["summary"])
                print(f"  {'translated' if entry['summary_en'] else 'FAILED (will retry next run)'}: {post['title']}")
            if post["tagline"] and not entry.get("tagline_en"):
                entry["tagline_en"] = translate(post["tagline"])
            post["tagline"] = entry.get("tagline_en") or post["tagline"]
            post["summary"] = entry.get("summary_en") or entry.get("summary") or ""
        else:
            post["summary"] = entry.get("summary") or ""
        post["pubDate"] = entry["pubDate"]

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(build_rss(posts))
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(posts)} items to {args.output} ({fetched} new summaries)")


if __name__ == "__main__":
    main()
