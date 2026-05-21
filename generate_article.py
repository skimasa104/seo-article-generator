#!/usr/local/bin/python3.12
"""
SEO記事自動生成スクリプト (Step 2-3)
Claude APIを使い、競合記事データから記事HTMLを自動生成する。

  Step 2: タグ構成設計（競合分析→H2/H3構成を動的に決定）
  Step 3: 本文HTML生成（タグ構成に従ってHTML出力）

使い方:
  python generate_article.py --keyword "AGA 横浜" --genre aga
  python generate_article.py --keyword "AGA 横浜" --genre aga --step 2   # Step 2のみ
  python generate_article.py --keyword "AGA 横浜" --genre aga --step 3   # Step 3のみ

環境変数:
  ANTHROPIC_API_KEY: Claude API キー
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import unicodedata
from datetime import datetime

from article_audit import validate_article_html
from bs4 import BeautifulSoup, NavigableString
from env_utils import load_project_env
from output_utils import (
    ensure_common_assets_for_key,
    ensure_output_dir_for_key,
    get_output_dir_for_key,
    keyword_to_slug,
    resolve_output_key,
)
from output_utils import get_keyword_scraped_dir
from variant_utils import (
    apply_editorial_variant_to_tag_structure,
    build_editorial_variant_instruction,
    build_variant_profile,
    inline_variant_shortcodes_in_html,
    is_nandemo_output_key,
    resolve_variant_embed_html,
)
from survey_utils import build_survey_policy, build_survey_prompt_instruction

# ========================================
# 設定
# ========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENRES_DIR = os.path.join(SCRIPT_DIR, "genres")

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL_STEP2 = "claude-opus-4-1-20250805"
CLAUDE_MODEL_STEP3 = "claude-sonnet-4-20250514"
MAX_TOKENS_STEP2 = 8192
MAX_TOKENS_STEP3 = 16384
MAX_SECTION_REPAIR_ATTEMPTS = 2
CLAUDE_RETRY_DELAYS = [30, 90, 180, 300]
STEP2_SUMMARY_MAX_CHARS = 12000
STEP2_STRUCTURE_MAX_CHARS = 14000
STEP3_SUMMARY_MAX_CHARS = 10000
STEP3_STRUCTURE_MAX_CHARS = 18000
STEP3_SECTIONAL_H2_THRESHOLD = 8
STEP3_SECTION_H3_SPLIT_THRESHOLD = 6
STEP3_SECTION_H3_CHUNK_SIZE = 4
STEP3_SECTION_MARKDOWN_SPLIT_THRESHOLD = 5000
FORBIDDEN_TEMPLATE_PATTERN = re.compile(r"\{\{(?:後で作成|後で挿入|TODO):[^}]*\}\}")

load_project_env()


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def find_forbidden_template_markers(text: str) -> list[str]:
    return FORBIDDEN_TEMPLATE_PATTERN.findall(text or "")


def ensure_no_forbidden_template_markers(text: str, context_label: str) -> None:
    markers = find_forbidden_template_markers(text)
    if not markers:
        return
    preview = " / ".join(markers[:3])
    raise ValueError(f"{context_label}に未解決のプレースホルダーが残っています: {preview}")


def cleanup_generated_html(content: str, keyword_slug: str) -> str:
    """後続ステップで差し込む画像プレースホルダーを除去する。"""
    patterns = [
        rf'\s*<img\s+[^>]*src="images/{re.escape(keyword_slug)}_top\.(?:png|jpg|jpeg|webp)"[^>]*>\s*',
        rf'\s*<img\s+[^>]*src="images/{re.escape(keyword_slug)}_h2_\d+\.(?:png|jpg|jpeg|webp)"[^>]*>\s*',
    ]

    cleaned = content
    for pattern in patterns:
        cleaned = re.sub(pattern, "\n", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_code_fences(content: str) -> str:
    content = content.strip()
    if content.startswith("```html"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = re.sub(r"</?(?:html|body)\b[^>]*>", "", content, flags=re.IGNORECASE)
    return content.strip()


def replace_markdown_bold_literals(content: str) -> str:
    """HTML断片に紛れた Markdown 強調記法を機械的に除去する。"""
    content = re.sub(r"\*\*([^<>\n*]+?)\*\*", r"<strong>\1</strong>", content)
    return content.replace("**", "")


def normalize_html_fragment(content: str) -> str:
    """Claudeが返したHTML断片を、余分なラッパーを除去した形に整える。"""
    content = strip_code_fences(content)
    if not content:
        return ""
    content = replace_markdown_bold_literals(content)

    wrapped = f'<div id="__codex_root__">{content}</div>'
    soup = BeautifulSoup(wrapped, "html.parser")
    root = soup.find(id="__codex_root__")
    if root is None:
        return content.strip()
    normalize_markdown_bold(root)
    return replace_markdown_bold_literals(root.decode_contents().strip())


def strip_h2_tags(fragment: str) -> str:
    return re.sub(r"(?is)<h2\b[^>]*>.*?</h2>\s*", "", fragment or "").strip()


def normalize_markdown_bold(root: BeautifulSoup | object) -> None:
    """誤って残った Markdown の **強調** を <strong> に変換する。"""
    bold_pattern = re.compile(r"\*\*(.+?)\*\*")
    factory = BeautifulSoup("", "html.parser")

    for text_node in list(root.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue
        parent = getattr(text_node, "parent", None)
        if parent is None or getattr(parent, "name", None) in {"script", "style", "code", "pre"}:
            continue

        original = str(text_node)
        if "**" not in original:
            continue

        cursor = 0
        replacements: list[object] = []
        for match in bold_pattern.finditer(original):
            if match.start() > cursor:
                replacements.append(NavigableString(original[cursor:match.start()]))
            strong = factory.new_tag("strong")
            strong.string = match.group(1)
            replacements.append(strong)
            cursor = match.end()

        if not replacements:
            continue
        if cursor < len(original):
            replacements.append(NavigableString(original[cursor:]))

        for replacement in replacements[::-1]:
            text_node.insert_after(replacement)
        text_node.extract()


def normalize_heading_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", "", text)
    return text.strip()


def canonical_heading_key(text: str) -> str:
    normalized = normalize_heading_text(text)
    if not normalized:
        return ""
    normalized = unicodedata.normalize("NFKC", normalized).lower()
    normalized = normalized.replace("〜", "-").replace("～", "-")
    normalized = re.sub(r"[◯○〇]+", "#", normalized)
    normalized = re.sub(r"\d+(?:\s*[-~]\s*\d+)?", "#", normalized)
    normalized = re.sub(r"[()（）［］\[\]【】「」『』:：・/／,，、。!！?？\-\s]+", "", normalized)
    normalized = re.sub(r"#+", "#", normalized)
    return normalized.strip("#")


def canonical_h2_key(text: str) -> str:
    raw = normalize_heading_text(text)
    if not raw:
        return ""
    normalized = canonical_heading_key(raw)
    if "よくある質問" in raw or "faq" in raw.lower():
        return "faq"
    if "まとめ" in raw:
        return "summary"
    if "オンライン診療" in raw and "クリニック" in raw:
        return "online_clinic"
    if "費用相場" in raw or "料金相場" in raw:
        return "cost"
    if "治療方法別" in raw or "治療法別" in raw:
        return "treatment_type"
    return normalized


def replace_or_append_h2_section(content: str, target_h2: str, section_html: str) -> str:
    """対象H2セクションを差し替える。存在しない場合は末尾に追加する。"""
    pattern = re.compile(
        rf'<h2[^>]*>\s*{re.escape(target_h2)}\s*</h2>.*?(?=<h2\b|$)',
        re.IGNORECASE | re.DOTALL,
    )
    section_html = section_html.strip()
    if pattern.search(content):
        return pattern.sub(section_html + "\n\n", content, count=1).strip()

    target_key = canonical_h2_key(target_h2)
    if target_key:
        actual_h2s, _ = extract_actual_headings(content)
        for actual_h2 in actual_h2s:
            if actual_h2 == target_h2:
                continue
            if canonical_h2_key(actual_h2) != target_key:
                continue
            similar_pattern = re.compile(
                rf'<h2[^>]*>\s*{re.escape(actual_h2)}\s*</h2>.*?(?=<h2\b|$)',
                re.IGNORECASE | re.DOTALL,
            )
            if similar_pattern.search(content):
                return similar_pattern.sub(section_html + "\n\n", content, count=1).strip()

    return (content.rstrip() + "\n\n" + section_html).strip()


def extract_expected_headings(tag_structure: str) -> tuple[list[str], list[str]]:
    h2s = [m.strip() for m in re.findall(r"^### \[H2\] (.+)$", tag_structure, re.MULTILINE)]
    h3s = [m.strip() for m in re.findall(r"^#### \[H3\] (.+)$", tag_structure, re.MULTILINE)]
    return h2s, h3s


def parse_tag_structure_sections(tag_structure: str) -> list[dict]:
    sections = []
    current = None
    for line in tag_structure.splitlines():
        if line.startswith("### [H2] "):
            if current:
                sections.append(current)
            current = {"h2": line.replace("### [H2] ", "", 1).strip(), "h3s": []}
        elif line.startswith("#### [H3] ") and current is not None:
            current["h3s"].append(line.replace("#### [H3] ", "", 1).strip())
    if current:
        sections.append(current)
    return sections


def extract_section_markdown(tag_structure: str, target_h2: str) -> str:
    pattern = re.compile(
        rf"(^### \[H2\] {re.escape(target_h2)}.*?)(?=^### \[H2\] |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(tag_structure)
    return match.group(1).strip() if match else ""


def extract_section_intro_markdown(section_markdown: str) -> str:
    if not section_markdown.strip():
        return ""
    parts = re.split(r"^#### \[H3\] ", section_markdown, maxsplit=1, flags=re.MULTILINE)
    return parts[0].strip()


def extract_h3_markdown(section_markdown: str, target_h3: str) -> str:
    pattern = re.compile(
        rf"(^#### \[H3\] {re.escape(target_h3)}.*?)(?=^#### \[H3\] |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(section_markdown)
    return match.group(1).strip() if match else ""


def chunk_list(items: list[str], chunk_size: int) -> list[list[str]]:
    if chunk_size <= 0:
        return [items]
    return [items[index:index + chunk_size] for index in range(0, len(items), chunk_size)]


def should_split_section_by_h3(section_markdown: str, h3s: list[str]) -> bool:
    return (
        len(h3s) >= STEP3_SECTION_H3_SPLIT_THRESHOLD
        or len(section_markdown) >= STEP3_SECTION_MARKDOWN_SPLIT_THRESHOLD
    )


def extract_intro_markdown(tag_structure: str) -> str:
    pattern = re.compile(
        r"^## 導入部分\s*(.*?)(?=^## タグ構成|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(tag_structure)
    return match.group(1).strip() if match else ""


def extract_actual_headings(html: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    h2s = [node.get_text(" ", strip=True) for node in soup.find_all("h2")]
    h3s = [node.get_text(" ", strip=True) for node in soup.find_all("h3")]
    return h2s, h3s


def find_missing_headings(tag_structure: str, html: str) -> tuple[list[str], list[str]]:
    expected_h2s, expected_h3s = extract_expected_headings(tag_structure)
    actual_h2s, actual_h3s = extract_actual_headings(html)
    actual_h2_keys = {canonical_h2_key(heading) for heading in actual_h2s}
    actual_h3_keys = {canonical_heading_key(heading) for heading in actual_h3s}
    missing_h2s = [
        heading
        for heading in expected_h2s
        if canonical_h2_key(heading) not in actual_h2_keys
    ]
    missing_h3s = [
        heading
        for heading in expected_h3s
        if canonical_heading_key(heading) not in actual_h3_keys
    ]
    return missing_h2s, missing_h3s


def compact_text_for_prompt(text: str, max_chars: int) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text, False
    marker = "\n...(中略)..."
    trimmed = text[: max_chars - len(marker)].rstrip()
    return trimmed + marker, True


def compact_structure_for_prompt(structure: str, max_chars: int) -> tuple[str, bool]:
    structure = (structure or "").strip()
    if len(structure) <= max_chars:
        return structure, False

    kept_lines = []
    section_text_lines = 0
    section_bullet_lines = 0

    for line in structure.splitlines():
        stripped = line.strip()
        if not stripped:
            if kept_lines and kept_lines[-1] != "":
                kept_lines.append("")
            continue

        is_heading = (
            stripped.startswith("#")
            or re.match(r"^(title|url|見出し|h[1-6]|本文要約|要点|メタ|description)\b", stripped, re.IGNORECASE)
        )
        is_bullet = stripped.startswith(("-", "*", "・")) or bool(re.match(r"^\d+\.", stripped))

        if is_heading:
            kept_lines.append(line)
            section_text_lines = 0
            section_bullet_lines = 0
        elif is_bullet and section_bullet_lines < 4:
            kept_lines.append(line)
            section_bullet_lines += 1
        elif section_text_lines < 2 and len(stripped) <= 180:
            kept_lines.append(line)
            section_text_lines += 1

        candidate = "\n".join(kept_lines).strip()
        if len(candidate) >= max_chars:
            break

    compacted = "\n".join(kept_lines).strip()
    if not compacted:
        compacted, _ = compact_text_for_prompt(structure, max_chars)
        return compacted, True

    if len(compacted) > max_chars:
        compacted = compacted[:max_chars].rstrip()

    if len(compacted) < len(structure):
        compacted = compacted.rstrip() + "\n...(中略)..."

    return compacted, True


def prepare_step2_prompt_context(scraped_data: dict) -> tuple[str, list[str]]:
    summary_json = json.dumps(scraped_data["summary"], ensure_ascii=False, indent=2)
    compact_summary, summary_compacted = compact_text_for_prompt(summary_json, STEP2_SUMMARY_MAX_CHARS)
    if summary_compacted:
        log(f"  Step2 summary compacted: {len(summary_json):,} -> {len(compact_summary):,} chars")

    compact_structures = []
    for index, structure in enumerate(scraped_data["structures"], 1):
        compact_structure, structure_compacted = compact_structure_for_prompt(
            structure,
            STEP2_STRUCTURE_MAX_CHARS,
        )
        if structure_compacted:
            log(
                f"  Step2 article_{index} compacted: "
                f"{len(structure):,} -> {len(compact_structure):,} chars"
            )
        compact_structures.append(compact_structure or "（データなし）")
    return compact_summary, compact_structures


def prepare_step3_prompt_structures(scraped_data: dict) -> str:
    parts = []
    for index, structure in enumerate(scraped_data["structures"], 1):
        if not structure:
            continue
        compact_structure, structure_compacted = compact_structure_for_prompt(
            structure,
            STEP3_STRUCTURE_MAX_CHARS,
        )
        if structure_compacted:
            log(
                f"  Step3 article_{index} compacted: "
                f"{len(structure):,} -> {len(compact_structure):,} chars"
            )
        parts.append(f"\n### 記事{index}\n{compact_structure}\n")
    return "".join(parts)


# ========================================
# 環境変数・設定読み込み
# ========================================
def load_api_key() -> str:
    """ANTHROPIC_API_KEYを取得"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        env_path = os.path.join(SCRIPT_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip("'\"")
                        break
    if not api_key:
        print("Error: ANTHROPIC_API_KEY が設定されていません。")
        print("  .env に ANTHROPIC_API_KEY=your-key を追加してください。")
        sys.exit(1)
    return api_key


