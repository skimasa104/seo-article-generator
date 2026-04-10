#!/usr/local/bin/python3.12
"""
キーワード検索スクリプト (Step 0)

Serper.dev API で Google 検索結果を取得し、
記事として使いやすい候補をスコアリングして返す。

- raw の検索結果はそのまま保存
- filtered は採用候補のみ
- 広いキーワードでは地域特化ページを強く減点
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

from env_utils import load_project_env
from output_utils import ensure_keyword_output_dir, keyword_to_slug

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERPER_API_URL = "https://google.serper.dev/search"
RAW_RESULT_COUNT = 20
DEFAULT_COUNT = 7

load_project_env()


NON_ARTICLE_DOMAINS = [
    "google.com", "google.co.jp",
    "youtube.com", "twitter.com", "x.com", "instagram.com",
    "facebook.com", "tiktok.com",
    "amazon.co.jp", "rakuten.co.jp",
    "wikipedia.org",
    "indeed.com",
    "maps.google.com",
]

OFFICIAL_SIGNALS = [
    "【公式】", "｜公式", "|公式", "公式サイト", "公式ホームページ",
    "ご予約", "ご相談", "診療のご案内", "当院について", "医師紹介",
    "アクセス・診療時間", "初めての方へ", "よくあるご質問",
    "求人情報", "採用情報", "スタッフ募集",
]

OFFICIAL_COMBO_SIGNALS = [
    "tel", "0120-", "住所", "診療時間", "受付時間", "営業時間",
    "アクセス", "〒", "当院", "当クリニック",
]

ARTICLE_SIGNALS = [
    "おすすめ", "比較", "ランキング", "選び方", "口コミ", "評判",
    "人気", "厳選", "まとめ", "徹底解説", "完全ガイド",
    "メリット", "デメリット", "違い", "効果", "費用", "相場",
    "レビュー", "紹介", "解説", "方法", "注意点", "ポイント",
]

PREFECTURES = [
    "北海道", "青森", "岩手", "宮城", "秋田", "山形", "福島",
    "茨城", "栃木", "群馬", "埼玉", "千葉", "東京", "神奈川",
    "新潟", "富山", "石川", "福井", "山梨", "長野",
    "岐阜", "静岡", "愛知", "三重",
    "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
    "鳥取", "島根", "岡山", "広島", "山口",
    "徳島", "香川", "愛媛", "高知",
    "福岡", "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島", "沖縄",
]

MAJOR_AREAS = [
    "新宿", "渋谷", "池袋", "横浜", "名古屋", "栄", "梅田", "難波", "なんば",
    "天王寺", "心斎橋", "札幌", "仙台", "大宮", "千葉", "川崎", "神戸",
    "京都", "博多", "天神", "広島", "金沢", "那覇",
]


def normalize_text(text: str) -> str:
    return (text or "").lower()


def is_non_article_domain(domain: str) -> bool:
    return any(pattern in domain for pattern in NON_ARTICLE_DOMAINS)


def looks_official(title: str, snippet: str) -> bool:
    text = normalize_text(f"{title} {snippet}")
    if any(signal.lower() in text for signal in OFFICIAL_SIGNALS):
        return True
    combo_count = sum(1 for signal in OFFICIAL_COMBO_SIGNALS if signal.lower() in text)
    return combo_count >= 2


def keyword_has_location(keyword: str) -> bool:
    return any(token in keyword for token in PREFECTURES + MAJOR_AREAS)


def find_location_tokens(text: str) -> list[str]:
    hits = []
    for token in PREFECTURES + MAJOR_AREAS:
        if token in text:
            hits.append(token)
    return sorted(set(hits))


def score_article(keyword: str, item: dict) -> tuple[int, list[str]]:
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    url = item.get("url", "")
    domain = item.get("domain", "")
    text = f"{title} {snippet} {url}"
    lowered = normalize_text(text)
    reasons = []
    score = 0

    if is_non_article_domain(domain):
        return -999, [f"非記事サイト（{domain}）"]

    if looks_official(title, snippet):
        return -999, ["公式サイト/院ページ寄り"]

    if any(signal in lowered for signal in [s.lower() for s in ARTICLE_SIGNALS]):
        score += 8
        reasons.append("比較記事シグナルあり")

    keyword_tokens = [token for token in re.split(r"\s+", keyword) if token]
    token_hits = sum(1 for token in keyword_tokens if token.lower() in lowered)
    score += token_hits * 3
    if token_hits:
        reasons.append(f"キーワード一致 {token_hits}件")

    if "online" in lowered or "オンライン" in text:
        score += 1

    location_hits = find_location_tokens(text)
    if keyword_has_location(keyword):
        keyword_locations = [loc for loc in location_hits if loc in keyword]
        if keyword_locations:
            score += 4
            reasons.append("地域一致")
        elif location_hits:
            score -= 4
            reasons.append("地域ズレ")
    else:
        if location_hits:
            score -= 10
            reasons.append("広いキーワードに対して地域特化")

    if re.search(r"/(column|blog|note|media|article)/", url):
        score += 1

    if len(snippet) < 40:
        score -= 2
        reasons.append("snippetが短い")

    return score, reasons


def search_google(keyword: str, api_key: str, max_results: int = RAW_RESULT_COUNT) -> list[dict]:
    body = json.dumps({
        "q": keyword,
        "gl": "jp",
        "hl": "ja",
        "num": max_results,
    }).encode()

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(SERPER_API_URL, data=body, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  API Error: {e.code}")
        print(f"  {e.read().decode()[:300]}")
        return []

    results = []
    for i, item in enumerate(data.get("organic", []), start=1):
        url = item.get("link", "")
        title = item.get("title", "")
        if not url or not title:
            continue
        results.append({
            "rank": i,
            "title": title,
            "url": url,
            "domain": urlparse(url).netloc.lower(),
            "snippet": item.get("snippet", ""),
        })
        if len(results) >= max_results:
            break
    return results


def filter_urls(keyword: str, urls: list[dict], count: int) -> list[dict]:
    scored = []
    for item in urls:
        score, reasons = score_article(keyword, item)
        item["score"] = score
        item["score_reasons"] = reasons
        item["excluded"] = score < 0
        scored.append(item)

    candidates = [item for item in scored if item["score"] >= 0]
    candidates.sort(key=lambda x: (-x["score"], x["rank"]))
    selected = candidates[:count]

    for item in scored:
        if item not in selected:
            item["excluded"] = True
            if item["score"] >= 0:
                item["reason"] = "スコア順で対象外"
            elif item["score_reasons"]:
                item["reason"] = " / ".join(item["score_reasons"])
        else:
            item["excluded"] = False

    return selected


def main():
    parser = argparse.ArgumentParser(description="Google検索キーワードからURL取得")
    parser.add_argument("keyword", help="検索キーワード（例: 'AGA 横浜'）")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"取得するURL数（デフォルト: {DEFAULT_COUNT}）")
    parser.add_argument("--scrape", action="store_true", help="取得後にscrape.pyを実行")
    parser.add_argument("--show-all", action="store_true", help="raw結果と除外理由を表示")
    args = parser.parse_args()

    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("Error: SERPER_API_KEY が設定されていません。")
        sys.exit(1)

    keyword = args.keyword
    print(f"検索キーワード: {keyword}")
    print(f"取得数: {args.count}")
    print()

    print("Google検索中（Serper.dev API）...")
    all_results = search_google(keyword, api_key, max_results=RAW_RESULT_COUNT)
    if not all_results:
        print("検索結果を取得できませんでした。")
        sys.exit(1)

    print(f"検索結果: {len(all_results)} 件取得\n")
    filtered = filter_urls(keyword, all_results, count=args.count)

    if args.show_all:
        print("=" * 60)
        print("全検索結果:")
        print("=" * 60)
        for item in all_results:
            mark = "[採用]" if item in filtered else f"[除外: {item.get('reason', '')}]"
            score = item.get("score", "")
            print(f"  {item['rank']}. {item['title']} {mark} score={score}")
            print(f"     {item['url']}")
        print()

    print("=" * 60)
    print(f"対象記事 TOP {len(filtered)}:")
    print("=" * 60)
    for i, item in enumerate(filtered, 1):
        print(f"  {i}. {item['title']}")
        print(f"     {item['url']}")
        if item.get("score_reasons"):
            print(f"     reasons: {', '.join(item['score_reasons'])}")
    print()

    if not filtered:
        print("対象となる記事が見つかりませんでした。")
        sys.exit(1)

    output_dir = ensure_keyword_output_dir(keyword)
    keyword_slug = keyword_to_slug(keyword)
    json_path = os.path.join(output_dir, f"{keyword_slug}_search_results.json")
    save_data = {
        "keyword": keyword,
        "source": "serper_google",
        "filtered": filtered,
        "all_results": all_results,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"保存: {json_path}")

    if args.scrape:
        urls = [item["url"] for item in filtered]
        scrape_path = os.path.join(SCRIPT_DIR, "scrape.py")
        cmd = [sys.executable, scrape_path, keyword] + urls
        print("\nscrape.py を実行中...")
        subprocess.run(cmd, cwd=SCRIPT_DIR)


if __name__ == "__main__":
    main()
