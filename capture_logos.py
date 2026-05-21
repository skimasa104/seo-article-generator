#!/usr/local/bin/python3.12
"""
公式サイトロゴ取得スクリプト

記事HTMLからクリニック名を抽出し、公式サイトからロゴ画像を自動取得する。
取得済みロゴは output/.logo_cache.json で全記事間で共有・再利用される。

使い方:
  # urls.json を指定して実行
  python3.12 capture_logos.py --html output/aga_横浜/aga_横浜_記事.html --urls urls.json

  # urls.json なし（同じkeywordフォルダの urls.json を優先利用）
  python3.12 capture_logos.py --html output/aga_横浜/aga_横浜_記事.html

ロジック:
  1. clinic-list-tbl の .cl-cell から対象クリニック名を抽出（なければ H3 全体）
  2. 各クリニックについて:
     a. キャッシュ（output/.logo_cache.json）を確認
     b. ヒット → 既存ファイルを当該記事の images/ にコピー
     c. ミス → 公式サイトを Playwright で開き、ロゴ要素を抽出
        - 候補セレクタを優先順位で試行
        - 幅/アスペクト比/URL でフィルタ
        - 失敗時はヘッダー領域スクショで代替
     d. 取得した画像を images/logo_{識別子}.png として保存
     e. キャッシュ更新
  3. clinic-list-tbl の <div class="cl-logo-text">…</div> を <img> に差し替え
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from io import BytesIO

import requests
from bs4 import BeautifulSoup

from env_utils import load_project_env
from official_site_utils import (
    extract_lookup_name_variants,
    normalize_clinic_lookup_name,
)
from output_utils import OUTPUT_ROOT, ensure_keyword_images_dir

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Error: playwright パッケージが必要です。")
    print("  pip install playwright && python -m playwright install chromium")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    Image = None


load_project_env()


# ========================================
# 設定
# ========================================
LOGO_CACHE_PATH = os.path.join(OUTPUT_ROOT, ".logo_cache.json")
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900
PAGE_LOAD_TIMEOUT = 30000  # ms

# ロゴ候補セレクタ（優先順位順）
LOGO_SELECTORS = [
    # 高精度
    "header a.custom-logo-link img",
    "header img.custom-logo",
    "header .site-logo img",
    "header .l-header__logo img",
    "header .p-header__logo img",
    "header .header__logo img",
    "header h1 img",
    "header .logo img",
    "header [class*='logo' i] img",
    # 中精度
    "[class*='site-logo' i] img",
    "[class*='header-logo' i] img",
    "a[class*='logo' i] img",
    "header a img:first-of-type",
    # 汎用フォールバック
    "header img[alt*='ロゴ' i]",
    "header img[alt*='logo' i]",
    "img[alt*='ロゴ' i][src*='logo' i]",
]

# ロゴ判定の数値基準
MIN_WIDTH = 60
MAX_WIDTH = 600
MIN_HEIGHT = 20
MAX_HEIGHT = 250
MIN_ASPECT = 0.8   # w/h
MAX_ASPECT = 8.0

# 除外パターン（ファイル名 or alt text）
REJECT_PATTERNS = re.compile(
    r"(favicon|spinner|loading|banner|hero|kv|mainvisual|mv-|cert(ificate)?|badge|"
    r"facebook|twitter|instagram|line|youtube|sns|share)",
    re.IGNORECASE,
)


# ========================================
# キャッシュ
# ========================================
def load_logo_cache() -> dict:
    if os.path.exists(LOGO_CACHE_PATH):
        try:
            with open(LOGO_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_logo_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(LOGO_CACHE_PATH), exist_ok=True)
    with open(LOGO_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def cache_lookup(cache: dict, clinic_name: str) -> dict | None:
    """クリニック名（バリアント含む）でキャッシュを検索。ヒット時はエントリ dict を返す。"""
    candidates = [clinic_name]
    candidates.extend(extract_lookup_name_variants(clinic_name))
    candidates.append(normalize_clinic_lookup_name(clinic_name))

    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if cand in cache:
            return cache[cand]
        normalized = _strip_ws(cand)
        for key, entry in cache.items():
            if _strip_ws(key) == normalized:
                return entry
    return None


# ========================================
# HTML 解析
# ========================================
def extract_clinic_list_targets(html: str) -> list[str]:
    """clinic-list-tbl の .cl-cell から対象クリニック名を抽出する。
    なければ clinic-index-box / H3 から拾う。"""
    soup = BeautifulSoup(html, "lxml")

    targets: list[str] = []

    # 1) clinic-list-tbl > .cl-cell > .cl-name
    for cell in soup.select(".clinic-list-tbl .cl-cell"):
        name_tag = cell.select_one(".cl-name a") or cell.select_one(".cl-name")
        if name_tag:
            text = re.sub(r"\s+", " ", name_tag.get_text(" ", strip=True)).strip()
            if text and text not in targets:
                targets.append(text)
    if targets:
        return targets

    # 2) clinic-index-box のアンカー
    for anchor in soup.select(".clinic-index-box a"):
        text = anchor.get_text(" ", strip=True)
        if text and text not in targets:
            targets.append(text)
    return targets


def _strip_ws(text: str) -> str:
    """正規化に加えて全角・半角スペースも除去（br由来の空白対策）。"""
    if not text:
        return ""
    base = normalize_clinic_lookup_name(text)
    return re.sub(r"[\s　]+", "", base)


def find_clinic_url(name: str, url_map: dict[str, str]) -> str | None:
    if not name:
        return None
    if name in url_map:
        return url_map[name]
    normalized_target = _strip_ws(name)
    for key, value in url_map.items():
        if _strip_ws(key) == normalized_target:
            return value
    # 部分一致
    for key, value in url_map.items():
        if not value:
            continue
        nk = _strip_ws(key)
        if nk and (nk in normalized_target or normalized_target in nk):
            return value
    return None


# ========================================
# Playwright によるロゴ抽出
# ========================================
def absolutize_url(base_url: str, src: str) -> str:
    if not src:
        return ""
    if src.startswith("data:"):
        return src
    return urllib.parse.urljoin(base_url, src)


def evaluate_candidate(box: dict, src: str, alt: str) -> tuple[bool, str]:
    """候補画像のサイズ・URL・alt を見て採用可否を判定する。"""
    if REJECT_PATTERNS.search(src or "") or REJECT_PATTERNS.search(alt or ""):
        return False, "reject_pattern"

    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)
    if width < MIN_WIDTH or width > MAX_WIDTH:
        return False, f"width_out_of_range({width:.0f})"
    if height < MIN_HEIGHT or height > MAX_HEIGHT:
        return False, f"height_out_of_range({height:.0f})"
    if height <= 0:
        return False, "no_height"
    aspect = width / height
    if aspect < MIN_ASPECT or aspect > MAX_ASPECT:
        return False, f"aspect_out_of_range({aspect:.2f})"
    return True, "ok"


def fetch_image_bytes(url: str, base_url: str) -> bytes | None:
    """画像URLからバイト列を取得する。data: スキームと相対URLにも対応。"""
    if url.startswith("data:"):
        match = re.match(r"data:[^;]+;base64,(.+)$", url)
        if not match:
            return None
        import base64
        try:
            return base64.b64decode(match.group(1))
        except Exception:
            return None
    abs_url = absolutize_url(base_url, url)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": base_url,
        }
        resp = requests.get(abs_url, headers=headers, timeout=15, stream=True)
        if resp.status_code != 200:
            return None
        return resp.content
    except Exception as e:
        print(f"    fetch_image_bytes error: {e}")
        return None


def normalize_to_png(image_bytes: bytes) -> bytes | None:
    """画像バイト列を PNG に正規化する。失敗したら元のまま返す。"""
    if Image is None:
        return image_bytes
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return image_bytes


def capture_logo_from_page(page, official_url: str) -> tuple[bytes | None, str]:
    """ページから優先順位付きでロゴ画像を抽出する。戻り値: (画像バイト列, 採用方法)"""
    try:
        try:
            page.goto(official_url, wait_until="load", timeout=PAGE_LOAD_TIMEOUT)
        except Exception:
            page.goto(official_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        time.sleep(1.5)
    except Exception as e:
        print(f"    page.goto error: {e}")
        return None, "load_failed"

    # Cookie同意バナーの除去（任意）
    for selector in [
        "button:has-text('同意')", "button:has-text('Accept')", "button:has-text('OK')",
        "[class*='cookie'] button", "[class*='consent'] button",
    ]:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(0.3)
                break
        except Exception:
            pass

    # 1) セレクタ優先で候補を試す
    for selector in LOGO_SELECTORS:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        for el in elements[:3]:
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box() or {}
                src = el.get_attribute("src") or el.get_attribute("data-src") or ""
                alt = el.get_attribute("alt") or ""
                ok, reason = evaluate_candidate(box, src, alt)
                if not ok:
                    continue

                # SVG または data: スキームの場合は要素スクショで PNG 化（WP は SVG 拒否することが多い）
                src_lower = (src or "").lower()
                use_screenshot = (
                    src_lower.endswith(".svg")
                    or "image/svg" in src_lower
                    or src_lower.startswith("data:image/svg")
                )

                if use_screenshot:
                    try:
                        img_bytes = el.screenshot(omit_background=True)
                        if img_bytes and len(img_bytes) >= 200:
                            print(f"    Matched selector: {selector} (alt='{alt[:40]}', SVG→element screenshot)")
                            return img_bytes, f"selector:{selector}+element_screenshot"
                    except Exception as e:
                        print(f"    element.screenshot failed: {e}")

                image_bytes = fetch_image_bytes(src, official_url)
                if not image_bytes or len(image_bytes) < 200:
                    # ダウンロード失敗 → 要素スクショで救済
                    try:
                        img_bytes = el.screenshot(omit_background=True)
                        if img_bytes and len(img_bytes) >= 200:
                            print(f"    Matched selector: {selector} (download failed→element screenshot)")
                            return img_bytes, f"selector:{selector}+element_screenshot"
                    except Exception:
                        pass
                    continue

                # SVG コンテンツが PNG 拡張子で配信されているケースの検出
                if image_bytes[:5] in (b"<?xml", b"<svg "):
                    try:
                        img_bytes = el.screenshot(omit_background=True)
                        if img_bytes and len(img_bytes) >= 200:
                            print(f"    Matched selector: {selector} (alt='{alt[:40]}', SVG content→element screenshot)")
                            return img_bytes, f"selector:{selector}+element_screenshot"
                    except Exception:
                        pass

                normalized = normalize_to_png(image_bytes) or image_bytes
                print(f"    Matched selector: {selector} (alt='{alt[:40]}')")
                return normalized, f"selector:{selector}"
            except Exception:
                continue

    # 2) ヘッダー領域スクショで代替
    print("    No good <img> logo found, falling back to header screenshot")
    try:
        header = page.query_selector("header") or page.query_selector("[class*='header' i]")
        if header is not None and header.is_visible():
            box = header.bounding_box() or {}
            if box.get("height", 0) > 0:
                clip = {
                    "x": max(0, box.get("x", 0)),
                    "y": max(0, box.get("y", 0)),
                    "width": min(page.viewport_size["width"], box.get("width", 600)),
                    "height": min(180, box.get("height", 100)),
                }
                buf = page.screenshot(clip=clip)
                return buf, "header_screenshot"
    except Exception as e:
        print(f"    header screenshot error: {e}")

    # 3) 最終フォールバック: ページ上部 1280x100 を切り抜き
    try:
        buf = page.screenshot(clip={"x": 0, "y": 0, "width": VIEWPORT_WIDTH, "height": 100})
        return buf, "page_top_crop"
    except Exception:
        pass
    return None, "all_failed"


# ========================================
# 識別子・ファイル名
# ========================================
def make_logo_filename(name: str) -> str:
    base = re.split(r"\s*[|｜（(]", name, maxsplit=1)[0].strip()
    base = re.sub(r"[^\w\-]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    return f"logo_{base or 'clinic'}.png"


def normalize_cache_key(name: str) -> str:
    """キャッシュ保存に使う正規化キー。記事間で共通化されるよう代表名にする。"""
    # 「（）」内の支店名等を除去して代表ブランド名にする
    base = re.split(r"\s*[|｜（(]", name, maxsplit=1)[0].strip()
    return base or name


# ========================================
# メイン取得処理
# ========================================
def acquire_logo_for_clinic(
    *,
    clinic_name: str,
    official_url: str | None,
    output_images_dir: str,
    cache: dict,
    page,
) -> dict | None:
    """1院分のロゴを取得（キャッシュ優先）。戻り値: {filename, src, method, official_url}"""
    print(f"\n--- {clinic_name} ---")

    cache_key = normalize_cache_key(clinic_name)
    cached = cache_lookup(cache, cache_key) or cache_lookup(cache, clinic_name)
    target_filename = make_logo_filename(cache_key)
    dest_path = os.path.join(output_images_dir, target_filename)

    # キャッシュヒット
    if cached and cached.get("stored_at"):
        stored_path = cached["stored_at"]
        if not os.path.isabs(stored_path):
            stored_path = os.path.join(os.path.dirname(LOGO_CACHE_PATH), os.pardir, stored_path)
            stored_path = os.path.abspath(stored_path)
        if os.path.exists(stored_path):
            if os.path.abspath(stored_path) != os.path.abspath(dest_path):
                shutil.copy2(stored_path, dest_path)
                print(f"  Cache HIT → copied from {stored_path}")
            else:
                print(f"  Cache HIT (already in place)")
            return {
                "filename": os.path.basename(dest_path),
                "stored_at": dest_path,
                "official_url": cached.get("official_url"),
                "method": cached.get("method", "cache"),
            }

    if not official_url:
        print("  Skip: official URL not provided and not cached")
        return None

    # 取得
    image_bytes, method = capture_logo_from_page(page, official_url)
    if not image_bytes:
        print(f"  Failed to acquire logo: {method}")
        return None

    with open(dest_path, "wb") as f:
        f.write(image_bytes)
    print(f"  Saved logo: {dest_path} ({len(image_bytes):,} bytes, method={method})")

    entry = {
        "filename": target_filename,
        "stored_at": os.path.relpath(dest_path, os.path.dirname(LOGO_CACHE_PATH) + "/.."),
        "official_url": official_url,
        "method": method,
        "fetched_at": time.strftime("%Y-%m-%d"),
    }
    # 二重キーで保存（元名と正規化キー）
    cache[cache_key] = entry
    if clinic_name != cache_key:
        cache[clinic_name] = entry
    return {
        "filename": target_filename,
        "stored_at": dest_path,
        "official_url": official_url,
        "method": method,
    }


# ========================================
# HTML への画像反映
# ========================================
def insert_logos_into_html(html_path: str, logo_map: dict[str, dict]) -> int:
    """clinic-list-tbl 内の cl-logo-text を <img> に差し替える。差し替えた件数を返す。"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    soup = BeautifulSoup(content, "lxml")
    replaced = 0

    for cell in soup.select(".clinic-list-tbl .cl-cell"):
        name_tag = cell.select_one(".cl-name a") or cell.select_one(".cl-name")
        if not name_tag:
            continue
        clinic_name = re.sub(r"\s+", " ", name_tag.get_text(" ", strip=True)).strip()

        info = logo_map.get(clinic_name)
        if not info:
            # 部分一致で探す（スペース無視）
            normalized = _strip_ws(clinic_name)
            for key, value in logo_map.items():
                if _strip_ws(key) == normalized:
                    info = value
                    break
        if not info:
            continue

        logo_box = cell.select_one(".cl-logo-box")
        if logo_box is None:
            continue

        rel_path = os.path.relpath(info["stored_at"], os.path.dirname(html_path))

        # 既に img があれば src 更新、なければ作成
        existing_img = logo_box.find("img")
        if existing_img is not None:
            existing_img["src"] = rel_path
            existing_img["alt"] = f"{clinic_name} ロゴ"
            existing_img["loading"] = "lazy"
        else:
            logo_box.clear()
            new_img = soup.new_tag("img")
            new_img["src"] = rel_path
            new_img["alt"] = f"{clinic_name} ロゴ"
            new_img["loading"] = "lazy"
            logo_box.append(new_img)
        replaced += 1

    if soup.body is not None:
        updated = soup.body.decode_contents().strip()
    else:
        updated = str(soup)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(updated)

    return replaced


