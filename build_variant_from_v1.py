#!/usr/local/bin/python3.12
"""
V1 完成版を雛形に variant N (2-5) を生成するスクリプト。

設計原則:
  1. 決定的処理(Python)で構造と shortcode を変える
  2. 本文は原則そのまま維持し、AI リライトは明示 opt-in の時だけ使う
  3. ファクト保持を厳格に検証し、NG なら V1 をそのまま使う

使い方:
  python build_variant_from_v1.py \\
    --base output/AGA_大阪__nandemo_v1/AGA_大阪__nandemo_v1_記事.html \\
    --variant-index 2 \\
    --keyword "AGA 大阪" \\
    --genre-json genres/aga.json
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from copy import copy
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from variant_utils import build_variant_profile, normalize_variant_count
from fill_final_cta import (
    build_final_cta,
    load_genre,
    CTA_START,
    CTA_END,
)
from generate_article import call_claude, load_api_key
from build_for_wp import (
    COMMON_CSS_PATH,
    COMMON_JS_PATH,
    build_inline_assets_block,
    get_variant_theme_css_path,
    load_text as load_asset_text,
    normalize_osaka_canonical_markup,
    strip_inline_assets_block,
)

SCRIPT_DIR = Path(__file__).resolve().parent
SHORTCODE_DIR = SCRIPT_DIR / "shortcodes" / "nandemo"

REWRITE_SYSTEM_PROMPT = """あなたはSEO記事のリライト編集者です。
渡された記事HTMLの構造（タグ・属性・shortcode）と固有情報（料金・住所・固有名詞・数値）は一切変更せず、
本文段落の表現だけを軽微に言い換えるのが仕事です。
見出し、タイトル相当の文言、比較軸、表の中身、リンク、画像、マップ、CTAは変更しません。
HTMLの体裁を崩さず、リライト後のHTMLのみを返してください（前置き・説明文不要）。"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V1記事を雛形にvariant N (2-5)を生成")
    parser.add_argument("--base", required=True, help="V1記事HTMLパス")
    parser.add_argument("--variant-index", type=int, required=True, choices=[2, 3, 4, 5])
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--genre-json", required=True)
    parser.add_argument(
        "--rewrite-body",
        action="store_true",
        help="本文の軽微なAIリライトを明示的に許可する（デフォルトは本文不変）",
    )
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="互換用オプション。現在はデフォルトで本文リライトOFF",
    )
    parser.add_argument("--limit-sections", type=int, default=0, help="最初のN個のH2セクションだけリライト（テスト用、0=全部）")
    return parser.parse_args()


# ============================================================
# 出力ディレクトリ準備
# ============================================================

def prepare_variant_dir(base_html_path: Path, variant_index: int) -> tuple[Path, Path]:
    base_dir = base_html_path.parent
    base_stem = base_html_path.stem  # AGA_大阪__nandemo_v1_記事
    if "__nandemo_v" not in base_stem or "__nandemo_v1_記事" not in base_stem:
        raise ValueError(f"V1ベースとして想定されるファイル名形式ではありません: {base_html_path}")

    new_dir = base_dir.parent / base_dir.name.replace("__nandemo_v1", f"__nandemo_v{variant_index}")
    new_html = new_dir / base_html_path.name.replace("__nandemo_v1_", f"__nandemo_v{variant_index}_")

    if new_dir.exists():
        print(f"  [skip-copy] {new_dir} already exists, reuse")
    else:
        # imagesディレクトリは複製せずシンボリックリンクで参照
        shutil.copytree(base_dir, new_dir, ignore=shutil.ignore_patterns("images", "*.json", "*.md"))
        # imagesは V1 のものを参照（シンボリックリンク）
        v1_images = base_dir / "images"
        new_images = new_dir / "images"
        if v1_images.exists() and not new_images.exists():
            os.symlink(v1_images.resolve(), new_images)
        print(f"  [copy] {base_dir} -> {new_dir}")

    # 記事HTMLは V1 のものを新しい名前で保存
    base_html_content = base_html_path.read_text(encoding="utf-8")
    new_html.write_text(base_html_content, encoding="utf-8")

    return new_dir, new_html


