#!/usr/local/bin/python3.12
"""
キーワード検索スクリプト (Step 0)
Serper.dev APIでGoogle検索を行い、アフィリエイト/メディア記事の上位URLを取得する。
クリニック公式HP等は除外し、上位3記事を返す。

使い方:
  python search_keyword.py "AGA 横浜"
  python search_keyword.py "AGA 横浜" --count 5
  python search_keyword.py "AGA 横浜" --scrape   # そのままscrape.pyに渡す

環境変数:
  SERPER_API_KEY: Serper.dev の API キー
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from urllib.parse import urlparse

# ========================================
# 設定
# ========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERPER_API_URL = "https://google.serper.dev/search"


# ========================================
# 除外パターン
# ========================================
EXCLUDE_DOMAIN_PATTERNS = [
    # 大手AGAクリニック
    "agaskin.net", "agahairclinic.or.jp", "clinic-bh.com",
    "clinicfor.life", "dmmclinic.com", "gorilla.clinic",
    "agaskin-woman.site", "hairmedical.com", "rs-clinic.com",
    "asc.clinic", "b-line-c.com", "yokohamachuoh-mens.com",
    "nido-clinic.com", "tokyobeauty.jp",
    "sbc-aga.jp", "will-agaclinic.com", "eastcl.com",
    "agacare.clinic", "aga-yobou.jp", "menshealth-tokyo.com",
    # 大手脱毛クリニック
    "rizeclinic.com", "aletheia-clinic.com", "tcb-beauty.net",
    "s-b-c.net", "reginaclinic.jp", "frey-a.com", "eminal-clinic.jp",
    # 大手ED
    "fit-clinic.com",
    # 総合病院・医療法人・行政
    ".or.jp", ".ac.jp", ".go.jp",
    # Google系
    "google.com", "google.co.jp",
    # SNS・動画
    "youtube.com", "twitter.com", "x.com", "instagram.com",
    "facebook.com", "tiktok.com", "ameblo.jp",
    # EC・ポータル
    "amazon.co.jp", "rakuten.co.jp", "yahoo.co.jp",
    # 地図・予約
    "hotpepper.jp", "epark.jp", "caloo.jp",
    # Wikipedia
    "wikipedia.org",
    # 求人
    "indeed.com", "recruit.co.jp",
]

EXCLUDE_PATH_PATTERNS = [
    "/access", "/price", "/doctor", "/about", "/contact",
    "/recruit", "/privacy", "/sitemap",
]


# ========================================
# フィルタリング
# ========================================
def is_clinic_official(url: str) -> bool:
    """クリニック公式HPかどうかを判定"""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    for pattern in EXCLUDE_DOMAIN_PATTERNS:
        if pattern in domain:
            return True

    for pattern in EXCLUDE_PATH_PATTERNS:
        if path.endswith(pattern) or path.endswith(pattern + "/"):
            return True

    parts = domain.replace("www.", "").split(".")
    name_part = parts[0] if parts else ""
    if any(w in name_part for w in ["clinic", "beauty", "medical", "hospital", "derm", "hifuka", "skin"]):
        path_depth = len([p for p in path.strip("/").split("/") if p])
        if path_depth <= 1:
            return True

    return False


def filter_urls(urls: list[dict], count: int = 3) -> list[dict]:
    """URLリストからアフィリエイト/メディア記事をフィルタリング"""
    results = []
    for item in urls:
        url = item["url"]
        if is_clinic_official(url):
            item["excluded"] = True
            item["reason"] = "クリニック公式HP"
            continue
        item["excluded"] = False
        results.append(item)
        if len(results) >= count:
            break
    return results


# ========================================
# Serper.dev API検索
# ========================================
def search_google(keyword: str, api_key: str, max_results: int = 20) -> list[dict]:
    """Serper.dev APIでGoogle検索を実行"""

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
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  API Error: {e.code}")
        print(f"  {e.read().decode()[:300]}")
        return []

    results = []
    seen_domains = set()

    for item in data.get("organic", []):
        url = item.get("link", "")
        title = item.get("title", "")
        if not url or not title:
            continue

        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        results.append({
            "rank": len(results) + 1,
            "title": title,
            "url": url,
            "domain": domain,
        })

        if len(results) >= max_results:
            break

    return results


# ========================================
# メイン
# ========================================
def main():
    parser = argparse.ArgumentParser(description="Google検索キーワードからURL取得")
    parser.add_argument("keyword", help="検索キーワード（例: 'AGA 横浜'）")
    parser.add_argument("--count", type=int, default=3, help="取得するURL数（デフォルト: 3）")
    parser.add_argument("--scrape", action="store_true", help="取得後にscrape.pyを実行")
    parser.add_argument("--show-all", action="store_true", help="除外されたURLも表示")
    args = parser.parse_args()

    # APIキー確認
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        # .envから読み込み
        env_path = os.path.join(SCRIPT_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SERPER_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip("'\"")
                        break

    if not api_key:
        print("Error: SERPER_API_KEY が設定されていません。")
        print("  .env に SERPER_API_KEY=your-key を追加してください。")
        sys.exit(1)

    keyword = args.keyword
    print(f"検索キーワード: {keyword}")
    print(f"取得数: {args.count}")
    print()

    # Google検索実行
    print("Google検索中（Serper.dev API）...")
    all_results = search_google(keyword, api_key, max_results=20)

    if not all_results:
        print("検索結果を取得できませんでした。")
        sys.exit(1)

    print(f"検索結果: {len(all_results)} 件取得\n")

    # フィルタリング
    filtered = filter_urls(all_results, count=args.count)

    # 全結果表示
    if args.show_all:
        print("=" * 60)
        print("全検索結果:")
        print("=" * 60)
        for item in all_results:
            excluded = item.get("excluded", False)
            reason = item.get("reason", "")
            mark = "  [除外]" if excluded else ""
            print(f"  {item['rank']}. {item['title']}")
            print(f"     {item['url']}{mark} {reason}")
        print()

    # フィルタ結果表示
    print("=" * 60)
    print(f"対象記事 TOP {args.count}:")
    print("=" * 60)
    for i, item in enumerate(filtered, 1):
        print(f"  {i}. {item['title']}")
        print(f"     {item['url']}")
    print()

    if not filtered:
        print("対象となる記事が見つかりませんでした。")
        sys.exit(1)

    # JSON保存
    output_dir = os.path.join(SCRIPT_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    keyword_slug = keyword.replace(" ", "_")
    json_path = os.path.join(output_dir, f"{keyword_slug}_search_results.json")

    save_data = {
        "keyword": keyword,
        "filtered": filtered,
        "all_results": all_results,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"保存: {json_path}")

    # scrape.pyに渡す
    if args.scrape:
        urls = [item["url"] for item in filtered]
        scrape_path = os.path.join(SCRIPT_DIR, "scrape.py")
        cmd = [sys.executable, scrape_path, keyword] + urls
        print(f"\nscrape.py を実行中...")
        subprocess.run(cmd)


if __name__ == "__main__":
    main()
