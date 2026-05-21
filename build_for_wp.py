#!/usr/local/bin/python3.12
"""
記事HTML → WordPress手動投稿用HTML（_記事_for-wp.html）生成スクリプト

WordPress管理画面の「コードエディター」にそのまま貼り付けられる形式に変換する:
  1. output/article-common.css を <style> でインライン化（先頭にマーカー付きで配置）
  2. output/article-common.js を <script> でインライン化（CSSの直後）
  3. images/foo.png のローカル参照を output/.image_url_cache.json から WordPress 配信URLに差し替え
     - キャッシュにないものはローカルパスのまま残す（ユーザーが手動アップロード後に置換する想定）

入力: <article-folder>/<slug>_記事.html
出力: <article-folder>/<slug>_記事_for-wp.html

使い方:
  python3 build_for_wp.py --html output/AGA_名古屋__nandemo_v1/AGA_名古屋__nandemo_v1_記事.html

  # フォルダ単位で全 _記事.html を一括処理
  python3 build_for_wp.py --dir output/AGA_名古屋__nandemo_v1
  python3 build_for_wp.py --dir output  # output 配下を再帰的に走査

既存の _記事_for-wp.html は上書きされる。
ネット通信は一切行わない（キャッシュ済みURLのみ使用）。
"""

import argparse
import glob
import json
import os
import re
import sys

from bs4 import BeautifulSoup
from variant_utils import inline_all_nandemo_shortcodes

OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
COMMON_CSS_PATH = os.path.join(OUTPUT_ROOT, "article-common.css")
COMMON_JS_PATH = os.path.join(OUTPUT_ROOT, "article-common.js")
CACHE_FILE = os.path.join(OUTPUT_ROOT, ".image_url_cache.json")
ARTICLE_SCOPE_CLASS = "ndm-article"
ARTICLE_SCOPE_OPEN_RE = re.compile(
    r'^\s*<article[^>]*class="[^"]*\b' + re.escape(ARTICLE_SCOPE_CLASS) + r'\b[^"]*"',
    re.IGNORECASE,
)


def detect_variant_index(html_path: str) -> int | None:
    """パスから __nandemo_v(\\d+) を抽出。v1〜v5でなければ None。"""
    match = re.search(r"__nandemo_v(\d+)", html_path or "")
    if not match:
        return None
    n = int(match.group(1))
    if 1 <= n <= 5:
        return n
    return None


def get_variant_theme_css_path(variant_index: int | None) -> str | None:
    """v1〜v5 の記事バリアントに対応するテーマCSSパスを返す。"""
    if variant_index is None:
        return None
    path = os.path.join(OUTPUT_ROOT, f"article-theme-v{variant_index}.css")
    return path if os.path.exists(path) else None


def load_article_assets(html_path: str) -> tuple[str, str]:
    """共通アセットをベースに、必要なら記事ローカル上書きを後段で連結する。

    既存の article-common.css / .js は互換目的では読み込まず、
    明示的な記事単位の上書きは article-local.css / .js を使う。
    これにより、古いローカル common 資産が残っていても、
    生成物のデザインルールは常に output/article-common.css / .js を正本にする。
    """
    article_dir = os.path.dirname(os.path.abspath(html_path))
    local_css_path = os.path.join(article_dir, "article-local.css")
    local_js_path = os.path.join(article_dir, "article-local.js")
    legacy_css_path = os.path.join(article_dir, "article-common.css")
    legacy_js_path = os.path.join(article_dir, "article-common.js")

    css = load_text(COMMON_CSS_PATH) if os.path.exists(COMMON_CSS_PATH) else ""
    js = load_text(COMMON_JS_PATH) if os.path.exists(COMMON_JS_PATH) else ""

    if not css and os.path.exists(legacy_css_path):
        css = load_text(legacy_css_path)
    if not js and os.path.exists(legacy_js_path):
        js = load_text(legacy_js_path)

    if os.path.exists(local_css_path):
        local_css = load_text(local_css_path)
        if local_css.strip():
            css = css.rstrip() + "\n\n/* === Article Local CSS === */\n" + local_css
    if os.path.exists(local_js_path):
        local_js = load_text(local_js_path)
        if local_js.strip():
            js = js.rstrip() + "\n\n/* === Article Local JS === */\n" + local_js
    return css, js


CSS_START_MARKER = "<!-- seo-article-common-css:start -->"
CSS_END_MARKER = "<!-- seo-article-common-css:end -->"
JS_START_MARKER = "<!-- seo-article-common-js:start -->"
JS_END_MARKER = "<!-- seo-article-common-js:end -->"