# ============================================================
# Shortcode 差し替え (ndm-aga-v1 -> ndm-aga-vN)
# ============================================================

def swap_hayamihyou_shortcode(html: str, variant_index: int, genre_id: str = "aga") -> str:
    template_path = SHORTCODE_DIR / f"{genre_id}-hayamihyou-{variant_index}.html"
    if not template_path.exists():
        raise FileNotFoundError(f"shortcode template not found: {template_path}")
    template = template_path.read_text(encoding="utf-8").strip()

    # V1 の <style> + <section class="ndm-aga-v1"> ... </section> を一括置換
    pattern = re.compile(
        r'<style>\s*\n\.ndm-aga-v1\{.*?</style>\s*\n*<section class="ndm-aga-v1">.*?</section>',
        re.DOTALL,
    )
    matches = pattern.findall(html)
    if not matches:
        print("  [warn] ndm-aga-v1 セクションが見つかりません")
        return html

    new_html = pattern.sub(template, html)
    print(f"  [shortcode] ndm-aga-v1 → ndm-aga-v{variant_index} 置換: {len(matches)}件")
    return new_html


# ============================================================
# 順序回転 (rotation件分の循環シフト)
# ============================================================

AREA_H2_KEYWORDS = ["梅田・大阪駅", "難波・心斎橋", "天王寺・阿倍野"]


def collect_h2_sections(soup: BeautifulSoup) -> list[tuple[Tag, list]]:
    """各 H2 とその後ろの兄弟要素群を [(h2, [siblings])] で返す"""
    sections = []
    h2_tags = soup.find_all("h2")
    for i, h2 in enumerate(h2_tags):
        if "ndm-aga-v" in (h2.get("class") or [""])[0] or "article-final-cta" in (h2.get("class") or [""])[0]:
            continue
        siblings = []
        sib = h2.next_sibling
        while sib is not None:
            if isinstance(sib, Tag) and sib.name == "h2":
                if any(c in (sib.get("class") or []) for c in ["ndm-aga-v1__title", "ndm-aga-v2__title",
                       "ndm-aga-v3__title", "ndm-aga-v4__title", "ndm-aga-v5__title", "article-final-cta__title"]):
                    sib = sib.next_sibling
                    continue
                break
            siblings.append(sib)
            sib = sib.next_sibling
        sections.append((h2, siblings))
    return sections


def rotate_area_h2(soup: BeautifulSoup, rotation: int) -> int:
    """エリア別H2(梅田/難波/天王寺)を rotation 件循環"""
    if rotation == 0:
        return 0
    sections = collect_h2_sections(soup)
    area_indices = []
    for i, (h2, _) in enumerate(sections):
        text = h2.get_text(strip=True)
        if any(kw in text for kw in AREA_H2_KEYWORDS):
            area_indices.append(i)
    if len(area_indices) < 2:
        return 0
    # rotation 件を先頭から末尾に回す
    n = len(area_indices)
    shift = rotation % n
    if shift == 0:
        return 0
    new_order = area_indices[shift:] + area_indices[:shift]
    # area_indices の各セクションを new_order の順で並び替え
    # 元の area_indices 位置に新しい順番のセクションを挿入
    target_h2s = [sections[i][0] for i in area_indices]
    target_blocks = [(sections[i][0], sections[i][1]) for i in area_indices]
    # 新しい順序で並び替えたブロックを取り出す
    reordered = [target_blocks[(i + shift) % n] for i in range(n)]
    # 既存のエリア H2 を抜き出して、新しい順序で配置
    # 最初のエリア H2 の前に並べ替え後のブロックを挿入し、元のブロックを削除
    first_pos = sections[area_indices[0]][0]
    # 旧ブロックを extract
    for h2, sibs in target_blocks:
        h2.extract()
        for s in sibs:
            if s.parent:
                s.extract()
    # first_pos の前に配置 → first_pos は extract 済みなので、parent から prepend
    # 代替: first_pos が指していた parent + index を覚えておく必要がある
    # シンプルに、最初のエリア H2 の旧位置を覚えるため、元の前の sibling を覚える
    # → 実装複雑、別アプローチ採用
    return shift


