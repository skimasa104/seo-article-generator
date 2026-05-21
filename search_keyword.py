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
from urllib.parse import urlparse, urlunparse

from env_utils import load_project_env
from output_utils import ensure_output_dir_for_key, keyword_to_slug, resolve_output_key

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


def canonicalize_result_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    clean = parsed._replace(query="", fragment="")
    canonical = urlunparse(clean)
    if canonical.endswith("/") and clean.path not in {"", "/"}:
        canonical = canonical.rstrip("/")
    return canonical


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
        score += 4
        reasons.append("記事URL構造")

    if len(snippet) < 40:
        score -= 2
        reasons.append("snippetが短い")

    return score, reasons


def search_google(keyword: str, api_key: str, max_results: int = RAW_RESULT_COUNT) -> tuple[list[dict], dict]:
    """Serper.dev で Google 検索結果を取得。
    戻り値は (organic 結果のリスト, 関連情報 dict)。
    関連情報には people_also_ask / related_searches を含める（SEO 用のサジェストキーワード抽出のため）。"""
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
        return [], {}

    results = []
    for i, item in enumerate(data.get("organic", []), start=1):
        url = item.get("link", "")
        title = item.get("title", "")
        if not url or not title:
            continue
        url = canonicalize_result_url(url)
        results.append({
            "rank": i,
            "title": title,
            "url": url,
            "domain": urlparse(url).netloc.lower(),
            "snippet": item.get("snippet", ""),
        })
        if len(results) >= max_results:
            break

    # 関連キーワード・PAA（SEO 設計に使う）
    paa = []
    for item in data.get("peopleAlsoAsk", []) or []:
        q = (item.get("question") or "").strip()
        if q:
            paa.append({
                "question": q,
                "snippet": (item.get("snippet") or "").strip(),
            })
    related = []
    for item in data.get("relatedSearches", []) or []:
        q = (item.get("query") or "").strip()
        if q:
            related.append(q)

    suggest_info = {
        "people_also_ask": paa,
        "related_searches": related,
    }
    return results, suggest_info


def _ensure_reference_url_first(
    reference_url: str,
    filtered: list[dict],
    all_results: list[dict],
    count: int,
) -> list[dict]:
    """指定された reference_url を filtered リストの先頭に配置する。
    - filtered 内に既にあれば → 先頭へ移動
    - all_results にあれば → そこから引き上げて先頭に追加
    - どちらにも無ければ → 最小情報で entry を作って先頭に追加（scrape は URL があれば実行できる）
    末尾を切り詰めて count 件を維持する。"""
    canonical = canonicalize_result_url(reference_url) or reference_url

    def _matches(item: dict) -> bool:
        u = (item.get("url") or "").strip()
        return u == reference_url or u == canonical

    # 既に filtered 内
    for i, item in enumerate(filtered):
        if _matches(item):
            if i == 0:
                return filtered
            picked = filtered.pop(i)
            picked.setdefault("score_reasons", []).append("参考URL指定で先頭固定")
            return [picked] + filtered

    # all_results 内（除外されたが取得済み）
    for item in all_results:
        if _matches(item):
            picked = dict(item)
            picked["excluded"] = False
            picked.setdefault("score_reasons", []).append("参考URL指定で先頭固定（スコアに依らず採用）")
            return [picked] + [it for it in filtered if not _matches(it)][: max(0, count - 1)]

    # 検索結果に無い → 新規エントリ作成
    parsed_domain = urlparse(canonical).netloc.lower()
    picked = {
        "rank": 0,
        "title": canonical,
        "url": canonical,
        "domain": parsed_domain,
        "snippet": "",
        "score": 999,
        "score_reasons": ["参考URL指定で先頭固定（検索結果外から手動指定）"],
        "excluded": False,
    }
    return [picked] + [it for it in filtered if not _matches(it)][: max(0, count - 1)]


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
    parser.add_argument("--output-key", help="出力先の識別キー（省略時はキーワード）")
    parser.add_argument("--scrape", action="store_true", help="取得後にscrape.pyを実行")
    parser.add_argument("--show-all", action="store_true", help="raw結果と除外理由を表示")
    parser.add_argument(
        "--reference-url",
        default="",
        help="構造参照する競合記事URL。指定すると filtered の先頭に強制配置され、scrape の articles[0] になる",
    )
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
    all_results, suggest_info = search_google(keyword, api_key, max_results=RAW_RESULT_COUNT)
    if not all_results:
        print("検索結果を取得できませんでした。")
        sys.exit(1)

    print(f"検索結果: {len(all_results)} 件取得")
    if suggest_info.get("people_also_ask"):
        print(f"  People Also Ask: {len(suggest_info['people_also_ask'])} 件")
    if suggest_info.get("related_searches"):
        print(f"  関連キーワード: {len(suggest_info['related_searches'])} 件")
    print()
    filtered = filter_urls(keyword, all_results, count=args.count)

    # reference_url が指定されたら、その URL を filtered の先頭に強制配置する。
    # （Step 2 のタグ構成設計で「1位記事」として参照されるのは articles[0]＝scrape は filtered の順序を踏襲するため）
    reference_url = (args.reference_url or "").strip()
    if reference_url:
        filtered = _ensure_reference_url_first(reference_url, filtered, all_results, args.count)
        print(f"参考URL指定: {reference_url} を先頭に配置しました")

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

    output_key = resolve_output_key(keyword, args.output_key)
    output_dir = ensure_output_dir_for_key(output_key)
    keyword_slug = keyword_to_slug(output_key)
    json_path = os.path.join(output_dir, f"{keyword_slug}_search_results.json")
    save_data = {
        "keyword": keyword,
        "output_key": output_key,
        "source": "serper_google",
        "filtered": filtered,
        "all_results": all_results,
        "suggest": suggest_info,
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
