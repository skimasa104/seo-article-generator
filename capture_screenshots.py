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
import glob
import json
import os
import re
import shutil
import sys
import time
import urllib.parse

from bs4 import BeautifulSoup

from env_utils import load_project_env
from fill_list_box import iter_candidate_clinic_h3_tags, slugify_heading
from official_site_utils import (
    extract_lookup_name_variants,
    find_cached_official_url,
    find_official_url,
    is_banned_domain,
    normalize_clinic_lookup_name,
)
from output_utils import OUTPUT_ROOT, ensure_keyword_images_dir

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
    """HTMLファイルからスクショ対象のH3見出しを抽出する"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    soup = BeautifulSoup(content, "lxml")
    return [tag.get_text(" ", strip=True) for tag in iter_candidate_clinic_h3_tags(soup)]


def get_default_urls_path(html_path: str) -> str:
    html_dir = os.path.dirname(html_path)
    keyword_slug = os.path.splitext(os.path.basename(html_path))[0].replace("_記事", "")
    return os.path.join(html_dir, f"{keyword_slug}_urls.json")


def resolve_existing_url(url_map: dict[str, str], target_name: str) -> str | None:
    reference_urls = {
        str(value).strip()
        for key, value in url_map.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and "参考元記事" in key
        and value.strip()
    }

    direct = url_map.get(target_name)
    if direct:
        if is_banned_domain(direct):
            print(f"  Ignoring cached non-official URL for {target_name}: {direct}")
        elif direct.strip() in reference_urls:
            print(f"  Ignoring reference article URL for {target_name}: {direct}")
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
            if (
                normalized_key == normalized_target
                and not is_banned_domain(value)
                and value.strip() not in reference_urls
            ):
                return value
    for normalized_target in normalized_variants:
        for _, normalized_key, value in normalized_pairs:
            if (
                (normalized_target in normalized_key or normalized_key in normalized_target)
                and not is_banned_domain(value)
                and value.strip() not in reference_urls
            ):
                return value
    return find_cached_official_url(target_name)


def build_screenshot_basename(name: str) -> str:
    """スクリーンショット画像名用に短い院名ベースを返す。"""
    base = re.split(r"\s*[|｜]\s*", name, maxsplit=1)[0].strip()
    safe_name = re.sub(r"[^\w\-]", "_", base)
    safe_name = re.sub(r"_+", "_", safe_name).strip("_")
    return safe_name or "clinic"


def iter_screenshot_basename_candidates(name: str) -> list[str]:
    candidates: list[str] = []
    raw_candidates = [name]
    raw_candidates.extend(extract_lookup_name_variants(name))

    seen = set()
    for candidate in raw_candidates:
        basename = build_screenshot_basename(candidate)
        if basename and basename not in seen:
            candidates.append(basename)
            seen.add(basename)
    return candidates


def find_reusable_screenshot(name: str, output_dir: str) -> str | None:
    current_output_dir = os.path.abspath(output_dir)

    for basename in iter_screenshot_basename_candidates(name):
        current_candidate = os.path.join(current_output_dir, f"screenshot_{basename}.png")
        if os.path.exists(current_candidate):
            return current_candidate

    matches: list[str] = []
    for basename in iter_screenshot_basename_candidates(name):
        pattern = os.path.join(OUTPUT_ROOT, "*", "images", f"screenshot_{basename}.png")
        for path in glob.glob(pattern):
            absolute = os.path.abspath(path)
            if absolute.startswith(current_output_dir + os.sep):
                continue
            matches.append(absolute)

    if not matches:
        return None

    matches = sorted(set(matches), key=lambda path: os.path.getmtime(path), reverse=True)
    return matches[0]


def reuse_screenshot_if_available(name: str, destination_path: str, output_dir: str) -> bool:
    reusable = find_reusable_screenshot(name, output_dir)
    if not reusable:
        return False

    if os.path.abspath(reusable) != os.path.abspath(destination_path):
        shutil.copy2(reusable, destination_path)
        print(f"  Reused existing screenshot: {reusable}")
    else:
        print(f"  Reusing current screenshot: {reusable}")
    return True


# ========================================
# スクリーンショット取得
# ========================================
def capture_screenshot(page, url: str, output_path: str) -> bool:
    """指定URLのスクリーンショットを取得する"""
    try:
        try:
            response = page.goto(url, wait_until="load", timeout=30000)
        except Exception:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response is not None and response.status >= 400:
            print(f"    HTTP status {response.status} for {url}")
            return False
        time.sleep(2)  # 動的コンテンツの読み込み待ち

        page_title = (page.title() or "").lower()
        if "404" in page_title or "not found" in page_title:
            print(f"    Suspicious page title for screenshot: {page_title}")
            return False

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

    def ensure_heading_id(tag, name: str) -> None:
        existing_id = (tag.get("id") or "").strip()
        if existing_id:
            return
        base_name = re.split(r"\s*[|｜]\s*", name, maxsplit=1)[0].strip()
        slug_base = slugify_heading(base_name) or "section"
        candidate = f"clinic-{slug_base}"
        suffix = 2
        while soup.find(id=candidate) is not None:
            candidate = f"clinic-{slug_base}-{suffix}"
            suffix += 1
        tag["id"] = candidate

    target_names = []
    for name in list(url_map.keys()) + list(screenshots.keys()):
        if name not in target_names:
            target_names.append(name)

    for name in target_names:
        img_path = screenshots.get(name)
        rel_path = (
            os.path.relpath(img_path, os.path.dirname(html_path))
            if img_path
            else None
        )
        official_url = (url_map.get(name) or "").strip()
        heading = soup.find("h3", string=lambda text: text and text.strip() == name)
        if heading is None:
            continue
        ensure_heading_id(heading, name)

        section_nodes = []
        node = heading.find_next_sibling()
        while node and getattr(node, "name", None) not in {"h2", "h3"}:
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

        if rel_path and screenshot_div:
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
        elif rel_path:
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

        if not official_url or official_url == "#":
            continue

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
            anchor["class"] = ["official-site-button"]
            anchor["style"] = (
                "display:inline-flex;align-items:center;justify-content:center;"
                "background:#0f6cbd;color:#fff;padding:14px 24px;border-radius:999px;"
                "text-decoration:none;font-size:15px;font-weight:bold;line-height:1.5;"
                "text-align:center;width:min(100%,420px);max-width:100%;"
            )
            anchor.string = "公式サイトを見る"
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
            f'class="official-site-button" '
            f'style="display:inline-flex;align-items:center;justify-content:center;background:#0f6cbd;color:#fff;padding:14px 24px;border-radius:999px;text-decoration:none;font-size:15px;font-weight:bold;line-height:1.5;text-align:center;width:min(100%,420px);max-width:100%;">'
            f'公式サイトを見る</a></p>'
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存スクリーンショットを再利用せず、公式サイトから撮り直す",
    )
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
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            locale="ja-JP",
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
              get: () => undefined
            });
            """
        )

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

            if not args.force and reuse_screenshot_if_available(name, filepath, output_dir):
                screenshots[name] = filepath
                results.append((name, url, True))
                continue

            print(f"  Capturing: {url}")
            success = capture_screenshot(page, url, filepath)

            if not success:
                print("  Retrying with fresh official-site lookup...")
                retry_url, _candidates = find_official_url(name)
                if retry_url and retry_url != url:
                    print(f"  Retry found: {retry_url}")
                    url = retry_url
                    url_map[name] = retry_url
                    success = capture_screenshot(page, retry_url, filepath)

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
    elif not args.skip_insert and url_map:
        insert_screenshots_into_html(args.html, screenshots, url_map)

    print("\nDone!")


if __name__ == "__main__":
    main()