def rotate_clinic_h3(soup: BeautifulSoup, rotation: int) -> int:
    """H2-2 配下の19院H3を rotation 件循環"""
    if rotation == 0:
        return 0
    # H2 で「○○クリニック19院」を含むものの後ろの H3 群
    target_h2 = None
    for h2 in soup.find_all("h2"):
        if "クリニック19院" in h2.get_text() or ("19院" in h2.get_text() and "口コミ" in h2.get_text()):
            target_h2 = h2
            break
    if not target_h2:
        return 0

    h3_blocks = []  # [(h3, [siblings])]
    sib = target_h2.next_sibling
    h3_pending = None
    h3_sibs = []
    while sib is not None:
        if isinstance(sib, Tag) and sib.name == "h2":
            if h3_pending is not None:
                h3_blocks.append((h3_pending, h3_sibs))
            break
        if isinstance(sib, Tag) and sib.name == "h3" and sib.get("id", "").startswith("clinic-"):
            if h3_pending is not None:
                h3_blocks.append((h3_pending, h3_sibs))
            h3_pending = sib
            h3_sibs = []
        elif h3_pending is not None:
            h3_sibs.append(sib)
        sib = sib.next_sibling
    else:
        if h3_pending is not None:
            h3_blocks.append((h3_pending, h3_sibs))

    if len(h3_blocks) < 2:
        return 0
    n = len(h3_blocks)
    shift = rotation % n
    if shift == 0:
        return 0

    # 並び替え: index i のブロックを new_order[i] のブロックに置き換える
    new_order = h3_blocks[shift:] + h3_blocks[:shift]

    # 既存ブロックを extract、新順序で順次 insert_after で配置
    # アンカーポイント: target_h2 の直後
    anchor = target_h2

    # まず全ブロックを extract（順序は元のまま）
    for h3, sibs in h3_blocks:
        for s in [h3] + sibs:
            if s.parent:
                s.extract()

    # 新順序で anchor の後ろに順番に挿入
    cursor = anchor
    for h3, sibs in new_order:
        cursor.insert_after(h3)
        cursor = h3
        for s in sibs:
            cursor.insert_after(s)
            cursor = s

    print(f"  [rotate] 19院H3を {shift} 件循環")
    return shift


# ============================================================
# 末尾 CTA 再生成
# ============================================================

def regenerate_final_cta(html: str, keyword: str, genre: dict, html_path: Path, variant_index: int) -> str:
    cta_html = build_final_cta(keyword, genre, html, str(html_path), variant_index=variant_index)
    if CTA_START in html and CTA_END in html:
        return re.sub(
            rf"{re.escape(CTA_START)}.*?{re.escape(CTA_END)}",
            cta_html,
            html,
            flags=re.DOTALL,
        )
    return html.rstrip() + "\n\n" + cta_html + "\n"


# ============================================================
# クラス名の variant 化 (article-final-cta cta-variant-1 -> -N)
# ============================================================

def normalize_variant_classes(html: str, variant_index: int) -> str:
    return html.replace("cta-variant-1", f"cta-variant-{variant_index}")


# ============================================================
# AI リライト
# ============================================================

