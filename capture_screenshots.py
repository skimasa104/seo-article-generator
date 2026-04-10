#!/usr/local/bin/python3.12
"""
公式サイトスクリーンショット取得スクリプト

記事HTMLからクリニック/商材名を抽出し、公式サイトのスクリーンショットを自動取得する。

使い方:
  # URLリストJSONを指定して実行
  python3.12 capture_screenshots.py --html output/aga_横浜/aga_横浜_記事.html --urls urls.json

  # URLリストなし（同じkeywordフォルダの urls.json を優先利用し、なければ公式サイトを自動特定）
  python3.12 capture_screenshots.py --html output/aga_横浜/aga_横浜_記事.html

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

from bs4 import BeautifulSoup

from article_audit import extract_clinic_names
from env_utils import load_project_env
from official_site_utils import (
    extract_lookup_name_variants,
    find_official_url,
    is_banned_domain,
    normalize_clinic_lookup_name,
)
from output_utils import ensure_keyword_images_dir

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Error: playwright パッケージが必要です。")
    print("  pip install playwright && python -m playwright install chromium")
    sys.exit(1)


# ========================================
# 設定
# ========================================
VIEWPORT_WIDTH = 1200
VIEWPORT_HEIGHT = 800
SCREENSHOT_TIMEOUT = 15000  # ページロードのタイムアウト (ms)
load_project_env()


# ========================================
# HTML解析
# ========================================
def extract_h3_names(html_path: str) -> list[str]:
    """HTMLファイルからクリニックH3見出しのみ抽出する"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return extract_clinic_names(content)


def get_default_urls_path(html_path: str) -> str:
    html_dir = os.path.dirname(html_path)
    keyword_slug = os.path.splitext(os.path.basename(html_path))[0].replace("_記事", "")
    return os.path.join(html_dir, f"{keyword_slug}_urls.json")


def resolve_existing_url(url_map: dict[str, str], target_name: str) -> str | None:
    direct = url_map.get(target_name)
    if direct:
        if is_banned_domain(direct):
            print(f"  Ignoring cached non-official URL for {target_name}: {direct}")
        else:
            return direct

    target_variants = extract_lookup_name_variants(target_name)
    normalized_variants = [normalize_clinic_lookup_name(variant) for variant in target_variants]
    normalized_pairs = [
        (key, normalize_clinic_lookup_name(key), value)
        for key, value in url_map.items()
        if value
    ]
    for normalized_target in normalized_variants:
        for _, normalized_key, value in normalized_pairs:
            if normalized_key == normalized_target and not is_banned_domain(value):
                return value
    for normalized_target in normalized_variants:
        for _, normalized_key, value in normalized_pairs:
            if (
                (normalized_target in normalized_key or normalized_key in normalized_target)
                and not is_banned_domain(value)
            ):
                return value
    return None


def build_screenshot_basename(name: str) -> str:
    """スクリーンショット画像名用に短い院名ベースを返す。"""
    base = re.split(r"\s*[|｜]\s*", name, maxsplit=1)[0].strip()
    safe_name = re.sub(r"[^\w\-]", "_", base)
    safe_name = re.sub(r"_+", "_", safe_name).strip("_")
    return safe_name or "clinic"


