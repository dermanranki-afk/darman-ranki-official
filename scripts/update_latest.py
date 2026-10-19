#!/usr/bin/env python3
"""Refresh public episode info for the author's GitHub Pages site.

Standard library only. Feed/page structure may change; never replace an existing
chapter with an unverified entry. Logs are visible in GitHub Actions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "latest.json"
ROYAL_ID = "198087"
KAKUYOMU_ID = "2912051604574171459"
ROYAL_RSS_URL = f"https://www.royalroad.com/fiction/syndication/{ROYAL_ID}"
ROYAL_WORK_URL = f"https://www.royalroad.com/fiction/{ROYAL_ID}"
KAKUYOMU_WORK_URL = f"https://kakuyomu.jp/works/{KAKUYOMU_ID}"
JST = ZoneInfo("Asia/Tokyo")


def request_text(url: str) -> str:
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; NovelUpdateChecker/1.0; +https://github.com)",
        "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.6",
        "Accept-Language": "ja,en;q=0.8",
    })
    with urlopen(req, timeout=25) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read(8_000_000).decode(charset, errors="replace")


def publication_day(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Always show dates in Japan time, regardless of where the automation runs.
    return dt.astimezone(JST).date().isoformat()


def safe_chapter(url: str, site: str) -> bool:
    p = urlparse(url)
    if p.scheme != "https":
        return False
    if site == "royalroad":
        return p.hostname in {"www.royalroad.com", "royalroad.com"} and bool(re.search(rf"^/fiction/{ROYAL_ID}/(?:[^/]+/)?chapter/\d+", p.path))
    return p.hostname == "kakuyomu.jp" and bool(re.fullmatch(rf"/works/{KAKUYOMU_ID}/episodes/\d+/?", p.path))


def royal_rss(xml: str) -> dict:
    root = ET.fromstring(xml)
    candidates = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        date = (item.findtext("pubDate") or "").strip()
        if title and safe_chapter(link, "royalroad"):
            candidates.append((date, {"title": title, "url": link, "published_date": publication_day(date)}))
    if not candidates:
        # Atom fallback (for feed versions which use Atom, not RSS)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for item in root.findall(".//atom:entry", ns):
            title = (item.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_element = item.find("atom:link", ns)
            link = link_element.get("href", "") if link_element is not None else ""
            date = (item.findtext("atom:published", default="", namespaces=ns) or "").strip()
            if title and safe_chapter(link, "royalroad"):
                candidates.append((date, {"title": title, "url": link, "published_date": publication_day(date)}))
    if not candidates:
        raise ValueError("Royal Road RSS contained no verified chapter URLs")
    # PubDate is RFC 2822, not ISO; do not sort alphabetically by weekday.
    # Preserve RSS order (normally newest-first) when a timestamp is absent.
    def timestamp(candidate):
        raw = candidate[0]
        try:
            return parsedate_to_datetime(raw).timestamp()
        except (TypeError, ValueError, OverflowError):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                return float("-inf")
    candidates.sort(key=timestamp, reverse=True)
    return candidates[0][1]


class RoyalChapterLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            full = urljoin("https://www.royalroad.com", href)
            if safe_chapter(full, "royalroad"):
                self.href = full
                self.text = []

    def handle_data(self, data):
        if self.href:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            title = " ".join("".join(self.text).split())
            if title:
                self.links.append({"title": title, "url": self.href, "published_date": None})
            self.href = None
            self.text = []


def royal_html_fallback(html: str) -> dict:
    parser = RoyalChapterLinks()
    parser.feed(html)
    if not parser.links:
        raise ValueError("Royal Road page has no recognized chapter links")
    # Work-page table of contents is displayed in chapter order.
    return parser.links[-1]


def fetch_royalroad() -> dict:
    try:
        return royal_rss(request_text(ROYAL_RSS_URL))
    except (ValueError, ET.ParseError, HTTPError, URLError) as exc:
        print(f"Royal Road RSS unavailable; trying public story page: {exc}", file=sys.stderr)
        return royal_html_fallback(request_text(ROYAL_WORK_URL))


class NextDataExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.scripts = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.active = True
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.active = False
            self.scripts.append("".join(self.parts))


def kakuyomu_html(html: str) -> dict:
    parser = NextDataExtractor()
    parser.feed(html)
    if not parser.scripts:
        raise ValueError("Kakuyomu __NEXT_DATA__ not found")
    state = json.loads(parser.scripts[0])["props"]["pageProps"]["__APOLLO_STATE__"]
    if f"Work:{KAKUYOMU_ID}" not in state:
        raise ValueError("Kakuyomu page did not belong to expected work")
    now = datetime.now(timezone.utc)
    candidates = []
    for key, episode in state.items():
        if not isinstance(episode, dict) or episode.get("__typename") != "Episode":
            continue
        eid = str(episode.get("id") or key.rsplit(":", 1)[-1])
        title = episode.get("title")
        published = episode.get("publishedAt")
        url = f"{KAKUYOMU_WORK_URL}/episodes/{eid}"
        if not isinstance(title, str) or not title.strip() or not published or not safe_chapter(url, "kakuyomu"):
            continue
        try:
            instant = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if instant > now:
            continue  # Do not list scheduled but unpublished chapters.
        candidates.append((instant, {"title": title.strip(), "url": url, "published_date": publication_day(published)}))
    if not candidates:
        raise ValueError("Kakuyomu page contained no published episodes")
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def fetch_kakuyomu() -> dict:
    return kakuyomu_html(request_text(KAKUYOMU_WORK_URL))


def update_data():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    successes = 0
    for site, fetcher in (("royalroad", fetch_royalroad), ("kakuyomu", fetch_kakuyomu)):
        try:
            info = fetcher()
            prev = data.get(site) or {}
            # Preserve last known date when the fallback HTML has no exact timestamp
            if info["published_date"] is None and info["url"] == prev.get("url"):
                info["published_date"] = prev.get("published_date")
            if data.get(site) != info:
                print(f"Updated {site}: {info['title']}")
                data[site] = info
            else:
                print(f"No new chapter for {site}")
            successes += 1
        except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError, HTTPError, URLError, TimeoutError) as exc:
            print(f"WARNING: {site} fetch failed; retaining existing published chapter: {exc}", file=sys.stderr)
    if successes == 0:
        raise RuntimeError("Both sites could not be checked; keeping previous deployed page")
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    update_data()