CLINIC_NAMES_PRESERVE = [
    "Dr.AGAクリニック", "AGAスキンクリニック", "クリニックフォア",
    "AHC梅田メディカルサロン", "AGAヘアクリニック", "Dクリニック",
    "ゴリラクリニック", "イースト駅前クリニック", "ウィルAGAクリニック",
    "駅前AGAクリニック", "ABCクリニック", "親和クリニック",
    "湘南AGAクリニック", "湘南美容クリニック", "大阪AGA加藤クリニック",
    "大阪梅田紳士クリニック", "玉城クリニック", "江坂クリニック",
    "浜口クリニック", "東梅田かなもりクリニック",
    "DMMオンラインクリニック", "イーライフ", "レバクリ",
    "フィナステリド", "ミノキシジル", "デュタステリド", "プロペシア", "ザガーロ",
]


def extract_facts(html: str) -> dict:
    """ファクト検証用: URL集合・数値パターン集合・固有名詞出現回数"""
    # 「0円」は「無料」と意味同一で言い換え許容のため、yen_values からは除外
    yen_values = re.findall(r'\d{1,3}(?:,\d{3})*円', html)
    yen_values = [v for v in yen_values if v != "0円"]
    return {
        "hrefs": sorted(set(re.findall(r'href="([^"]+)"', html))),
        "imgs": sorted(set(re.findall(r'<img[^>]+src="([^"]+)"', html))),
        "iframes": sorted(set(re.findall(r'<iframe[^>]+src="([^"]+)"', html))),
        "shortcode_classes": sorted(set(re.findall(r'class="(ndm-aga-v\d[^"\s]*)"', html))),
        "yen_values": sorted(yen_values),
        "walk_min": sorted(re.findall(r'徒歩\d+分', html)),
        "times": sorted(re.findall(r'\d{1,2}:\d{2}', html)),
        "clinic_counts": {name: html.count(name) for name in CLINIC_NAMES_PRESERVE},
    }


def facts_match(orig_facts: dict, new_facts: dict, tolerance: float = 0.0) -> tuple[bool, str]:
    """orig と new でファクトが保持されているか確認（URLは厳格、数値・名詞は集合保持を確認）"""
    # URL系・shortcode classes: 集合が完全一致（一つでも消えたら NG）
    for k in ["hrefs", "imgs", "iframes", "shortcode_classes"]:
        orig_set = set(orig_facts[k])
        new_set = set(new_facts[k])
        if orig_set - new_set:
            return False, f"{k} 消失: {list(orig_set - new_set)[:3]}"
    # 数値系: orig の値が new に全部含まれること（出現回数の差は許容）
    for k in ["yen_values", "walk_min", "times"]:
        orig_set = set(orig_facts[k])
        new_set = set(new_facts[k])
        missing = orig_set - new_set
        if missing:
            return False, f"{k} 数値消失: {list(missing)[:3]}"
    # クリニック名・薬剤名: orig で出現していたものが new で 0 になったら NG（出現回数の減少は許容）
    for name, orig_count in orig_facts["clinic_counts"].items():
        if orig_count > 0 and new_facts["clinic_counts"][name] == 0:
            return False, f"固有名詞消失: '{name}'"
    return True, ""


def build_rewrite_prompt(section_html: str, variant_profile: dict) -> str:
    return f"""以下のHTMLセクションを variant_index={variant_profile['variant_index']} 向けにリライトしてください。

## 絶対変更禁止（厳守）
- すべてのタグ・class・id・属性は変更しない
- すべての <a href>・<img src>・<iframe src> の URL は変更しない
- **`<!--PROTECTED_BLOCK_N-->` というHTMLコメントは絶対に削除・変更しない、コメントの中身も書き換えない、コメントを別の場所に移動しない**（このコメントの位置に重要なブロックが入る）
- すべての数値（料金「◯円」「0円」、徒歩◯分、HH:MM、電話番号）は数字も単位も変更しない
- 固有名詞（クリニック名・院名・駅名・地名・住所・薬剤名）は一字一句保持
- `<h2>` `<h3>` `<h4>` の中身は変更しない
- 表（`<table>` 内）の文言は変更しない
- CTA文言、ボタン文言、注記文言は変更しない

## 変更対象（リライト対象）
- 段落 <p> の文章
- リスト <li> の文章（固有名詞・数値以外の修飾表現）
- review-bubble 内の口コミ文章

## variant_index={variant_profile['variant_index']} の編集スタイル
- 導入: {variant_profile['lead_style']}
- 文章運び: {variant_profile['sentence_style']}
- CTA直前: {variant_profile['cta_style']}

## 表現は別の言い回しに
同じ意味でも語彙・語順・接続詞を軽く変える。
ただし、元記事から大幅に意味や論旨を変えない。別記事由来の話題を足さない。
ファクト（料金・固有名詞・数値・「無料」と「0円」など意味同一の数値表現も）はオリジナル通りに保持する。

## 元HTML
{section_html}

## 出力形式
リライト後のHTMLのみを返してください。前置き・説明・コードフェンス（```）は不要。
`<!--PROTECTED_BLOCK_N-->` コメントは元のまま保持してください。
"""