def _compact_asset_for_inline(text: str) -> str:
    """空行を1行に潰す（wpautopによる </p><p> 化を防ぐため、wp_post.pyと同じロジック）。"""
    if not text:
        return ""
    return re.sub(r"\n\s*\n+", "\n", text)


def build_inline_assets_block(css: str | None, js: str | None) -> str:
    # マーカーは <style> / <script> の内側に CSS/JS コメント形式で埋め込む。
    # 旧仕様の HTML コメント形式 (<!-- ... -->) は WordPress wpautop が
    # 単独 <p> で囲み、本文冒頭に空白を生むため新仕様に統一する。
    block = ""
    if css:
        block += (
            f"<style>/* seo-article-common-css:start */"
            f"{_compact_asset_for_inline(css)}"
            f"/* seo-article-common-css:end */</style>\n"
        )
    if js:
        block += (
            f"<script>/* seo-article-common-js:start */"
            f"{_compact_asset_for_inline(js)}"
            f"/* seo-article-common-js:end */</script>\n"
        )
    return block


def strip_inline_assets_block(content: str) -> str:
    """既存のマーカーブロックを除去（再生成時の重複防止）。新旧両仕様を剥がす。"""
    if not content:
        return content or ""
    # 新仕様: <style>...marker:start ... marker:end ...</style>
    content = re.sub(
        r'<style>\s*/\*\s*seo-article-common-css:start\s*\*/.*?/\*\s*seo-article-common-css:end\s*\*/\s*</style>\s*',
        "",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r'<script>\s*/\*\s*seo-article-common-js:start\s*\*/.*?/\*\s*seo-article-common-js:end\s*\*/\s*</script>\s*',
        "",
        content,
        flags=re.DOTALL,
    )
    # 旧仕様: HTML コメントが <style>/<script> の外側にあったブロック（後方互換）
    content = re.sub(
        re.escape(CSS_START_MARKER) + r".*?" + re.escape(CSS_END_MARKER) + r"\s*",
        "",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        re.escape(JS_START_MARKER) + r".*?" + re.escape(JS_END_MARKER) + r"\s*",
        "",
        content,
        flags=re.DOTALL,
    )
    return content


def load_image_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            url = value.get("url")
        else:
            url = value
        if url:
            out[key] = url
    return out


def find_tag_structure_path(html_path: str) -> str | None:
    """同フォルダの *_タグ構成.md を探す。"""
    folder = os.path.dirname(html_path)
    base = os.path.basename(html_path)
    slug = base.replace("_記事.html", "")
    candidate = os.path.join(folder, f"{slug}_タグ構成.md")
    if os.path.exists(candidate):
        return candidate
    return None


