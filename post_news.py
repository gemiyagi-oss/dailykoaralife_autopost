#!/usr/bin/env python3
"""デイリーコアラライフのRSSを確認し、新着ニュースをThreadsに投稿する。

必要な環境変数（GitHub Secrets）:
  THREADS_USER_ID       ThreadsのユーザーID
  THREADS_ACCESS_TOKEN  Threadsのアクセストークン

任意の環境変数:
  MAX_PER_RUN          1回の実行で投稿する最大件数（既定: 2）
  MAX_AGE_HOURS        これより古い記事は投稿しない（既定: 36）
  EXCLUDE_CATEGORIES   投稿しないカテゴリ（カンマ区切り。例: 事件・事故,政治・社会）
  DRY_RUN=1            投稿せず、投稿予定の文面だけ表示する
  RSS_URL              RSSのURL（テスト用に file:// も可）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

RSS_URL = os.environ.get("RSS_URL", "https://koalalife-my.net/rss.xml")
STATE_FILE = Path(__file__).parent / "posted.json"
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "2"))
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "36"))
EXCLUDE_CATEGORIES = {
    c.strip() for c in os.environ.get("EXCLUDE_CATEGORIES", "").split(",") if c.strip()
}
DRY_RUN = os.environ.get("DRY_RUN") == "1"
API = "https://graph.threads.net/v1.0"
TEXT_LIMIT = 500  # Threadsの1投稿の上限文字数
KEEP_STATE = 500  # 投稿済みIDを保持する件数


def fetch_items():
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "koalalife-threads-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        root = ET.fromstring(res.read())
    items = []
    for node in root.iter("item"):
        def t(tag):
            el = node.find(tag)
            return (el.text or "").strip() if el is not None else ""

        link = t("link")
        try:
            published = parsedate_to_datetime(t("pubDate"))
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        items.append(
            {
                "id": t("guid") or link,
                "title": t("title"),
                "link": link,
                "category": t("category"),
                "description": t("description"),
                "published": published,
            }
        )
    return items


def build_text(item):
    head = f"📰 {item['title']}"
    tail = f"▶ 詳しくはデイリーコアラライフで\n{item['link']}"
    budget = TEXT_LIMIT - len(head) - len(tail) - 4  # 改行ぶん
    desc = item["description"]
    if budget < 20:
        desc = ""
    elif len(desc) > budget:
        desc = desc[: budget - 1].rstrip() + "…"
    parts = [head]
    if desc:
        parts.append(desc)
    parts.append(tail)
    return "\n\n".join(parts)


def api_post(path, params):
    token = os.environ["THREADS_ACCESS_TOKEN"]
    data = urllib.parse.urlencode({**params, "access_token": token}).encode()
    req = urllib.request.Request(f"{API}/{path}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Threads API {e.code}: {body}") from None


def post_to_threads(item):
    user_id = os.environ["THREADS_USER_ID"]
    container = api_post(
        f"{user_id}/threads",
        {"media_type": "TEXT", "text": build_text(item), "link_attachment": item["link"]},
    )
    time.sleep(5)
    published = api_post(f"{user_id}/threads_publish", {"creation_id": container["id"]})
    return published["id"]


def load_state():
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("posted", [])


def save_state(posted):
    if DRY_RUN:
        return
    STATE_FILE.write_text(
        json.dumps({"posted": posted[-KEEP_STATE:]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    items = fetch_items()
    if not items:
        print("RSSに記事がありません。")
        return 0

    posted = load_state()
    if posted is None and not DRY_RUN:
        # 初回は現在ある記事をすべて「投稿済み」として記録するだけ（過去記事の一斉投稿を防ぐ）
        save_state([i["id"] for i in items])
        print(f"初回実行: 既存の{len(items)}件を記録しました。投稿は次回以降の新着からです。")
        return 0
    posted = posted or []

    seen = set(posted)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    candidates = []
    for item in items:
        if item["id"] in seen:
            continue
        if item["published"] < cutoff or item["category"] in EXCLUDE_CATEGORIES:
            posted.append(item["id"])  # 対象外は投稿せず既読にする
            continue
        candidates.append(item)

    candidates.sort(key=lambda i: i["published"], reverse=True)  # 新しい順
    print(f"新着 {len(candidates)}件 / 今回の投稿上限 {MAX_PER_RUN}件")

    failed = False
    for item in candidates[:MAX_PER_RUN]:
        if DRY_RUN:
            print("--- DRY RUN ---")
            print(build_text(item))
            continue
        try:
            post_id = post_to_threads(item)
        except Exception as e:  # noqa: BLE001
            print(f"投稿失敗: {item['title']}\n  {e}", file=sys.stderr)
            failed = True
            break
        posted.append(item["id"])
        save_state(posted)
        print(f"投稿OK: {item['title']} (post id {post_id})")
        time.sleep(3)

    save_state(posted)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
