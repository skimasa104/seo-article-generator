#!/usr/local/bin/python3.12
"""
記事内のGoogleマップ埋め込みを補正する。

- 可能なら住所をジオコーディングして座標埋め込みにする
- 取れない場合は住所優先の検索クエリにフォールバックする
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import quote

import requests


SECTION_PATTERN = re.compile(r"(<h3 id=\"clinic-[^\"]+\">.*?</h3>)(.*?)(?=<h3 id=\"clinic-|$)", re.DOTALL)
MAP_PATTERN = re.compile(
    r'(<div class="clinic-map">\s*<iframe[^>]*src=")([^"]+)("[^>]*></iframe>\s*</div>)',
    re.DOTALL,
)
NAME_PATTERN = re.compile(r"<h3 id=\"clinic-[^\"]+\">(.*?)</h3>", re.DOTALL)
ADDRESS_PATTERN = re.compile(r"<tr><th>住所</th><td>(.*?)</td></tr>", re.DOTALL)
CACHE_PATH = os.path.join(os.path.dirname(__file__), ".map_geocode_cache.json")
MAP_SRC_TEMPLATE = "https://maps.google.com/maps?hl=ja&q={query}&t=m&z=17&output=embed&iwloc=B"


def strip_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_address(text: str) -> str:
    text = strip_tags(text)
    if "：" in text:
        text = text.split("：", 1)[1].strip()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    return text


def load_cache() -> dict:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


def has_multiple_locations(address: str) -> bool:
    return any(marker in address for marker in ["西口院", "東口院", "本院：", "院："])


def geocode_address(address: str, cache: dict) -> str | None:
    address = normalize_address(address)
    if not address:
        return None
    if address in cache:
        return cache[address]

    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": "seo-article-generator/1.0"},
            timeout=10,
        )
        response.raise_for_status()
        rows = response.json()
    except Exception:
        return None

    if not rows:
        return None

    lat = rows[0].get("lat")
    lon = rows[0].get("lon")
    if not lat or not lon:
        return None

    value = f"{lat},{lon}"
    cache[address] = value
    return value


def build_query(name: str, address: str) -> str:
    name = strip_tags(name)
    address = normalize_address(address)
    # 単一院のときは住所のみの方が Google Maps で単独地点に寄りやすい。
    if address and not has_multiple_locations(address):
        return address
    if address:
        return f"{name} {address}"
    return name


def replace_section_map(match, cache: dict):
    heading_html = match.group(1)
    body_html = match.group(2)

    name_match = NAME_PATTERN.search(heading_html)
    address_match = ADDRESS_PATTERN.search(body_html)
    if not name_match or not address_match:
        return match.group(0)

    name = strip_tags(name_match.group(1))
    address = normalize_address(address_match.group(1))
    coordinates = geocode_address(address, cache)
    query = coordinates or build_query(name, address)
    query = quote(query, safe=",")

    def map_replacer(map_match):
        return map_match.group(1) + MAP_SRC_TEMPLATE.format(query=query) + map_match.group(3)

    new_body_html, count = MAP_PATTERN.subn(map_replacer, body_html, count=1)
    if count == 0:
        return match.group(0)
    return heading_html + new_body_html


def main():
    parser = argparse.ArgumentParser(description="Googleマップ埋め込み補正")
    parser.add_argument("--html", required=True, help="記事HTMLファイル")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    cache = load_cache()

    def section_replacer(match):
        return replace_section_map(match, cache)

    updated_html, count = SECTION_PATTERN.subn(section_replacer, html)

    with open(args.html, "w", encoding="utf-8") as f:
        f.write(updated_html)

    save_cache(cache)

    print(f"マップ埋め込みを補正しました: {count}セクション")


if __name__ == "__main__":
    main()
