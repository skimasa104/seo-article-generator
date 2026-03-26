#!/usr/local/bin/python3.12
"""
公式サイトスクリーンショット取得スクリプト

記事HTMLからクリニック/商材名を抽出し、公式サイトのスクリーンショットを自動取得する。

使い方:
  # URLリストJSONを指定して実行
  python3.12 capture_screenshots.py --html output/aga_横浜_記事.html --urls urls.json

  # URLリストなし（Google検索で公式サイトを自動特定）
  python3.12 capture_screenshots.py --html output/aga_横浜_記事.html --keyword "AGA 横浜"

  # URLリスト自動生成（手動確認用）
  python3.12 capture_screenshots.py --html output/aga_横浜_記事.html --generate-urls

urls.json の形式:
  {
    "イースト駅前クリニック横浜院": "https://www.eastcl.com/yokohama/",
    "AGAヘアクリニック": "https://agahairclinic.or.jp/"
  }
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Error: playwright パッケージが必要です。")
    print("  pip install playwright && python -m playwright install chromium")
    sys.exit(1)


# ========================================
# 設定
# ========================================
OUTPUT_DIR = "output/images"
VIEWPORT_WIDTH = 1200
VIEWPORT_HEIGHT = 800
SCREENSHOT_TIMEOUT = 15000  # ページロードのタイムアウト (ms)


# ========================================
# HTML解析
# ========================================
def extract_h3_names(html_path: str) -> list[str]:
    """HTMLファイルからH3見出し（クリニック/商材名）を抽出する"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    h3_pattern = re.compile(r"<h3>(.*?)</h3>", re.DOTALL)
    names = []
    for match in h3_pattern.finditer(content):
        text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        # 「選び方」「FAQ」などのH3は除外（H2直下のH3のみ対象）
        # アフィカセットのプレースホルダーが直後にあるH3のみ対象にする
        start = match.end()
        next_content = content[start:start + 200]
        if "アフィカセット" in next_content or "後で作成" in next_content:
            names.append(text)

    return names


# ========================================
# URL自動検索
# ========================================
def search_official_url(page, name: str) -> str | None:
    """Google検索でクリニック/商材の公式サイトURLを取得する"""
    query = f"{name} 公式サイト"
    search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"

    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=SCREENSHOT_TIMEOUT)
        time.sleep(2)

        # 検索結果から最初のリンクを取得
        # Google検索結果のリンク要素を取得
        links = page.query_selector_all("div#search a[href^='http']")

        for link in links:
            href = link.get_attribute("href")
            if not href:
                continue
            # 広告やGoogle自身のリンクを除外
            if "google.com" in href or "youtube.com" in href:
                continue
            # Wikipedia等も除外
            if "wikipedia.org" in href:
                continue
            return href

    except Exception as e:
        print(f"    Search error for {name}: {e}")

    return None


# ========================================
# スクリーンショット取得
# ========================================
def capture_screenshot(page, url: str, output_path: str) -> bool:
    """指定URLのスクリーンショットを取得する"""
    try:
        page.goto(url, wait_until="load", timeout=30000)
        time.sleep(2)  # 動的コンテンツの読み込み待ち

        # Cookie同意バナー等を閉じる試み
        close_selectors = [
            "button:has-text('同意')",
            "button:has-text('承諾')",
            "button:has-text('Accept')",
            "button:has-text('OK')",
            "button:has-text('閉じる')",
            "[class*='cookie'] button",
            "[class*='consent'] button",
            "[id*='cookie'] button",
        ]
        for selector in close_selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    time.sleep(0.5)
                    break
            except Exception:
                pass

        # スクリーンショット取得（ファーストビュー）
        page.screenshot(path=output_path, clip={
            "x": 0,
            "y": 0,
            "width": VIEWPORT_WIDTH,
            "height": VIEWPORT_HEIGHT,
        })

        return True

    except Exception as e:
        print(f"    Screenshot error: {e}")
        return False