def strip_code_fence(content: str) -> str:
    s = content.strip()
    s = re.sub(r"^```[a-zA-Z]*\n", "", s)
    s = re.sub(r"\n```\s*$", "", s)
    return s.strip()


def refresh_inline_common_assets(html: str, variant_index: int) -> str:
    """生HTML先頭の共通CSS/JSブロックを、現在の正本ルールで差し替える。"""
    common_css_path = Path(COMMON_CSS_PATH)
    common_js_path = Path(COMMON_JS_PATH)
    css = load_asset_text(str(common_css_path)) if common_css_path.exists() else ""
    theme_path = get_variant_theme_css_path(variant_index)
    if theme_path:
        theme_css = load_asset_text(theme_path)
        if theme_css:
            css = css.rstrip() + f"\n\n/* === Variant Theme v{variant_index} === */\n" + theme_css
    js = load_asset_text(str(common_js_path)) if common_js_path.exists() else ""
    body = strip_inline_assets_block(html).lstrip()
    body = normalize_osaka_canonical_markup(body)
    assets_block = build_inline_assets_block(css, js)
    return assets_block + body


SHORTCODE_PROTECT_PATTERNS = [
    # 共通 CSS/JS の inline 注入ブロック（build_for_wp.py 由来）
    re.compile(r'<!-- seo-article-common-css:start -->.*?<!-- seo-article-common-css:end -->', re.DOTALL),
    re.compile(r'<!-- seo-article-common-js:start -->.*?<!-- seo-article-common-js:end -->', re.DOTALL),
    # ndm-aga-v* 系の CSS 定義 (<style> ブロック)
    re.compile(r'<style>[^<]*?\.ndm-aga-v\d.*?</style>', re.DOTALL),
    # 早見表 section
    re.compile(r'<section class="ndm-aga-v\d[^"]*">.*?</section>', re.DOTALL),
    # final-cta block
    re.compile(r'<!-- final-cta:start -->.*?<!-- final-cta:end -->', re.DOTALL),
    # 各種ボタン HTML（公式サイトボタン、CTAリンク）— 全URLを保護
    re.compile(
        r'<div(?:\s+class="[^"]*official-site-button-wrap[^"]*")?[^>]*style="[^"]*text-align:center[^"]*"[^>]*>\s*<a[^>]+>[^<]*</a>\s*</div>',
        re.DOTALL,
    ),
    re.compile(
        r'<p\s+class="official-site-button-wrap"[^>]*>.*?</p>',
        re.DOTALL,
    ),
    # clinic-screenshot ブロック（画像URL保持）
    re.compile(r'<div class="clinic-screenshot">.*?</div>', re.DOTALL),
    # clinic-map ブロック（iframe URL保持）
    re.compile(r'<div class="clinic-map">.*?</div>', re.DOTALL),
    # clinic-maps-multi ブロック（複数院マップ全体）
    re.compile(r'<div class="clinic-maps-multi"[^>]*>.*?</div>\s*</div>\s*</div>', re.DOTALL),
]