def load_genre(genre_id: str) -> dict:
    """ジャンル設定を読み込む"""
    genre_path = os.path.join(GENRES_DIR, f"{genre_id}.json")
    if not os.path.exists(genre_path):
        available = [os.path.splitext(os.path.basename(f))[0]
                     for f in glob.glob(os.path.join(GENRES_DIR, "*.json"))]
        print(f"Error: ジャンル設定が見つかりません: {genre_path}")
        print(f"  利用可能なジャンル: {', '.join(available) if available else 'なし'}")
        sys.exit(1)
    with open(genre_path, encoding="utf-8") as f:
        return json.load(f)


def _backfill_competitor_summary(summary: dict, keyword: str) -> bool:
    """summary.json に competitor_summary が無い旧データに対して、その場で再計算する。
    成功すれば True を返し、呼び出し側でディスクに永続化する。"""
    if summary.get("competitor_summary"):
        return False
    articles = summary.get("articles") or []
    if not articles:
        return False
    try:
        # scrape.py は bs4 を import するため、未インストール環境（テスト等）では失敗する。
        # 失敗時は静かにスキップして既存挙動にフォールバック。
        from scrape import build_competitor_summary
    except Exception as e:
        log(f"  競合サマリ再計算スキップ（scrape import 失敗）: {e}")
        return False
    target_keyword = summary.get("keyword") or keyword
    summary["competitor_summary"] = build_competitor_summary(target_keyword, articles)
    return True