# ========================================
# HTMLへの挿入
# ========================================
def insert_screenshots_into_html(html_path: str, screenshots: dict[str, str]):
    """スクリーンショットをHTMLのアフィカセット部分に挿入する"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # アフィカセットプレースホルダーを全て抽出
    all_placeholders = re.findall(
        r"\{\{後で作成:アフィカセット\s*[—\-]\s*(.+?)\s*\}\}", content
    )

    for name, img_path in screenshots.items():
        # 相対パスに変換
        rel_path = os.path.relpath(img_path, os.path.dirname(html_path))

        img_tag = (
            f'<div class="clinic-screenshot" style="margin:1em 0;border:1px solid #ddd;border-radius:4px;overflow:hidden;">\n'
            f'<img src="{rel_path}" alt="{name}の公式サイト" '
            f'width="{VIEWPORT_WIDTH}" height="{VIEWPORT_HEIGHT}" loading="lazy" '
            f'style="width:100%;height:auto;display:block;">\n'
            f'</div>'
        )

        # 完全一致で検索
        placeholder_pattern = re.compile(
            r"\{\{後で作成:アフィカセット\s*[—\-]\s*" + re.escape(name) + r"\s*\}\}"
        )

        if placeholder_pattern.search(content):
            content = placeholder_pattern.sub(img_tag, content)
            print(f"  Inserted screenshot for: {name}")
        else:
            # H3名とプレースホルダー名が異なる場合、部分一致で探す
            # 例: H3「AGAヘアクリニック（AHCメディカルサロン横浜）」
            #     プレースホルダー「AGAヘアクリニック」
            matched = False
            for ph_name in all_placeholders:
                if ph_name in name or name in ph_name:
                    fallback_pattern = re.compile(
                        r"\{\{後で作成:アフィカセット\s*[—\-]\s*"
                        + re.escape(ph_name) + r"\s*\}\}"
                    )
                    if fallback_pattern.search(content):
                        content = fallback_pattern.sub(img_tag, content)
                        print(f"  Inserted screenshot for: {name} (matched placeholder: {ph_name})")
                        matched = True
                        break
            if not matched:
                print(f"  Warning: Placeholder not found for: {name}")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)


# ========================================
# メイン
# ========================================
def main():
    parser = argparse.ArgumentParser(description="公式サイトスクリーンショット取得")
    parser.add_argument("--html", required=True, help="記事HTMLファイルのパス")
    parser.add_argument("--urls", type=str, help="URLリストJSONファイルのパス")
    parser.add_argument("--keyword", type=str, help="検索キーワード（URL自動検索時に使用）")
    parser.add_argument("--generate-urls", action="store_true",
                        help="URLリストのテンプレートJSONを生成する")
    parser.add_argument("--skip-insert", action="store_true",
                        help="HTMLへの画像挿入をスキップ")
    parser.add_argument("--only", type=str, help="特定のクリニック名のみ処理")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    # H3見出しからクリニック名を抽出
    names = extract_h3_names(args.html)
    print(f"Found {len(names)} clinics/services:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")

    if not names:
        print("No clinic/service names found.")
        sys.exit(0)

    # URLリストテンプレート生成モード
    if args.generate_urls:
        template = {name: "" for name in names}
        keyword_slug = os.path.splitext(os.path.basename(args.html))[0]
        output_json = f"output/{keyword_slug}_urls.json"
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
        print(f"\nURL template generated: {output_json}")
        print("Fill in the URLs and run again with --urls option.")
        return

    # URLリスト読み込み
    url_map = {}
    if args.urls:
        with open(args.urls, "r", encoding="utf-8") as f:
            url_map = json.load(f)

    # 出力ディレクトリ作成
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Playwright起動
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        screenshots = {}
        results = []

        for name in names:
            if args.only and args.only != name:
                continue

            print(f"\n--- {name} ---")

            # URL取得
            url = url_map.get(name)

            if not url:
                # URLリストにない場合はGoogle検索で自動取得
                print(f"  Searching for official site...")
                url = search_official_url(page, name)
                if url:
                    print(f"  Found: {url}")
                    url_map[name] = url
                else:
                    print(f"  Could not find official site. Skipping.")
                    results.append((name, None, False))
                    continue

            # スクリーンショット取得
            safe_name = re.sub(r'[^\w\-]', '_', name)
            filename = f"screenshot_{safe_name}.png"
            filepath = os.path.join(OUTPUT_DIR, filename)

            print(f"  Capturing: {url}")
            success = capture_screenshot(page, url, filepath)

            if success:
                print(f"  Saved: {filepath}")
                screenshots[name] = filepath
            else:
                print(f"  Failed to capture screenshot.")

            results.append((name, url, success))
            time.sleep(1)  # レートリミット対策

        browser.close()

    # 結果サマリー
    print("\n" + "=" * 60)
    print("Results:")
    for name, url, success in results:
        status = "OK" if success else "FAILED"
        print(f"  [{status}] {name}")
        if url:
            print(f"          {url}")

    # 自動検索で見つけたURLを保存
    if not args.urls and url_map:
        keyword_slug = os.path.splitext(os.path.basename(args.html))[0]
        urls_output = f"output/{keyword_slug}_urls.json"
        with open(urls_output, "w", encoding="utf-8") as f:
            json.dump(url_map, f, ensure_ascii=False, indent=2)
        print(f"\nDiscovered URLs saved to: {urls_output}")
        print("You can re-run with --urls to skip search next time.")

    # HTML挿入
    if not args.skip_insert and screenshots:
        insert_screenshots_into_html(args.html, screenshots)

    print("\nDone!")


if __name__ == "__main__":
    main()