def protect_shortcodes(html: str) -> tuple[str, list[str]]:
    """shortcode 系ブロックを一時マーカーに置換し、後で復元できるよう退避する"""
    placeholders: list[str] = []

    def make_replacer(plist):
        def _replacer(match):
            plist.append(match.group(0))
            return f"<!--PROTECTED_BLOCK_{len(plist)-1}-->"
        return _replacer

    new_html = html
    for pattern in SHORTCODE_PROTECT_PATTERNS:
        new_html = pattern.sub(make_replacer(placeholders), new_html)
    return new_html, placeholders


def restore_shortcodes(html: str, placeholders: list[str]) -> str:
    for i, block in enumerate(placeholders):
        html = html.replace(f"<!--PROTECTED_BLOCK_{i}-->", block)
    return html


def rewrite_section_with_ai(section_html: str, variant_profile: dict, api_key: str) -> tuple[str, str]:
    """1セクションをAIでリライトする。返り値: (rewritten_html or original, status)"""
    orig_facts = extract_facts(section_html)
    protected, placeholders = protect_shortcodes(section_html)

    if placeholders and protected.strip() == "<!--PROTECTED_BLOCK_0-->" * len(placeholders):
        # セクション全体が保護対象（リライトする本文がない）→ 原文を返す
        return section_html, "skip_all_protected"

    prompt = build_rewrite_prompt(protected, variant_profile)

    try:
        content, _, _ = call_claude(REWRITE_SYSTEM_PROMPT, prompt, api_key, max_tokens=8192)
    except Exception as e:
        return section_html, f"api_error:{e}"

    rewritten = strip_code_fence(content)
    rewritten = restore_shortcodes(rewritten, placeholders)
    new_facts = extract_facts(rewritten)
    ok, reason = facts_match(orig_facts, new_facts)
    if not ok:
        return section_html, f"facts_mismatch:{reason}"
    return rewritten, "ok"


def split_into_h2_sections(html: str) -> list[tuple[str, str]]:
    """HTML全体を H2 ごとに区切って [(label, section_html), ...] で返す。"""
    parts = []
    pattern = re.compile(r"(<h2[^>]*>)", re.IGNORECASE)
    matches = list(pattern.finditer(html))
    if not matches:
        return [("full", html)]

    lead = html[: matches[0].start()]
    if lead.strip():
        parts.append(("lead", lead))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        chunk = html[start:end]
        h2_text_match = re.search(r"<h2[^>]*>(.*?)</h2>", chunk, re.DOTALL)
        label = (h2_text_match.group(1).strip() if h2_text_match else f"h2_{i}")[:40]
        parts.append((label, chunk))
    return parts


def split_h2_chunk_by_h3(label: str, chunk: str, max_chars: int = 25000) -> list[tuple[str, str]]:
    """H2 chunk が大きい場合、H3 ごとに細分化する"""
    if len(chunk) <= max_chars:
        return [(label, chunk)]
    h3_pattern = re.compile(r"(<h3[^>]*>)", re.IGNORECASE)
    matches = list(h3_pattern.finditer(chunk))
    if not matches:
        return [(label, chunk)]
    parts = []
    if matches[0].start() > 0:
        intro = chunk[: matches[0].start()]
        if intro.strip():
            parts.append((f"{label} (intro)", intro))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(chunk)
        sub_chunk = chunk[start:end]
        h3_match = re.search(r"<h3[^>]*>(.*?)</h3>", sub_chunk, re.DOTALL)
        sub_label = (h3_match.group(1).strip() if h3_match else f"h3_{i}")[:30]
        parts.append((f"{label}/{sub_label}", sub_chunk))
    return parts


def split_into_sections(html: str) -> list[tuple[str, str]]:
    """H2 → H3 と階層的に分割する"""
    h2_parts = split_into_h2_sections(html)
    final_parts = []
    for label, chunk in h2_parts:
        final_parts.extend(split_h2_chunk_by_h3(label, chunk))
    return final_parts


