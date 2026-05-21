#!/usr/bin/env python3
"""
「なんでも」サイト内のシステム生成記事だけをジャンル別カテゴリへ振り分ける。

使い方:
  .venv/bin/python categorize_nandemo_posts.py --site sites/nandemo.json --root-slug sakai --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass

import requests


SYSTEM_MARKERS = (
    "seo-article-common-css:start",
    "official-site-button-wrap",
    "clinic-screenshot",
)

GENRE_CATEGORY_SLUGS = {
    "aga": "sakai-aga-5domain",
    "ed": "sakai-ed-5domain",
    "houkei": "sakai-houkei-5domain",
    "manjaro": "sakai-manjaro-5domain",
}


@dataclass
class SiteConfig:
    site_url: str
    username: str
    app_password: str


def load_site_config(path: str) -> SiteConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return SiteConfig(
        site_url=data["site_url"].rstrip("/"),
        username=data["username"],
        app_password=data["app_password"],
    )


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def is_system_generated(raw_html: str) -> bool:
    return any(marker in (raw_html or "") for marker in SYSTEM_MARKERS)


def classify_genre(title: str, raw_html: str) -> str | None:
    text = normalize_text(f"{title}\n{raw_html}").lower()

    if any(token in text for token in ("マンジャロ", "mounjaro", "manjaro", "ダイエット注射", "glp-1")):
        return "manjaro"
    if "包茎" in text:
        return "houkei"
    if any(token in text for token in ("ed", "勃起不全", "バイアグラ", "シアリス", "レビトラ", "タダラフィル")):
        # "ed" は単独英字としても多いので、医療系キーワードと合わせて広めに拾う
        if any(token in text for token in ("ed治療", "edクリニック", "勃起", "バイアグラ", "シアリス", "レビトラ", "タダラフィル")):
            return "ed"
    if any(token in text for token in ("aga", "faga", "薄毛", "発毛", "抜け毛")):
        return "aga"
    return None


def fetch_category_map(cfg: SiteConfig) -> dict[str, int]:
    resp = requests.get(
        f"{cfg.site_url}/wp-json/wp/v2/categories",
        params={"per_page": 100, "context": "edit"},
        auth=(cfg.username, cfg.app_password),
        timeout=30,
    )
    resp.raise_for_status()
    return {item["slug"]: item["id"] for item in resp.json()}


def fetch_all_posts(cfg: SiteConfig) -> list[dict]:
    posts: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{cfg.site_url}/wp-json/wp/v2/posts",
            params={
                "context": "edit",
                "per_page": 100,
                "page": page,
                "status": "publish,future,draft,pending,private",
            },
            auth=(cfg.username, cfg.app_password),
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        posts.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return posts


def update_post_categories(cfg: SiteConfig, post_id: int, categories: list[int]) -> None:
    resp = requests.post(
        f"{cfg.site_url}/wp-json/wp/v2/posts/{post_id}",
        auth=(cfg.username, cfg.app_password),
        json={"categories": categories},
        timeout=30,
    )
    resp.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="なんでも記事をジャンル別カテゴリへ振り分ける")
    parser.add_argument("--site", required=True, help="サイト設定JSON")
    parser.add_argument("--root-slug", default="sakai", help="なんでも親カテゴリのスラッグ")
    parser.add_argument("--apply", action="store_true", help="実際にカテゴリを更新する")
    args = parser.parse_args()

    cfg = load_site_config(args.site)
    category_map = fetch_category_map(cfg)

    if args.root_slug not in category_map:
        print(f"Error: root category slug not found: {args.root_slug}")
        return 1

    for slug in GENRE_CATEGORY_SLUGS.values():
        if slug not in category_map:
            print(f"Error: genre category slug not found: {slug}")
            return 1

    root_category_id = category_map[args.root_slug]
    posts = fetch_all_posts(cfg)

    targets: list[dict] = []
    for post in posts:
        raw_html = (post.get("content") or {}).get("raw", "")
        if not is_system_generated(raw_html):
            continue
        genre = classify_genre(post["title"]["raw"], raw_html)
        if not genre:
            continue
        targets.append(
            {
                "id": post["id"],
                "status": post["status"],
                "title": post["title"]["raw"],
                "genre": genre,
                "current_categories": post.get("categories", []),
                "target_categories": [root_category_id, category_map[GENRE_CATEGORY_SLUGS[genre]]],
            }
        )

    print(f"Detected {len(targets)} system-generated nandemo posts.")
    for item in targets:
        print(
            f"[{item['genre']}] {item['id']} {item['status']} "
            f"current={item['current_categories']} target={item['target_categories']} "
            f"{item['title']}"
        )

    if not args.apply:
        return 0

    updated = 0
    for item in targets:
        if item["current_categories"] == item["target_categories"]:
            continue
        update_post_categories(cfg, item["id"], item["target_categories"])
        updated += 1
        print(f"Updated post {item['id']} -> {item['target_categories']}")

    print(f"Done. Updated {updated} posts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