def load_scraped_data(keyword: str, scraped_dir: str = None) -> dict:
    """スクレイピングデータを読み込む"""
    scraped_dir = scraped_dir or get_keyword_scraped_dir(keyword)

    summary_path = os.path.join(scraped_dir, "summary.json")
    if not os.path.exists(summary_path):
        print(f"Error: スクレイピングデータが見つかりません: {summary_path}")
        print("  先に scrape.py を実行してください。")
        sys.exit(1)

    with open(summary_path, encoding="utf-8") as f:
        summary = json.load(f)

    # 旧 summary.json には competitor_summary が無いので、その場で計算して永続化する
    if _backfill_competitor_summary(summary, keyword):
        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            log("  competitor_summary を再計算して summary.json に保存")
        except Exception as e:
            log(f"  competitor_summary 永続化に失敗（メモリ上は反映済み）: {e}")

    structures = []
    for i in range(1, 4):
        path = os.path.join(scraped_dir, f"article_{i}_structure.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                structures.append(f.read())
        else:
            structures.append("")

    return {"summary": summary, "structures": structures}


def _fetch_suggest_only(keyword: str) -> dict:
    """旧 search_results.json に suggest が無い場合のバックフィル用。
    Serper API を最小コールで呼び出して PAA / 関連検索だけ取得する。"""
    if not keyword:
        return {}
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        return {}
    try:
        from search_keyword import search_google
        _, suggest = search_google(keyword, api_key, max_results=10)
        return suggest or {}
    except Exception as e:
        log(f"  suggest 再取得スキップ: {e}")
        return {}


def load_search_suggest(output_key: str) -> dict:
    """search_keyword.py が保存した検索結果から People Also Ask / 関連検索を取り出す。
    旧データに suggest が無ければ Serper を再取得して保存する。Step 2 のキーワード設計に使用。"""
    try:
        output_dir = get_output_dir_for_key(output_key)
    except Exception:
        return {}
    keyword_slug = keyword_to_slug(output_key)
    path = os.path.join(output_dir, f"{keyword_slug}_search_results.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    suggest = data.get("suggest") or {}

    # 旧データに suggest が無い場合は Serper を再呼び出ししてバックフィル
    has_paa = bool(suggest.get("people_also_ask"))
    has_related = bool(suggest.get("related_searches"))
    if not has_paa and not has_related:
        keyword = data.get("keyword") or output_key
        new_suggest = _fetch_suggest_only(keyword)
        if new_suggest.get("people_also_ask") or new_suggest.get("related_searches"):
            suggest = new_suggest
            data["suggest"] = suggest
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                log("  suggest を Serper から再取得して search_results.json に保存")
            except Exception as e:
                log(f"  suggest 永続化に失敗（メモリ上は反映済み）: {e}")

    return {
        "people_also_ask": suggest.get("people_also_ask") or [],
        "related_searches": suggest.get("related_searches") or [],
    }


def build_seo_brief(summary: dict, suggest: dict, keyword: str) -> str:
    """競合の集計とサジェストキーワードを Step 2 プロンプト用の人間可読ブリーフに整形する。"""
    cs = (summary or {}).get("competitor_summary") or {}
    if not cs and not suggest:
        return ""

    lines: list[str] = []
    n = cs.get("competitor_count") or 0

    char_stats = cs.get("char_stats") or {}
    if char_stats:
        lines.append(
            f"- 競合 {n} 記事の文字数: 中央値 {int(char_stats.get('median', 0)):,} / 平均 {int(char_stats.get('avg', 0)):,} / 最大 {int(char_stats.get('max', 0)):,}"
        )
    h2_stats = cs.get("h2_count_stats") or {}
    if h2_stats:
        lines.append(
            f"- 競合のH2見出し数: 中央値 {int(h2_stats.get('median', 0))} / 平均 {h2_stats.get('avg', 0)} / 最大 {int(h2_stats.get('max', 0))}"
        )
    h3_stats = cs.get("h3_count_stats") or {}
    if h3_stats:
        lines.append(
            f"- 競合のH3見出し数: 中央値 {int(h3_stats.get('median', 0))} / 平均 {h3_stats.get('avg', 0)} / 最大 {int(h3_stats.get('max', 0))}"
        )

    coverage = cs.get("keyword_coverage") or {}
    if coverage and n:
        lines.append(
            f"- メインキーワード「{keyword}」のH2含有: {coverage.get('articles_with_keyword_in_h2', 0)}/{n} 記事 "
            f"(各記事のH2のうち平均 {int(coverage.get('h2_keyword_ratio_avg', 0) * 100)}%)"
        )
        lines.append(
            f"- 同 H3含有: {coverage.get('articles_with_keyword_in_h3', 0)}/{n} 記事 "
            f"(各記事のH3のうち平均 {int(coverage.get('h3_keyword_ratio_avg', 0) * 100)}%)"
        )
        lines.append(
            f"- title 含有: {coverage.get('in_title', 0)}/{n} / meta含有: {coverage.get('in_meta', 0)}/{n} / 導入文含有: {coverage.get('in_intro', 0)}/{n}"
        )

    must = cs.get("must_include_features") or []
    if must:
        lines.append(
            f"- 70%以上の競合に必ず登場するブロック（必須化対象）: {', '.join(must)}"
        )
    common = [f for f in (cs.get("common_features") or []) if f not in must]
    if common:
        lines.append(
            f"- 50%以上の競合に登場するブロック（採用推奨）: {', '.join(common)}"
        )

    common_h2 = cs.get("common_h2_topics") or []
    if common_h2:
        topics = ", ".join(f"{t['phrase']}({t['count']}件)" for t in common_h2[:8])
        lines.append(f"- 複数競合に共通するH2フレーズ（重複は無視して内容として取り入れる）: {topics}")

    # 1位記事（スコア最上位 ≒ SERP上位）のプロファイル
    top = cs.get("top_article_profile") or {}
    if top:
        lines.append("")
        lines.append("## 1位記事の構造プロファイル（**主軸の参照ターゲット**）")
        if top.get("url"):
            lines.append(f"- URL: {top.get('url')}")
        lines.append(
            f"- 全体: 文字数 {int(top.get('total_chars', 0)):,} / H2 {top.get('h2_count', 0)}個 / H3 {top.get('h3_count', 0)}個"
        )
        # 導入の構造
        intro_marks = []
        intro_marks.append("一覧ボックスあり" if top.get("intro_has_index_box") else "一覧ボックスなし")
        intro_marks.append("早見表あり" if top.get("intro_has_table") else "早見表なし")
        if top.get("intro_has_toc"):
            intro_marks.append("目次あり")
        lines.append(f"- 導入部: {' / '.join(intro_marks)}")
        # クリニックや論点H3の典型構造
        lines.append(
            f"- H3単位の構造: テーブル平均 {top.get('h3_table_count_avg', 0)}個 / "
            f"テーブルあり率 {top.get('h3_with_table_pct', 0)}% / "
            f"H3本文の中央値 {int(top.get('h3_text_length_median', 0)):,}字"
        )
        lines.append(
            f"- H3単位の付帯要素: 口コミブロックあり率 {top.get('h3_with_reviews_pct', 0)}% / "
            f"公式サイトCTAあり率 {top.get('h3_with_official_button_pct', 0)}% / "
            f"Googleマップあり率 {top.get('h3_with_map_pct', 0)}%"
        )
        # 1位記事の H2 単位ビュー（本記事の H2 設計はここに合わせる）
        h2_secs = top.get("h2_sections") or []
        if h2_secs:
            lines.append("")
            lines.append("### 1位記事 H2 単位の構造（本記事はこの並びと数を踏襲する）")
            for i, sec in enumerate(h2_secs, 1):
                heading = sec.get("heading", "")
                clen = int(sec.get("content_length", 0) or 0)
                h3c = int(sec.get("h3_count", 0) or 0)
                h3avg = int(sec.get("h3_text_length_avg", 0) or 0)
                # ブロック種別を簡潔に
                bs = sec.get("block_summary") or {}
                # ノイズ除去（汎用ブロック）
                meaningful = {k: v for k, v in bs.items() if k not in {"テキスト", "テキストブロック", "リスト", "リスト型ブロック"}}
                block_str = " / ".join(f"{k}×{v}" for k, v in sorted(meaningful.items(), key=lambda x: -x[1])[:5])
                line = f"  {i}. 「{heading}」 — 本文 {clen:,}字 / H3 {h3c}個"
                if h3c > 0:
                    line += f"（H3平均 {h3avg:,}字）"
                if block_str:
                    line += f" / 機能: {block_str}"
                lines.append(line)

    paa = (suggest or {}).get("people_also_ask") or []
    if paa:
        lines.append(
            "- People Also Ask（読者がよく検索する関連質問）:"
        )
        for q in paa[:6]:
            lines.append(f"  - {q.get('question','')}")
    related = (suggest or {}).get("related_searches") or []
    if related:
        lines.append(
            f"- 関連検索キーワード（H2/H3 か本文にできるだけ含める）: {', '.join(related[:10])}"
        )

    return "\n".join(lines)


# ========================================
# Claude API呼び出し
# ========================================
def call_claude(system_prompt: str, user_prompt: str, api_key: str,
                max_tokens: int = 8192,
                model: str = CLAUDE_MODEL_STEP3):
    """Claude APIを呼び出す

    Returns:
        (content, stop_reason, usage)
    """
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    req = urllib.request.Request(CLAUDE_API_URL, data=body, headers=headers)

    max_retries = 1 + len(CLAUDE_RETRY_DELAYS)
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())

            content = data["content"][0]["text"]
            stop_reason = data.get("stop_reason", "")
            usage = data.get("usage", {})
            return content, stop_reason, usage

        except urllib.error.HTTPError as e:
            error_body = e.read().decode()[:500]
            if e.code in (429, 529) and attempt < max_retries - 1:
                retry_after = 0
                try:
                    retry_after = int((e.headers or {}).get("retry-after", "0"))
                except Exception:
                    retry_after = 0
                wait = max(CLAUDE_RETRY_DELAYS[attempt], retry_after)
                log(f"  API {e.code} エラー。{wait}秒後にリトライ... ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            elif e.code == 401:
                print(f"Error: APIキーが無効です (401)")
                sys.exit(1)
            else:
                print(f"Error: Claude API エラー ({e.code})")
                print(f"  {error_body}")
                sys.exit(1)

        except Exception as e:
            if attempt < max_retries - 1:
                wait = CLAUDE_RETRY_DELAYS[min(attempt, len(CLAUDE_RETRY_DELAYS) - 1)]
                log(f"  接続エラー: {e}。{wait}秒後にリトライ...")
                time.sleep(wait)
                continue
            print(f"Error: Claude API 接続失敗: {e}")
            sys.exit(1)


def accumulate_usage(total: dict, delta: dict) -> None:
    for key, value in (delta or {}).items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


def call_claude_until_complete(
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    *,
    max_tokens: int,
    model: str,
    continuation_log: str,
    continuation_builder,
) -> tuple[str, dict]:
    content, stop_reason, usage = call_claude(
        system_prompt, user_prompt, api_key, max_tokens=max_tokens, model=model
    )

    while stop_reason == "max_tokens":
        log(continuation_log)
        continuation_prompt = continuation_builder(content)
        next_content, stop_reason, next_usage = call_claude(
            system_prompt, continuation_prompt, api_key, max_tokens=max_tokens, model=model
        )
        content += next_content
        accumulate_usage(usage, next_usage)

    return content, usage


# ========================================
# プロンプトの外部ファイル読み込み
# ========================================
# 旧仕様: STEP2_SYSTEM / STEP2_USER / STEP3_SYSTEM / STEP3_USER をハードコード保持
# 新仕様: prompts/step2_system.md などからロード。ルール正本は docs/ARTICLE_RULES.md
# - {article_rules} プレースホルダーには docs/ARTICLE_RULES.md の全文が注入される
# - これにより「ルールを書いたのに参照されない」問題を構造的に解消する
_PROMPTS_DIR = os.path.join(SCRIPT_DIR, "prompts")
_RULES_PATH = os.path.join(SCRIPT_DIR, "docs", "ARTICLE_RULES.md")


def _load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_prompt(filename: str) -> str:
    """prompts/{filename} を読み込み、{article_rules} プレースホルダーに ARTICLE_RULES.md を注入する。"""
    raw = _load_text(os.path.join(_PROMPTS_DIR, filename))
    rules = _load_text(_RULES_PATH)
    # {article_rules} を最初に置換 (str.format より前にやることで、rules 内の中括弧が壊れない)
    return raw.replace("{article_rules}", rules)


# ========================================
# Step 2: タグ構成設計
# ========================================
STEP2_SYSTEM = _load_prompt("step2_system.md")

STEP2_USER = _load_prompt("step2_user.md")


def run_step2(
    keyword: str,
    genre: dict,
    scraped_data: dict,
    api_key: str,
    output_key: str | None = None,
    variant_index: int = 1,
    variant_count: int = 1,
    survey_policy: dict | None = None,
) -> str:
    """Step 2: タグ構成設計"""
    log("Step 2: タグ構成設計 開始")

    resolved_output_key = resolve_output_key(keyword, output_key)
    genre_json = json.dumps(genre, ensure_ascii=False, indent=2)
    summary_json, compact_structures = prepare_step2_prompt_context(scraped_data)
    variant_instruction = build_editorial_variant_instruction(variant_index, variant_count)
    survey_prompt_instruction = build_survey_prompt_instruction(survey_policy or {})
    suggest = load_search_suggest(resolved_output_key)
    seo_brief = build_seo_brief(scraped_data.get("summary") or {}, suggest, keyword) or "（競合集計データなし）"

    user_prompt = STEP2_USER.format(
        keyword=keyword,
        genre_name=genre.get("genre_name", ""),
        genre_json=genre_json,
        scraped_summary=summary_json,
        article_1_structure=compact_structures[0] if len(compact_structures) > 0 else "（データなし）",
        article_2_structure=compact_structures[1] if len(compact_structures) > 1 else "（データなし）",
        article_3_structure=compact_structures[2] if len(compact_structures) > 2 else "（データなし）",
        current_year=datetime.now().year,
        editorial_variant_instruction=variant_instruction,
        survey_prompt_instruction=survey_prompt_instruction,
        seo_brief=seo_brief,
    )

    total_chars = len(STEP2_SYSTEM) + len(user_prompt)
    log(f"  入力: {total_chars:,} 文字")
    log("  Claude API 呼び出し中...")

    content, stop_reason, usage = call_claude(
        STEP2_SYSTEM,
        user_prompt,
        api_key,
        max_tokens=MAX_TOKENS_STEP2,
        model=CLAUDE_MODEL_STEP2,
    )

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    log(f"  完了: {len(content):,} 文字出力 (入力: {input_tokens:,} tokens, 出力: {output_tokens:,} tokens)")

    if stop_reason == "max_tokens":
        log("  ⚠ 出力が途中で切れています（max_tokens到達）")

    content = apply_editorial_variant_to_tag_structure(content, variant_index, variant_count)
    ensure_no_forbidden_template_markers(content, "タグ構成")

    # 保存
    keyword_slug = keyword_to_slug(resolved_output_key)
    output_dir = ensure_output_dir_for_key(resolved_output_key)
    tag_path = os.path.join(output_dir, f"{keyword_slug}_タグ構成.md")
    with open(tag_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"  保存: {tag_path}")

    return content


# ========================================
# Step 3: 本文HTML生成
# ========================================
STEP3_SYSTEM = _load_prompt("step3_system.md")

STEP3_USER = _load_prompt("step3_user.md")


def run_step3(
    keyword: str,
    genre_id: str | None,
    genre: dict,
    tag_structure: str,
    scraped_data: dict,
    api_key: str,
    output_key: str | None = None,
    variant_index: int = 1,
    variant_count: int = 1,
    survey_policy: dict | None = None,
) -> str:
    """Step 3: 本文HTML生成"""
    log("Step 3: 本文HTML生成 開始")

    resolved_output_key = resolve_output_key(keyword, output_key)
    resolved_genre_id = genre_id or genre.get("genre_id") or "aga"
    keyword_slug = keyword_to_slug(resolved_output_key)
    genre_json = json.dumps(genre, ensure_ascii=False, indent=2)
    scraped_text = prepare_step3_prompt_structures(scraped_data)
    scraped_summary_json = json.dumps(scraped_data["summary"], ensure_ascii=False, indent=2)
    scraped_summary, summary_compacted = compact_text_for_prompt(
        scraped_summary_json,
        STEP3_SUMMARY_MAX_CHARS,
    )
    if summary_compacted:
        log(f"  Step3 summary compacted: {len(scraped_summary_json):,} -> {len(scraped_summary):,} chars")

    variant_instruction = build_editorial_variant_instruction(variant_index, variant_count)
    survey_prompt_instruction = build_survey_prompt_instruction(survey_policy or {})
    user_prompt = STEP3_USER.format(
        keyword=keyword,
        keyword_slug=keyword_slug,
        genre_name=genre.get("genre_name", ""),
        genre_json=genre_json,
        tag_structure=tag_structure,
        scraped_structures=scraped_text,
        current_year=datetime.now().year,
        editorial_variant_instruction=variant_instruction,
        survey_prompt_instruction=survey_prompt_instruction,
    )

    total_chars = len(STEP3_SYSTEM) + len(user_prompt)
    log(f"  入力: {total_chars:,} 文字")
    sections = parse_tag_structure_sections(tag_structure)
    use_sectional_generation = (
        len(sections) >= STEP3_SECTIONAL_H2_THRESHOLD
        or len(tag_structure) >= 12000
    )

    if use_sectional_generation:
        log("  記事が長いため、H2単位の分割生成モードで本文を組み立てます...")
        usage = {"input_tokens": 0, "output_tokens": 0}
        content_parts = []

        intro_markdown = extract_intro_markdown(tag_structure)
        intro_prompt = f"""以下の「導入部分」だけをWordPress用HTMLで出力してください。

## 重要
- この導入パートでは `<h2>` や `<h3>` を使わない
- タグ構成設計書の導入部分に指定された要素だけを出力する
- 目次はWordPress側で自動生成されるので出力しない
- `<!-- ↑ここまでが導入部分。この直後にWordPress自動生成の目次が入る -->` のコメントを最後に入れる
- ショートコード指定がある場合は、そのまま出力してよい
- 不明な情報は書かず、プレースホルダーも残さない

## キーワード
{keyword}

## 導入部分のタグ構成抜粋
{intro_markdown or "導入指定なし"}

## タグ構成設計書の冒頭
{tag_structure[:2500]}

## 競合記事の要約
{scraped_summary}
"""
        intro_html, intro_usage = call_claude_until_complete(
            STEP3_SYSTEM,
            intro_prompt,
            api_key,
            max_tokens=MAX_TOKENS_STEP3,
            model=CLAUDE_MODEL_STEP3,
            continuation_log="  導入生成が途中で切れたため、続きを要求中...",
            continuation_builder=lambda current: (
                "導入パートの続きを、重複なしでHTMLだけ出力してください。前回の末尾:\n"
                + current[-500:]
            ),
        )
        accumulate_usage(usage, intro_usage)
        intro_html = normalize_html_fragment(intro_html)
        intro_html = re.split(r"(?i)<h2\b", intro_html, maxsplit=1)[0].strip()
        if intro_html:
            content_parts.append(intro_html)

        for index, section in enumerate(sections, 1):
            section_markdown = extract_section_markdown(tag_structure, section["h2"])
            log(f"  H2分割生成中... ({index}/{len(sections)}: {section['h2']})")
            if section["h3s"] and should_split_section_by_h3(section_markdown, section["h3s"]):
                log(
                    "  H2配下が大きいため、H3チャンク分割モードで生成します... "
                    f"({len(section['h3s'])}件)"
                )
                section_parts = []
                intro_markdown = extract_section_intro_markdown(section_markdown)
                intro_prompt = f"""以下の1セクションの「H2直下の導入部分だけ」をHTMLで出力してください。

## 重要
- 出力はこのH2セクションだけに限定する
- 最初の行は必ず `<h2>{section["h2"]}</h2>` から始める
- `<h3>` は一切出力しない
- H2見出しテキストは1文字も変えない
- 対象H2の直下に置く説明・比較表・注意書き・ショートコードだけを出力する
- 不明な情報は削る。推測で埋めない
- プレースホルダーやコメントは出力しない

## キーワード
{keyword}

## 対象H2
{section["h2"]}

## このH2の導入部分のタグ構成抜粋
{intro_markdown or f"### [H2] {section['h2']}"}

## 競合記事の構造データ
{scraped_text}

## 競合記事の要約
{scraped_summary}
"""
                intro_html, intro_usage = call_claude_until_complete(
                    STEP3_SYSTEM,
                    intro_prompt,
                    api_key,
                    max_tokens=MAX_TOKENS_STEP3,
                    model=CLAUDE_MODEL_STEP3,
                    continuation_log="  H2導入生成が途中で切れたため、続きを要求中...",
                    continuation_builder=lambda current, heading=section["h2"]: (
                        f"前回のH2 `{heading}` 導入部分の続きを、重複なしでHTMLだけ出力してください。末尾:\n"
                        + current[-500:]
                    ),
                )
                accumulate_usage(usage, intro_usage)
                intro_html = normalize_html_fragment(intro_html)
                intro_html = re.split(r"(?i)<h3\b", intro_html, maxsplit=1)[0].strip()
                if not intro_html:
                    intro_html = f"<h2>{section['h2']}</h2>"
                section_parts.append(intro_html)

                h3_chunks = chunk_list(section["h3s"], STEP3_SECTION_H3_CHUNK_SIZE)
                for chunk_index, h3_chunk in enumerate(h3_chunks, 1):
                    chunk_markdown = "\n\n".join(
                        extract_h3_markdown(section_markdown, h3) or f"#### [H3] {h3}"
                        for h3 in h3_chunk
                    )
                    log(
                        "  H3チャンク生成中... "
                        f"({chunk_index}/{len(h3_chunks)}: {h3_chunk[0]}"
                        f"{' ほか' if len(h3_chunk) > 1 else ''})"
                    )
                    chunk_prompt = f"""以下のH3サブセクションだけをHTMLで出力してください。

## 重要
- 出力は指定されたH3サブセクションだけに限定する
- `<h2>` は出力しない
- 最初の行は必ず `<h3>{h3_chunk[0]}</h3>` から始める
- 下記H3をこの順序どおりにすべて含める
- H3見出しテキストは1文字も変えない
- 不明な情報は削る。推測で埋めない
- プレースホルダーやコメントは出力しない

## キーワード
{keyword}

## 親H2
{section["h2"]}

## このチャンクで必ず含めるH3
{chr(10).join("- " + h for h in h3_chunk)}

## このチャンクのタグ構成抜粋
{chunk_markdown}

## 競合記事の構造データ
{scraped_text}

## 競合記事の要約
{scraped_summary}
"""
                    chunk_html, chunk_usage = call_claude_until_complete(
                        STEP3_SYSTEM,
                        chunk_prompt,
                        api_key,
                        max_tokens=MAX_TOKENS_STEP3,
                        model=CLAUDE_MODEL_STEP3,
                        continuation_log="  H3チャンク生成が途中で切れたため、続きを要求中...",
                        continuation_builder=lambda current, heading=h3_chunk[0]: (
                            f"前回のH3 `{heading}` から始まるチャンクの続きを、重複なしでHTMLだけ出力してください。末尾:\n"
                            + current[-500:]
                        ),
                    )
                    accumulate_usage(usage, chunk_usage)
                    chunk_html = normalize_html_fragment(chunk_html)
                    chunk_html = strip_h2_tags(chunk_html)
                    if chunk_html:
                        section_parts.append(chunk_html)

                content_parts.append("\n\n".join(part for part in section_parts if part.strip()))
                continue

            section_prompt = f"""以下の1セクションだけをHTMLで出力してください。

## 重要
- 出力はこのH2セクションだけに限定する
- 最初の行は必ず `<h2>{section["h2"]}</h2>` から始める
- 下記のH3が指定されている場合は、そのH3をこの順序どおりにすべて含める
- H2/H3見出しテキストは1文字も変えない
- タグ構成設計書にあるブロック要件に従う
- 不明な情報は削る。推測で埋めない
- プレースホルダーやコメントは出力しない

## キーワード
{keyword}

## 対象H2
{section["h2"]}

## このH2配下に必ず含めるH3
{chr(10).join("- " + h for h in section["h3s"]) if section["h3s"] else "なし"}

## このH2のタグ構成抜粋
{section_markdown}

## 競合記事の構造データ
{scraped_text}

## 競合記事の要約
{scraped_summary}
"""
            section_html, section_usage = call_claude_until_complete(
                STEP3_SYSTEM,
                section_prompt,
                api_key,
                max_tokens=MAX_TOKENS_STEP3,
                model=CLAUDE_MODEL_STEP3,
                continuation_log="  セクション生成が途中で切れたため、続きを要求中...",
                continuation_builder=lambda current, heading=section["h2"]: (
                    f"前回のH2 `{heading}` セクションの続きを、重複なしでHTMLだけ出力してください。末尾:\n"
                    + current[-500:]
                ),
            )
            accumulate_usage(usage, section_usage)
            content_parts.append(normalize_html_fragment(section_html))

        content = "\n\n".join(part for part in content_parts if part.strip())
    else:
        log("  Claude API 呼び出し中（時間がかかります）...")
        content, usage = call_claude_until_complete(
            STEP3_SYSTEM,
            user_prompt,
            api_key,
            max_tokens=MAX_TOKENS_STEP3,
            model=CLAUDE_MODEL_STEP3,
            continuation_log="  出力が途中で切れたため、続きを要求中...",
            continuation_builder=lambda current: (
                "続きを出力してください。前回の出力の末尾:\n" + current[-500:]
            ),
        )
        content = normalize_html_fragment(content)

    missing_h2s, missing_h3s = find_missing_headings(tag_structure, content)
    section_map = {section["h2"]: section for section in sections}
    h3_to_h2 = {
        h3: section["h2"]
        for section in sections
        for h3 in section["h3s"]
    }
    repair_count = 0
    max_repair_attempts = MAX_SECTION_REPAIR_ATTEMPTS * max(len(sections), 1)
    while (missing_h2s or missing_h3s) and repair_count < max_repair_attempts:
        if missing_h2s:
            target_h2 = missing_h2s[0]
        else:
            target_h2 = h3_to_h2.get(missing_h3s[0], "")
        section = section_map.get(target_h2, {"h2": target_h2, "h3s": []})
        section_markdown = extract_section_markdown(tag_structure, target_h2)
        repair_count += 1
        log(
            "  不足セクションを補完中... "
            f"(対象H2: {target_h2}, 不足H2: {len(missing_h2s)}件, 不足H3: {len(missing_h3s)}件, {repair_count}/{max_repair_attempts})"
        )
        repair_prompt = f"""以下の1セクションだけをHTMLで出力してください。

## 重要
- 出力はこのH2セクションだけに限定する
- 最初の行は必ず `<h2>{section["h2"]}</h2>` から始める
- 下記のH3が指定されている場合は、そのH3をこの順序どおりにすべて含める
- H2/H3見出しテキストは1文字も変えない。数字・期間・括弧・ `◯` などの記号も言い換えない
- 既存セクションの再出力は禁止
- `※要確認` や説明コメントを出力しない
- 比較記事でない場合は、比較記事向けテンプレートを無理に使わない

## 対象H2
{section["h2"]}

## このH2配下に必ず含めるH3
{chr(10).join("- " + h for h in section["h3s"]) if section["h3s"] else "なし"}

## このH2のタグ構成抜粋
{section_markdown}

## 競合記事のサマリー
{scraped_summary}

## 既存記事の直前コンテキスト
{content[-2500:]}
"""
        repair_html, repair_stop_reason, repair_usage = call_claude(
            STEP3_SYSTEM,
            repair_prompt,
            api_key,
            max_tokens=MAX_TOKENS_STEP3,
            model=CLAUDE_MODEL_STEP3,
        )
        for key in repair_usage:
            if key in usage and isinstance(usage[key], int):
                usage[key] += repair_usage[key]

        while repair_stop_reason == "max_tokens":
            log("  セクション補完が途中で切れたため、続きを要求中...")
            continuation_prompt = (
                f"前回のH2 `{section['h2']}` セクションの続きを、重複なしで続けてHTMLだけ出力してください。末尾:\n"
                + repair_html[-500:]
            )
            next_content, repair_stop_reason, next_usage = call_claude(
                STEP3_SYSTEM,
                continuation_prompt,
                api_key,
                max_tokens=MAX_TOKENS_STEP3,
                model=CLAUDE_MODEL_STEP3,
            )
            repair_html += next_content
            for key in next_usage:
                if key in usage and isinstance(usage[key], int):
                    usage[key] += next_usage[key]

        repair_html = normalize_html_fragment(repair_html)
        content = replace_or_append_h2_section(content, target_h2, repair_html)
        missing_h2s, missing_h3s = find_missing_headings(tag_structure, content)

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    log(f"  完了: {len(content):,} 文字出力 (入力: {input_tokens:,} tokens, 出力: {output_tokens:,} tokens)")

    # 後処理: コードフェンス除去とHTML断片の正規化
    content = normalize_html_fragment(content)
    content = cleanup_generated_html(content, keyword_slug)
    content = inline_variant_shortcodes_in_html(
        content,
        genre_id=resolved_genre_id,
        output_key=resolved_output_key,
        variant_index=variant_index,
    )
    # validate_article_html は clinic-summary-table / clinic-detail-table のクラスを厳密に要求するが、
    # Claude の出力はクラス漏れすることがある。バリデーション前に sanitize_article の自動クラス補完を
    # 走らせて Claude の付け忘れを救う。これにより無駄なリトライ（同じエラーで Claude API 3回分の浪費）を防ぐ。
    try:
        from sanitize_article import normalize_table_classes, ensure_table_wrappers
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(content, "lxml")
        normalize_table_classes(soup)
        ensure_table_wrappers(soup)
        content = str(soup)
    except Exception as e:
        log(f"  警告: テーブルクラスの自動補完に失敗（バリデーションを続行）: {e}")
    missing_h2s, missing_h3s = find_missing_headings(tag_structure, content)
    if missing_h2s or missing_h3s:
        print("Error: タグ構成どおりに本文を出力し切れていません。")
        if missing_h2s:
            print("  不足H2: " + " / ".join(missing_h2s[:5]))
        if missing_h3s:
            print("  不足H3: " + " / ".join(missing_h3s[:5]))
        sys.exit(1)
    issues = validate_article_html(content, keyword_slug)
    if issues:
        print("Error: 生成HTMLの構造検証に失敗しました。")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)

    # 保存
    output_dir = ensure_output_dir_for_key(resolved_output_key)
    ensure_common_assets_for_key(resolved_output_key)
    html_path = os.path.join(output_dir, f"{keyword_slug}_記事.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"  保存: {html_path}")

    return content


# ========================================
# メインのオーケストレーション
# ========================================
def generate_article(
    keyword: str,
    genre_id: str,
    scraped_dir: str = None,
    step: int = None,
    output_key: str | None = None,
    site_config_path: str | None = None,
    variant_index: int = 1,
    variant_count: int = 1,
) -> dict:
    """記事自動生成のメイン関数

    Args:
        keyword: 検索キーワード
        genre_id: ジャンルID（aga, ed, hair_removal等）
        scraped_dir: スクレイピングデータのディレクトリ
        step: None=両方, 2=Step2のみ, 3=Step3のみ

    Returns:
        dict: 実行結果
    """
    api_key = load_api_key()
    genre = load_genre(genre_id)
    resolved_output_key = resolve_output_key(keyword, output_key)
    shortcodes = dict(genre.get("shortcodes", {}))
    if shortcodes.get("早見表"):
        shortcodes["早見表"] = resolve_variant_embed_html(
            shortcodes["早見表"],
            genre_id=genre_id,
            output_key=resolved_output_key,
            variant_index=variant_index,
        )
        genre = {**genre, "shortcodes": shortcodes}
    scraped_data = load_scraped_data(keyword, scraped_dir)
    keyword_slug = keyword_to_slug(resolved_output_key)
    output_dir = ensure_output_dir_for_key(resolved_output_key)
    variant_profile = build_variant_profile(variant_index, variant_count)
    site_url = ""
    if site_config_path and os.path.exists(site_config_path):
        try:
            with open(site_config_path, encoding="utf-8") as f:
                site_config_data = json.load(f)
            site_url = site_config_data.get("site_url", "")
        except Exception:
            site_url = ""
    survey_policy = build_survey_policy(
        keyword,
        genre_id,
        is_nandemo=is_nandemo_output_key(resolved_output_key),
        site_url=site_url,
        variant_index=variant_index,
        variant_count=variant_count,
    )
    result = {
        "keyword": keyword,
        "output_key": resolved_output_key,
        "genre_id": genre_id,
        "tag_structure_path": None,
        "html_path": None,
        "variant_profile": variant_profile,
        "survey_policy": survey_policy,
    }

    # Step 2: タグ構成設計
    tag_structure = None
    if step is None or step == 2:
        tag_structure = run_step2(
            keyword,
            genre,
            scraped_data,
            api_key,
            output_key=resolved_output_key,
            variant_index=variant_index,
            variant_count=variant_count,
            survey_policy=survey_policy,
        )
        result["tag_structure_path"] = os.path.join(output_dir, f"{keyword_slug}_タグ構成.md")

    # Step 3: 本文HTML生成
    if step is None or step == 3:
        # Step 2の出力を取得（Step 2を実行していない場合はファイルから読み込み）
        if tag_structure is None:
            tag_path = os.path.join(output_dir, f"{keyword_slug}_タグ構成.md")
            if not os.path.exists(tag_path):
                print(f"Error: タグ構成ファイルが見つかりません: {tag_path}")
                print("  先に --step 2 を実行してください。")
                sys.exit(1)
            with open(tag_path, encoding="utf-8") as f:
                tag_structure = f.read()
        ensure_no_forbidden_template_markers(tag_structure, "タグ構成")

        run_step3(
            keyword,
            genre_id,
            genre,
            tag_structure,
            scraped_data,
            api_key,
            output_key=resolved_output_key,
            variant_index=variant_index,
            variant_count=variant_count,
            survey_policy=survey_policy,
        )
        result["html_path"] = os.path.join(output_dir, f"{keyword_slug}_記事.html")

    log("記事生成 完了")
    return result


# ========================================
# CLI
# ========================================
def main():
    parser = argparse.ArgumentParser(description="SEO記事自動生成（Claude API）")
    parser.add_argument("--keyword", required=True, help="検索キーワード（例: AGA 横浜）")
    parser.add_argument("--genre", required=True, help="ジャンルID（例: aga, ed, hair_removal）")
    parser.add_argument("--step", type=int, choices=[2, 3],
                        help="実行ステップ（省略時は2→3を連続実行）")
    parser.add_argument("--scraped-dir",
                        help="スクレイピングデータのディレクトリ（デフォルト: scraped_data/）")
    parser.add_argument("--output-key", help="出力先の識別キー（省略時はキーワード）")
    parser.add_argument("--site-config-path", help="サイト設定JSONのパス（アンケート/デザイン方針判定に使用）")
    parser.add_argument("--variant-index", type=int, default=1, help="編集バリエーション番号")
    parser.add_argument("--variant-count", type=int, default=1, help="生成する総バリエーション数")

    args = parser.parse_args()

    print("=" * 50)
    print("SEO記事自動生成（Claude API）")
    print(f"  キーワード: {args.keyword}")
    print(f"  ジャンル: {args.genre}")
    print(f"  ステップ: {args.step or '2→3 連続実行'}")
    if args.output_key:
        print(f"  出力キー: {args.output_key}")
    if args.variant_count > 1:
        print(f"  バリエーション: {args.variant_index}/{args.variant_count}")
    print("=" * 50)
    print()

    result = generate_article(
        keyword=args.keyword,
        genre_id=args.genre,
        scraped_dir=args.scraped_dir,
        step=args.step,
        output_key=args.output_key,
        site_config_path=args.site_config_path,
        variant_index=args.variant_index,
        variant_count=args.variant_count,
    )

    print()
    print("=" * 50)
    print("結果:")
    if result.get("tag_structure_path"):
        print(f"  タグ構成: {result['tag_structure_path']}")
    if result.get("html_path"):
        print(f"  記事HTML: {result['html_path']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
