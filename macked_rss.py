#!/usr/bin/env python3
"""
Generate an RSS 2.0 feed from the https://macked.app homepage.

Usage:
    pip install requests beautifulsoup4
    python macked_rss.py                      # writes feed.xml
    python macked_rss.py -o /var/www/feed.xml # custom output path
    python macked_rss.py --pages 3            # also scrape /page/2, /page/3

The homepage only shows relative times ("5分钟前"), so the script remembers when
it first saw each post (in a small JSON state file) and uses that as <pubDate>.
Run it on a schedule (cron / GitHub Actions) to keep the feed fresh.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

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

# Links that are navigation/category pages, not posts
SKIP_PREFIXES = ("/programs", "/tag/", "/topics/", "/page/", "/faq", "/wishlist",
                 "/app-store-share", "/advertise", "/user-sign", "/oauth", "/cdn-cgi")


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def parse_posts(html):
    soup = BeautifulSoup(html, "html.parser")
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

        # Short description: first non-empty text block after the title
        desc = ""
        for sib in h2.find_next_siblings():
            txt = sib.get_text(" ", strip=True)
            if txt:
                desc = txt
                break
        if not desc and h2.parent:
            desc = h2.parent.get_text(" ", strip=True).replace(title, "", 1).strip()

        # Thumbnail (skip placeholder images)
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
        posts.append({"title": title, "link": href, "desc": desc, "img": img})
    return posts


def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def build_rss(items):
    now = format_datetime(datetime.now(timezone.utc))
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
        desc = escape(it["desc"])
        if it.get("img"):
            desc = f'&lt;img src="{escape(it["img"])}" /&gt;&lt;br/&gt;' + desc
        out += [
            "<item>",
            f"<title>{escape(it['title'])}</title>",
            f"<link>{escape(it['link'])}</link>",
            f'<guid isPermaLink="true">{escape(it["link"])}</guid>',
            f"<pubDate>{it['pubDate']}</pubDate>",
            f"<description>{desc}</description>",
            "</item>",
        ]
    out += ["</channel>", "</rss>"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="feed.xml")
    ap.add_argument("--state", default="macked_state.json")
    ap.add_argument("--pages", type=int, default=1, help="number of listing pages to scrape")
    ap.add_argument("--max-items", type=int, default=50)
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
        print("No posts found - the site layout may have changed or Cloudflare blocked the request.",
              file=sys.stderr)
        sys.exit(1)

    state = load_state(args.state)
    now = format_datetime(datetime.now(timezone.utc))
    for post in posts:
        # first-seen time becomes the pubDate and never changes afterwards
        post["pubDate"] = state.setdefault(post["link"], now)

    posts = posts[: args.max_items]
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(build_rss(posts))
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(posts)} items to {args.output}")


if __name__ == "__main__":
    main()