def filter_rewritable(parts: list[tuple[str, str]]) -> list[tuple[int, str, str]]:
    """リライト対象だけを (index, label, html) で返す。
    shortcodeのH2 (ndm-aga-v*__title)、final-cta__title はスキップ。"""
    rewritable = []
    for i, (label, chunk) in enumerate(parts):
        # shortcode 内のH2は対象外
        if 'class="ndm-aga-v' in chunk[:1000] and "__title" in chunk[:1000]:
            # チャンクの先頭が早見表H2 → このセクション全部 shortcode なのでスキップ
            if not re.search(r"<h2(?![^>]*__title)", chunk):
                continue
        # final-cta H2 もそのチャンク全体が CTA なら skip
        if 'class="article-final-cta__title"' in chunk[:300]:
            continue
        rewritable.append((i, label, chunk))
    return rewritable


# ============================================================
# メイン
# ============================================================

def main() -> int:
    args = parse_args()
    base_path = Path(args.base).resolve()
    if not base_path.exists():
        print(f"Error: base file not found: {base_path}")
        return 1

    print(f"=== Variant V{args.variant_index} 生成開始 ===")
    print(f"  base: {base_path}")

    # Step 1: ディレクトリ複製
    new_dir, new_html_path = prepare_variant_dir(base_path, args.variant_index)

    # Step 2: HTML読み込み
    html = new_html_path.read_text(encoding="utf-8")

    # Step 3: shortcode 差し替え (ndm-aga-v1 → vN)
    html = swap_hayamihyou_shortcode(html, args.variant_index, genre_id="aga")

    # Step 4: クラス名 normalize (cta-variant-1 → -N)
    html = normalize_variant_classes(html, args.variant_index)

    # Step 5: 末尾CTA再生成
    genre = load_genre(args.genre_json)
    html = regenerate_final_cta(html, args.keyword, genre, new_html_path, args.variant_index)
    print(f"  [cta] 末尾CTAを variant_index={args.variant_index} で再生成")

    # Step 6: 順序回転
    rotation = args.variant_index - 1
    soup = BeautifulSoup(html, "html.parser")
    rotated_h3 = rotate_clinic_h3(soup, rotation)
    html = str(soup)

    # Step 7: AI リライト（本文不変を既定値とし、明示 opt-in の場合のみ実行）
    if args.rewrite_body and not args.no_rewrite:
        api_key = load_api_key()
        variant_profile = build_variant_profile(args.variant_index, 5)
        print(f"  [rewrite] variant_profile: {variant_profile['lead_style'][:30]}...")

        parts = split_into_sections(html)
        rewritable = filter_rewritable(parts)
        if args.limit_sections > 0:
            rewritable = rewritable[: args.limit_sections]
        print(f"  [rewrite] {len(rewritable)} セクションをリライト対象に")

        for idx, label, chunk in rewritable:
            print(f"    [section] {label} ... ", end="", flush=True)
            rewritten, status = rewrite_section_with_ai(chunk, variant_profile, api_key)
            if status == "ok":
                parts[idx] = (label, rewritten)
                print(f"OK ({len(chunk)} -> {len(rewritten)} chars)")
            else:
                print(f"FALLBACK ({status[:80]})")

        html = "".join(chunk for _, chunk in parts)
    else:
        print("  [rewrite] スキップ（デフォルト: 本文不変）")

    # Step 8: 生HTML先頭の共通CSS/JSを、現在の正本ルールで更新
    html = refresh_inline_common_assets(html, args.variant_index)

    # Step 9: 出力
    new_html_path.write_text(html, encoding="utf-8")
    print(f"  [output] {new_html_path}")

    # Step 10: build_for_wp.py を呼び出して _for-wp.html を生成
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "build_for_wp.py"), "--html", str(new_html_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"  [for-wp] 生成完了")
    except subprocess.CalledProcessError as e:
        print(f"  [for-wp] 生成失敗: {e.stderr[:200]}")

    print(f"=== Variant V{args.variant_index} 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