# ========================================
# メイン
# ========================================
def get_default_urls_path(html_path: str) -> str:
    html_dir = os.path.dirname(html_path)
    keyword_slug = os.path.splitext(os.path.basename(html_path))[0].replace("_記事", "")
    return os.path.join(html_dir, f"{keyword_slug}_urls.json")


def main():
    parser = argparse.ArgumentParser(description="公式サイトロゴ取得")
    parser.add_argument("--html", required=True, help="記事HTMLファイルのパス")
    parser.add_argument("--urls", type=str, help="URLリストJSONファイルのパス（省略時は自動探索）")
    parser.add_argument("--skip-insert", action="store_true",
                        help="HTMLへの画像挿入をスキップ（取得のみ）")
    parser.add_argument("--only", type=str, help="特定クリニック名のみ処理（部分一致）")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    # URLリスト読み込み
    urls_path = args.urls or get_default_urls_path(args.html)
    url_map: dict[str, str] = {}
    if os.path.exists(urls_path):
        with open(urls_path, "r", encoding="utf-8") as f:
            url_map = json.load(f)
        print(f"URLs loaded: {urls_path} ({len(url_map)} entries)")
    else:
        print(f"Warning: urls.json not found ({urls_path}). Cache lookup only.")

    # 対象クリニック抽出
    with open(args.html, "r", encoding="utf-8") as f:
        html_content = f.read()
    targets = extract_clinic_list_targets(html_content)
    if args.only:
        targets = [t for t in targets if args.only in t]
    if not targets:
        print("No clinic targets found in HTML.")
        sys.exit(0)

    print(f"\nTargets ({len(targets)}):")
    for t in targets:
        print(f"  - {t}")

    images_dir = os.path.join(os.path.dirname(args.html), "images")
    os.makedirs(images_dir, exist_ok=True)

    cache = load_logo_cache()
    logo_map: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for name in targets:
            official_url = find_clinic_url(name, url_map)
            info = acquire_logo_for_clinic(
                clinic_name=name,
                official_url=official_url,
                output_images_dir=images_dir,
                cache=cache,
                page=page,
            )
            if info:
                logo_map[name] = info

        browser.close()

    save_logo_cache(cache)
    print(f"\nLogo cache updated: {LOGO_CACHE_PATH}")
    print(f"Acquired logos: {len(logo_map)}/{len(targets)}")

    if args.skip_insert:
        return

    replaced = insert_logos_into_html(args.html, logo_map)
    print(f"HTML updated: {replaced} cl-logo-text replaced with <img>")


if __name__ == "__main__":
    main()