# ========================================
# スクリーンショット取得
# ========================================
def capture_screenshot(page, url: str, output_path: str) -> bool:
    """指定URLのスクリーンショットを取得する"""
    try:
        try:
            page.goto(url, wait_until="load", timeout=30000)
        except Exception:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
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
def insert_screenshots_into_html(html_path: str, screenshots: dict[str, str], url_map: dict[str, str]):
    """スクリーンショットのsrc更新と公式サイトボタン差し替えを行う。"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    soup = BeautifulSoup(content, "lxml")

    for name, img_path in screenshots.items():
        rel_path = os.path.relpath(img_path, os.path.dirname(html_path))
        official_url = url_map.get(name, "#")
        heading = soup.find("h3", string=lambda text: text and text.strip() == name)
        if heading is None:
            continue

        section_nodes = []
        node = heading.find_next_sibling()
        while node and not (getattr(node, "name", None) == "h3" and (node.get("id") or "").startswith("clinic-")) and getattr(node, "name", None) != "h2":
            section_nodes.append(node)
            node = node.find_next_sibling()

        screenshot_div = None
        button_wrap = None
        placeholder_node = None
        for section_node in section_nodes:
            if getattr(section_node, "name", None) == "div" and "clinic-screenshot" in (section_node.get("class") or []):
                screenshot_div = section_node
            if getattr(section_node, "name", None) == "p" and "official-site-button-wrap" in (section_node.get("class") or []):
                button_wrap = section_node
            if getattr(section_node, "string", None) and "後で作成:アフィカセット" in section_node.string:
                placeholder_node = section_node

        if screenshot_div:
            img = screenshot_div.find("img")
            if img is None:
                img = soup.new_tag("img")
                screenshot_div.append(img)
            img["src"] = rel_path
            img["alt"] = f"{name}の公式サイト"
            img["width"] = "1200"
            img["height"] = "800"
            img["loading"] = "lazy"
            print(f"  Updated screenshot path for: {name}")
        else:
            screenshot_div = soup.new_tag("div")
            screenshot_div["class"] = ["clinic-screenshot"]
            img = soup.new_tag("img")
            img["src"] = rel_path
            img["alt"] = f"{name}の公式サイト"
            img["width"] = "1200"
            img["height"] = "800"
            img["loading"] = "lazy"
            screenshot_div.append(img)
            if button_wrap is not None:
                button_wrap.insert_before(screenshot_div)
            else:
                heading.insert_after(screenshot_div)
            print(f"  Inserted screenshot block for: {name}")

        if button_wrap is None and placeholder_node is not None:
            button_wrap = soup.new_tag("p")
            button_wrap["class"] = ["official-site-button-wrap"]
            button_wrap["style"] = "margin:1em 0 1.5em;text-align:center;"
            placeholder_node.replace_with(button_wrap)

        if button_wrap is None:
            button_wrap = soup.new_tag("p")
            button_wrap["class"] = ["official-site-button-wrap"]
            button_wrap["style"] = "margin:1em 0 1.5em;text-align:center;"
            if screenshot_div is not None:
                screenshot_div.insert_after(button_wrap)
            else:
                heading.insert_after(button_wrap)

        if button_wrap is not None:
            button_wrap.clear()
            anchor = soup.new_tag("a")
            anchor["href"] = official_url
            anchor["target"] = "_blank"
            anchor["rel"] = "noopener noreferrer sponsored"
            anchor["style"] = "display:inline-block;background:#0f6cbd;color:#fff;padding:14px 32px;border-radius:999px;text-decoration:none;font-size:15px;font-weight:bold;"
            anchor.string = f"{name}の公式サイトを見る"
            button_wrap.append(anchor)
            print(f"  Updated official site button for: {name}")

    if soup.body is not None:
        updated_html = soup.body.decode_contents().strip()
    else:
        updated_html = str(soup)

    # 旧アフィカセット用プレースホルダーが残る記事との後方互換
    placeholder_names = re.findall(r"\{\{後で作成:アフィカセット\s*[—\-]\s*(.+?)\s*\}\}", updated_html)
    for placeholder_name in placeholder_names:
        matched_name = None
        candidate_names = sorted(set(list(url_map.keys()) + list(screenshots.keys())), key=len, reverse=True)
        for name in candidate_names:
            if placeholder_name in name or name in placeholder_name:
                matched_name = name
                break
        if not matched_name:
            continue

        official_url = url_map.get(matched_name)
        if not official_url:
            continue

        button_html = (
            f'<p class="official-site-button-wrap" style="margin:1em 0 1.5em;text-align:center;">'
            f'<a href="{official_url}" target="_blank" rel="noopener noreferrer sponsored" '
            f'style="display:inline-block;background:#0f6cbd;color:#fff;padding:14px 32px;border-radius:999px;text-decoration:none;font-size:15px;font-weight:bold;">'
            f'{matched_name}の公式サイトを見る</a></p>'
        )
        pattern = re.compile(
            r"\{\{後で作成:アフィカセット\s*[—\-]\s*" + re.escape(placeholder_name) + r"\s*\}\}"
        )
        updated_html = pattern.sub(button_html, updated_html)
        print(f"  Replaced affiliate placeholder: {placeholder_name} -> {matched_name}")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(updated_html)


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
        html_dir = os.path.dirname(args.html)
        keyword_slug = os.path.splitext(os.path.basename(args.html))[0].replace("_記事", "")
        output_json = os.path.join(html_dir, f"{keyword_slug}_urls.json")
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
        print(f"\nURL template generated: {output_json}")
        print("Fill in the URLs and run again with --urls option.")
        return

    url_map = {}
    urls_path = args.urls or get_default_urls_path(args.html)
    if os.path.exists(urls_path):
        with open(urls_path, "r", encoding="utf-8") as f:
            url_map = json.load(f)
        print(f"\nLoaded URL map: {urls_path}")

    # 出力ディレクトリ作成
    html_dir = os.path.dirname(args.html)
    keyword = os.path.splitext(os.path.basename(args.html))[0].replace("_記事", "").replace("_", " ")
    output_dir = ensure_keyword_images_dir(keyword)

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
            url = resolve_existing_url(url_map, name)
            if url:
                url_map[name] = url

            if not url:
                # URLリストにない場合はGoogle検索で自動取得
                print(f"  Searching for official site...")
                url, candidates = find_official_url(name)
                if url:
                    print(f"  Found: {url}")
                    url_map[name] = url
                else:
                    if candidates:
                        best = candidates[0]
                        print(f"  Could not confidently identify official site. Best guess was skipped: {best.get('url')} (score={best.get('score')})")
                    else:
                        print(f"  Could not find official site. Skipping.")
                    results.append((name, None, False))
                    continue

            # スクリーンショット取得
            safe_name = build_screenshot_basename(name)
            filename = f"screenshot_{safe_name}.png"
            filepath = os.path.join(output_dir, filename)

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
    if url_map:
        urls_output = get_default_urls_path(args.html)
        with open(urls_output, "w", encoding="utf-8") as f:
            json.dump(url_map, f, ensure_ascii=False, indent=2)
        print(f"\nDiscovered URLs saved to: {urls_output}")
        if not args.urls:
            print("You can re-run with --urls to skip search next time.")

    # HTML挿入
    if not args.skip_insert and screenshots:
        insert_screenshots_into_html(args.html, screenshots, url_map)

    print("\nDone!")


if __name__ == "__main__":
    main()