def extract_title_and_meta(tag_structure_path: str) -> tuple[str, str]:
    """タグ構成.md から **titleタグ** と **メタディスクリプション** を抽出。
    通常形式（**titleタグ**: xxx）と表形式（| **titleタグ** | xxx |）の両方に対応。"""
    title = ""
    meta = ""
    with open(tag_structure_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            # 表形式: | **titleタグ** | 値 |
            m_table_title = re.match(r"\|\s*\*\*titleタグ\*\*\s*\|\s*(.+?)\s*\|", stripped)
            m_table_meta = re.match(r"\|\s*\*\*メタディスクリプション\*\*\s*\|\s*(.+?)\s*\|", stripped)
            # 通常形式: **titleタグ**: 値
            m_plain_title = re.match(r"\*\*titleタグ\*\*[:：]\s*(.+)", stripped)
            m_plain_meta = re.match(r"\*\*メタディスクリプション\*\*[:：]\s*(.+)", stripped)
            if not title:
                if m_table_title:
                    title = m_table_title.group(1).strip()
                elif m_plain_title:
                    title = m_plain_title.group(1).strip()
            if not meta:
                if m_table_meta:
                    meta = m_table_meta.group(1).strip()
                elif m_plain_meta:
                    meta = m_plain_meta.group(1).strip()
    return title, meta


def build_meta_comment(title: str, meta: str) -> str:
    """for-wp.html 冒頭に挿入する HTML コメント（手動投稿時の参照用）。
    ※WPブロックエディタで空ブロック化を避けるため、改行なしの単一コメントに圧縮する。"""
    if not title and not meta:
        return ""
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if meta:
        parts.append(f"Meta: {meta}")
    return f"<!-- WP投稿設定 | {' | '.join(parts)} -->"


def _minify_css(css: str) -> str:
    """軽量CSSミニファイ。コメント除去＋空白圧縮。"""
    if not css:
        return ""
    # /* ... */ コメント除去
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    # 連続空白を1つに
    css = re.sub(r"\s+", " ", css)
    # セレクタ・宣言区切り周辺の空白除去
    css = re.sub(r"\s*([{}:;,>])\s*", r"\1", css)
    # 末尾セミコロン除去
    css = re.sub(r";}", "}", css)
    return css.strip()


def _minify_js(js: str) -> str:
    """軽量JSミニファイ。/* */ ブロックコメントと // 行コメントを除去、行頭空白を削る。"""
    if not js:
        return ""
    # /* */ ブロックコメント除去
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    # // 行コメント除去（文字列内の // は除外しないので注意）
    js = re.sub(r"^\s*//.*$", "", js, flags=re.MULTILINE)
    # 各行 strip して空行除去
    lines = [l.strip() for l in js.splitlines() if l.strip()]
    return "".join(lines)


def build_compact_assets_block(css: str, js: str) -> str:
    """for-wp.html 用に <style>/<script> をコメントマーカーなし・改行なしで連結。
    WPブロックエディタで空 Custom HTML ブロックが生成されて段落間に余白が出るのを防ぐ。
    CSS/JS は軽量ミニファイ（コメント除去・空白圧縮）してファイルサイズも削減する。"""
    parts = []
    minified_css = _minify_css(css)
    if minified_css:
        parts.append(f"<style>{minified_css}</style>")
    minified_js = _minify_js(js)
    if minified_js:
        parts.append(f"<script>{minified_js}</script>")
    return "".join(parts)


def load_per_article_url_map(html_path: str) -> dict:
    """記事ディレクトリ内の _wp_media_urls.json（per-article 上書き）があれば読み込む。
    グローバル .image_url_cache.json より優先される。"""
    article_dir = os.path.dirname(html_path)
    candidate = os.path.join(article_dir, "_wp_media_urls.json")
    if not os.path.exists(candidate):
        return {}
    try:
        with open(candidate, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def write_setup_sidecar(html_path: str, title: str, meta: str) -> None:
    """title/meta を別ファイル（_setup.txt）に書き出して人間用参照を残す。"""
    if not title and not meta:
        return
    base, _ = os.path.splitext(html_path)
    if base.endswith("_記事_for-wp"):
        base = base[: -len("_記事_for-wp")]
    elif base.endswith("_記事"):
        base = base[: -len("_記事")]
    sidecar = f"{base}_wp_setup.txt"
    lines = ["WordPress 投稿時に設定する項目", "=" * 40, ""]
    if title:
        lines.append(f"Title:\n{title}")
        lines.append("")
    if meta:
        lines.append(f"Meta description:\n{meta}")
        lines.append("")
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def replace_local_image_paths(html: str, cache: dict, per_article: dict | None = None) -> tuple[str, int, int]:
    """
    src="images/foo.png" を cache / per_article から見つけて WP URL に差し替える。
    per_article は記事ディレクトリの _wp_media_urls.json（個別アップロード結果）でグローバルキャッシュより優先。
    戻り値: (置換後HTML, 置換できた数, ヒットしなかったローカル参照の数)
    """
    replaced = 0
    missed = 0
    per_article = per_article or {}

    def _sub(match: re.Match) -> str:
        nonlocal replaced, missed
        attr = match.group(1)
        path = match.group(2)
        # path を cache key 形式（"images/xxx"）に正規化
        key = path if path.startswith("images/") else f"images/{path.split('images/')[-1]}"
        # per-article は basename（"logo_xxx.png"）形式キーで保持されているので両方試す
        basename = os.path.basename(path)
        url = None
        if isinstance(per_article.get(basename), str) and per_article[basename]:
            url = per_article[basename]
        elif isinstance(per_article.get(key), str) and per_article[key]:
            url = per_article[key]
        elif key in cache:
            cache_value = cache[key]
            url = cache_value.get("url") if isinstance(cache_value, dict) else cache_value
        if url:
            replaced += 1
            return f'{attr}="{url}"'
        missed += 1
        return match.group(0)  # そのまま残す

    pattern = re.compile(r'(src|data-src|href)="((?:\./)?images/[^"]+)"')
    new_html = pattern.sub(_sub, html)
    return new_html, replaced, missed


def strip_meta_comment(content: str) -> str:
    """既存のメタコメントブロックを除去（再生成時の重複防止）。
    旧フォーマット（複数行コメント）と新フォーマット（単一行コメント）の両方に対応。"""
    if not content:
        return content
    # 旧フォーマット: 複数行コメント
    old_pattern = re.compile(
        r"<!--\s*\n\s*=+\s*\n\s*WordPress 投稿時に設定する項目.*?-->\s*\n?",
        re.DOTALL,
    )
    content = old_pattern.sub("", content)
    # 新フォーマット: 単一行コメント
    new_pattern = re.compile(r"<!--\s*WP投稿設定\s*\|.*?-->\s*", re.DOTALL)
    content = new_pattern.sub("", content)
    return content


def wrap_article_scope(content: str) -> str:
    """for-wp 用本文も .ndm-article でラップしてテーマCSSを確実に効かせる。"""
    if not content or not content.strip():
        return content
    if ARTICLE_SCOPE_OPEN_RE.search(content):
        return content
    body = content.strip()
    return f'<article class="{ARTICLE_SCOPE_CLASS}">\n{body}\n</article>'


def _style_contains(value: str | None, needle: str) -> bool:
    if not value:
        return False
    return needle in value.replace(" ", "").lower()


def _canonicalize_button_link(soup: BeautifulSoup, source_link) -> object:
    new_link = soup.new_tag("a", href=source_link.get("href", ""))
    new_link["class"] = ["official-site-button"]
    if source_link.get("target"):
        new_link["target"] = source_link["target"]
    rel = source_link.get("rel")
    if rel:
        new_link["rel"] = rel
    new_link.string = source_link.get_text(" ", strip=True)
    return new_link


def normalize_osaka_canonical_markup(content: str) -> str:
    """大阪版で採用している構造に主要CTAを正規化する。

    デザイン差分の原因は CSS だけでなく、記事ごとに残る inline style / wrapper の揺れ。
    ここでは本文テキストは変えず、見た目に効く HTML 構造だけを大阪系の canonical markup に寄せる。
    """
    if not content or not content.strip():
        return content

    soup = BeautifulSoup(content, "html.parser")

    for wrapper in list(soup.find_all(["div", "p"])):
        classes = set(wrapper.get("class") or [])
        style = wrapper.get("style", "")
        direct_links = [child for child in wrapper.find_all("a", href=True, recursive=False)]

        should_convert = "official-site-button-wrap" in classes
        if not should_convert and wrapper.name == "div" and len(direct_links) == 1:
            link = direct_links[0]
            should_convert = _style_contains(style, "text-align:center") and (
                "official-site-button" in set(link.get("class") or [])
                or _style_contains(link.get("style", ""), "background:")
            )

        if not should_convert or not direct_links:
            continue

        wrapper.name = "p"
        wrapper.attrs = {"class": ["official-site-button-wrap"]}
        canonical_link = _canonicalize_button_link(soup, direct_links[0])
        wrapper.clear()
        wrapper.append(canonical_link)

    for cta in soup.select("div.article-final-cta"):
        for eyebrow in cta.select(".article-final-cta__eyebrow"):
            eyebrow.decompose()

    for cta in soup.select("div.article-final-cta"):
        movable_nodes = []
        split_started = False
        for child in list(cta.children):
            if getattr(child, "name", None) == "style" and "ndm-" in child.get_text():
                split_started = True
            elif getattr(child, "name", None) == "section":
                classes = child.get("class") or []
                if any(str(cls).startswith("ndm-") for cls in classes):
                    split_started = True

            if split_started:
                movable_nodes.append(child)

        for node in reversed(movable_nodes):
            cta.insert_after(node.extract())

    for heading in soup.select('h3[id^="clinic-"]'):
        screenshot = None
        for sibling in heading.next_siblings:
            name = getattr(sibling, "name", None)
            if name in {"h2", "h3"}:
                break
            if not getattr(sibling, "get", None):
                continue
            classes = sibling.get("class") or []
            if "clinic-screenshot" in classes:
                screenshot = sibling
                break

        if screenshot is None:
            continue

        cursor = heading.next_sibling
        while cursor is not None and (
            getattr(cursor, "name", None) is None
            and not str(cursor).strip()
        ):
            cursor = cursor.next_sibling

        if cursor is screenshot:
            continue

        heading.insert_after(screenshot.extract())

    return str(soup)


def build_for_wp(
    html_path: str,
    css_path: str = COMMON_CSS_PATH,
    js_path: str = COMMON_JS_PATH,
    *,
    skip_image_cache: bool = False,
) -> str:
    """記事HTMLを for-wp 用に変換した文字列を返す。"""
    if not os.path.exists(html_path):
        raise FileNotFoundError(html_path)

    raw = load_text(html_path)
    variant_index = detect_variant_index(html_path)
    body = strip_meta_comment(strip_inline_assets_block(raw))
    if variant_index is not None:
        body, unresolved_shortcodes = inline_all_nandemo_shortcodes(
            body,
            variant_index=variant_index,
        )
        if unresolved_shortcodes:
            print(
                "  Unresolved nandemo shortcodes: "
                + ", ".join(sorted(set(unresolved_shortcodes)))
            )
    body = normalize_osaka_canonical_markup(body)
    body = wrap_article_scope(body)

    css, js = load_article_assets(html_path)
    theme_path = get_variant_theme_css_path(variant_index)
    if theme_path:
        theme_css = load_text(theme_path)
        if theme_css:
            css = css.rstrip() + f"\n\n/* === Variant Theme v{variant_index} === */\n" + theme_css

    assets_block = build_inline_assets_block(css, js)

    # タグ構成.md から title/meta を抽出
    tag_structure_path = find_tag_structure_path(html_path)
    title = ""
    meta = ""
    if tag_structure_path:
        title, meta = extract_title_and_meta(tag_structure_path)
    meta_comment = build_meta_comment(title, meta)
    # title/meta は単一行コメントで本文先頭に置く + サイドカー .txt にも書き出す
    write_setup_sidecar(html_path, title, meta)

    cache = {} if skip_image_cache else load_image_cache()
    per_article = {} if skip_image_cache else load_per_article_url_map(html_path)
    body, replaced, missed = replace_local_image_paths(body, cache, per_article=per_article)

    print(f"  CSS: {len(css):,} chars" if css else "  CSS: (not found)")
    if theme_path:
        print(f"  Variant theme: {os.path.basename(theme_path)}")
    elif variant_index is not None:
        print("  Variant theme: base only")
    else:
        print("  Variant theme: (no __nandemo_vN in path)")
    print(f"  JS: {len(js):,} chars" if js else "  JS: (not found)")
    print(f"  Image URLs replaced: {replaced}")
    print(f"  Image URLs not in cache (left as-is): {missed}")
    if tag_structure_path:
        print(f"  タグ構成.md: {os.path.basename(tag_structure_path)}")
        print(f"    Title: {title or '(missing)'}")
        print(f"    Meta:  {(meta[:60] + '…') if len(meta) > 60 else (meta or '(missing)')}")
    else:
        print(f"  タグ構成.md: (not found — title/meta コメントは挿入されません)")

    # meta_comment は本文に入れない（WPブロックエディタで空ブロック化を防ぐ）。
    # title/meta は同時に書き出される _wp_setup.txt サイドカーで人間用に参照する。
    return assets_block + body.lstrip()


def derive_for_wp_path(html_path: str) -> str:
    base, ext = os.path.splitext(html_path)
    if base.endswith("_記事"):
        return f"{base}_for-wp{ext}"
    return f"{base}_for-wp{ext}"


def process_one(html_path: str, *, skip_image_cache: bool = False) -> str:
    print(f"\n=== {html_path} ===")
    out_path = derive_for_wp_path(html_path)
    out = build_for_wp(html_path, skip_image_cache=skip_image_cache)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"  → {out_path}  ({len(out):,} chars)")
    return out_path


def find_targets_in_dir(directory: str) -> list[str]:
    pattern = os.path.join(directory, "**", "*_記事.html")
    candidates = glob.glob(pattern, recursive=True)
    # for-wp.html は対象外
    return [p for p in candidates if not p.endswith("_記事_for-wp.html")]


def main() -> None:
    parser = argparse.ArgumentParser(description="記事HTML → 手動投稿用 _for-wp.html を生成")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--html", help="記事HTMLファイルのパス")
    group.add_argument("--dir", help="記事フォルダ（再帰的に *_記事.html を処理）")
    parser.add_argument(
        "--skip-image-cache",
        action="store_true",
        help="画像URLキャッシュを使わず、ローカル画像参照を残したまま _for-wp.html を生成する",
    )
    args = parser.parse_args()

    if args.html:
        if not args.html.endswith("_記事.html"):
            print(f"Warning: --html は通常 *_記事.html を指定。受け取った: {args.html}")
        process_one(args.html, skip_image_cache=args.skip_image_cache)
    else:
        targets = find_targets_in_dir(args.dir)
        if not targets:
            print(f"No *_記事.html found under {args.dir}")
            sys.exit(1)
        print(f"Found {len(targets)} article(s) under {args.dir}")
        for path in targets:
            try:
                process_one(path, skip_image_cache=args.skip_image_cache)
            except Exception as e:
                print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
