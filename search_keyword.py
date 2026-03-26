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
# 除外判定
# ========================================

# 明らかに記事コンテンツではないサイト（ドメインで即除外）
NON_ARTICLE_DOMAINS = [
    # Google系
    "google.com", "google.co.jp",
    # SNS・動画
    "youtube.com", "twitter.com", "x.com", "instagram.com",
    "facebook.com", "tiktok.com",
    # EC・ポータル
    "amazon.co.jp", "rakuten.co.jp",
    # Wikipedia
    "wikipedia.org",
    # 求人
    "indeed.com",
    # 地図
    "maps.google.com",
]

# title/snippetに含まれていたら「第三者の記事コンテンツ」と判定するキーワード
ARTICLE_SIGNALS = [
    "おすすめ", "比較", "ランキング", "選び方", "口コミ", "評判",
    "人気", "厳選", "まとめ", "徹底解説", "完全ガイド", "選",
    "メリット", "デメリット", "違い", "効果", "費用", "相場",
    "体験談", "レビュー", "紹介", "解説", "方法", "やり方",
    "注意点", "ポイント", "コツ", "始め方", "についてを",
]

# title/snippetに含まれていたら「公式サイト」と判定するキーワード
OFFICIAL_SIGNALS = [
    "【公式】", "｜公式", "|公式", "公式サイト", "公式ホームページ",
    "ご予約", "ご相談", "診療のご案内", "当院について", "医師紹介",
    "アクセス・診療時間", "初めての方へ", "よくあるご質問",
    "求人情報", "採用情報", "スタッフ募集",
]

# snippetに以下のうち2つ以上含まれたら「公式サイト」と判定
OFFICIAL_COMBO_SIGNALS = [
    "tel", "0120-",
    "住所",
    "診療時間", "受付時間", "営業時間",
    "アクセス",
    "〒",
    "当院", "当クリニック",
]


def is_non_article_domain(domain: str) -> bool:
    """明らかに記事コンテンツではないサイトかをドメインで判定"""
    for pattern in NON_ARTICLE_DOMAINS:
        if pattern in domain:
            return True
    return False


def is_article_content(title: str, snippet: str) -> bool:
    """title+snippetから第三者の記事コンテンツかどうかを判定

    判定ロジック:
    1. 公式サイトシグナル（単独で確定）があれば → 記事ではない
    2. 公式サイトコンボシグナル（2つ以上一致）があれば → 記事ではない
    3. 記事シグナルがあれば → 記事である
    4. どちらもなければ → snippetの長さで推定
    """
    text = (title + " " + snippet).lower()

    # 1. 公式サイトシグナル（単独で確定）
    for signal in OFFICIAL_SIGNALS:
        if signal.lower() in text:
            return False

    # 2. コンボシグナル（2つ以上一致で公式サイト判定）
    #    snippetにTEL+住所、診療時間+〒 等が同時にあれば公式HP
    combo_count = sum(1 for s in OFFICIAL_COMBO_SIGNALS if s.lower() in text)
    if combo_count >= 2:
        return False

    # 3. 記事シグナルチェック
    for signal in ARTICLE_SIGNALS:
        if signal in text:
            return True

    # 4. どちらのシグナルもない場合:
    # snippetが長く、説明的な内容なら記事の可能性が高い
    if len(snippet) > 80:
        return True

    # 短いsnippet＋シグナルなし → 公式サイトの可能性が高い
    return False


def filter_urls(urls: list[dict], count: int = 3) -> list[dict]:
    """URLリストから第三者の記事コンテンツをフィルタリング

    上位から順に判定し、記事コンテンツがcount件集まるまで進む。
    """
    results = []
    for item in urls:
        url = item["url"]
        domain = item.get("domain", urlparse(url).netloc.lower())
        title = item.get("title", "")
        snippet = item.get("snippet", "")

        # 1. 明らかに非記事サイト（SNS、動画、EC等）はドメインで即除外
        if is_non_article_domain(domain):
            item["excluded"] = True
            item["reason"] = f"非記事サイト（{domain}）"
            continue

        # 2. title+snippetで記事コンテンツかどうかを判定
        if not is_article_content(title, snippet):
            item["excluded"] = True
            item["reason"] = "公式サイト/非記事コンテンツと判定"
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

        snippet = item.get("snippet", "")

        results.append({
            "rank": len(results) + 1,
            "title": title,
            "url": url,
            "domain": domain,
            "snippet": snippet,
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
            mark = f"  [除外: {reason}]" if excluded else "  [採用]"
            print(f"  {item['rank']}. {item['title']}")
            print(f"     {item['url']}{mark}")
            snippet = item.get("snippet", "")
            if snippet:
                print(f"     → {snippet[:80]}...")
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
