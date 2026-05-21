#!/usr/local/bin/python3.12
"""
SEO記事自動生成パイプライン（統合スクリプト）

キーワードとサイト設定を受け取り、以下を一気通貫で実行する:
  Step 0: Google検索 → 上位記事URL取得
  Step 1: 競合記事スクレイピング
  Step 2: タグ構成設計（Claude API）
  Step 3: 本文HTML生成（Claude API）
  Step 5: 公式サイトスクリーンショット（任意）
  Step 6: AI画像生成 + HTML差し込み
  Step 7: WordPress下書き投稿

使い方:
  python pipeline.py --keyword "AGA 横浜" --genre aga --site sites/aurora_clinic.json
  python pipeline.py --keyword "AGA 横浜" --genre aga --site sites/aurora_clinic.json --category "AGA"
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime

from article_audit import validate_article_html
from add_references import validate_reference_output
from bs4 import BeautifulSoup
from env_utils import load_project_env
from fill_list_box import (
    has_valid_intro_list_box,
    iter_candidate_clinic_h3_tags,
    match_anchor_to_heading,
)
from fill_reviews import html_needs_reviews
from official_site_utils import normalize_text
from output_utils import (
    ensure_output_dir_for_key,
    ensure_keyword_output_dir,
    get_output_dir_for_key,
    get_keyword_scraped_dir,
    keyword_to_slug,
    load_variant_status,
    resolve_output_key,
)
from sanitize_article import normalize_table_classes
from variant_utils import (
    build_editorial_variant_instruction,
    build_variant_output_key,
    build_variant_profile,
    extract_shortcode_name,
    get_variant_embed_marker_from_shortcode_name,
    is_nandemo_output_key,
    is_nandemo_site,
    normalize_variant_count,
    resolve_variant_shortcode,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
STATE_DIR = os.path.join(SCRIPT_DIR, "runtime_state")
STEP_RETRY_DELAYS = [5, 60]
QUALITY_RETRY_DELAYS = [5, 60]
COMPARISON_ARTICLE_TOKENS = ("比較", "おすすめ", "ランキング")
STEP_SEQUENCE = [
    "search",
    "scrape",
    "tag_structure",
    "generate_html",
    "fill_list_box",
    "fact_check",
    "sanitize_article",
    "references",
    "fill_reviews",
    "fill_final_cta",
    "fill_maps",
    "screenshots",
    "logos",
    "wordpress",
]
FINAL_STATUS_TO_STEP = {
    "failed_at_search": "search",
    "search_output_invalid": "search",
    "failed_at_scrape": "scrape",
    "scrape_output_invalid": "scrape",
    "failed_at_tag_structure": "tag_structure",
    "tag_structure_credit_exhausted": "tag_structure",
    "tag_structure_output_invalid": "tag_structure",
    "failed_at_generate_html": "generate_html",
    "generate_html_credit_exhausted": "generate_html",
    "html_output_invalid": "generate_html",
    "failed_at_fill_list_box": "fill_list_box",
    "list_box_output_invalid": "fill_list_box",
    "failed_at_fact_check": "fact_check",
    "fact_check_output_invalid": "fact_check",
    "failed_at_sanitize_article": "sanitize_article",
    "sanitize_article_output_invalid": "sanitize_article",
    "failed_at_references": "references",
    "references_output_invalid": "references",
    "failed_at_fill_reviews": "fill_reviews",
    "reviews_output_invalid": "fill_reviews",
    "failed_at_fill_final_cta": "fill_final_cta",
    "final_cta_output_invalid": "fill_final_cta",
    "failed_at_fill_maps": "fill_maps",
    "map_output_invalid": "fill_maps",
    "failed_at_screenshots": "screenshots",
    "screenshot_output_invalid": "screenshots",
    "failed_at_logos": "logos",
    "logos_output_invalid": "logos",
    "failed_at_wordpress": "wordpress",
}


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


def is_anthropic_credit_error(text: str) -> bool:
    normalized = (text or "").lower()
    return (
        "credit balance is too low" in normalized
        or "please go to plans & billing to upgrade or purchase credits" in normalized
    )

load_project_env()


def log(msg: str):
    """タイムスタンプ付きログ出力"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def get_latest_log(keyword: str) -> dict | None:
    keyword_slug = keyword_to_slug(keyword)
    pattern = os.path.join(LOG_DIR, f"{keyword_slug}_*.json")
    candidates = sorted(glob.glob(pattern))
    if not candidates:
        return None
    latest = candidates[-1]
    try:
        with open(latest, encoding="utf-8") as f:
            data = json.load(f)
        data["_log_path"] = latest
        return data
    except (OSError, json.JSONDecodeError):
        return None


def is_success_like_final_status(status: str | None) -> bool:
    if not status:
        return False
    return status == "success" or status.startswith("completed_with_errors")


def variant_is_already_completed(keyword: str, variant_index: int) -> tuple[bool, dict | None]:
    output_key = build_variant_output_key(keyword, variant_index)
    status_payload = load_variant_status(output_key)
    if status_payload and status_payload.get("completed"):
        return True, {
            "final_status": "success",
            "status_source": "variant_status",
            "variant_status": status_payload,
        }

    latest_log = get_latest_log(output_key)
    if latest_log and is_success_like_final_status(latest_log.get("final_status")):
        return True, latest_log

    return False, None


def get_next_pending_variant_index(keyword: str, scan_limit: int = 5) -> int:
    scan_limit = max(1, min(5, int(scan_limit or 1)))
    for index in range(1, scan_limit + 1):
        already_completed, _ = variant_is_already_completed(keyword, index)
        if not already_completed:
            return index
    return min(scan_limit + 1, 5)


def get_runtime_state_path(keyword: str) -> str:
    keyword_slug = keyword_to_slug(keyword)
    return os.path.join(STATE_DIR, f"{keyword_slug}.json")


def load_runtime_state(keyword: str) -> dict | None:
    path = get_runtime_state_path(keyword)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["_state_path"] = path
        return data
    except (OSError, json.JSONDecodeError):
        return None


def save_runtime_state(keyword: str, data: dict) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = get_runtime_state_path(keyword)
    payload = dict(data)
    payload["updated_at"] = datetime.now().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def copy_search_results_between_output_keys(source_key: str, target_key: str) -> str:
    source_slug = keyword_to_slug(source_key)
    target_slug = keyword_to_slug(target_key)
    source_dir = get_output_dir_for_key(source_key)
    target_dir = ensure_output_dir_for_key(target_key)
    source_path = os.path.join(source_dir, f"{source_slug}_search_results.json")
    target_path = os.path.join(target_dir, f"{target_slug}_search_results.json")
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"shared search results not found: {source_path}")

    with open(source_path, encoding="utf-8") as f:
        payload = json.load(f)
    payload["output_key"] = target_key
    payload["copied_from_output_key"] = source_key
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return target_path


def try_copy_search_results_between_output_keys(source_key: str, target_key: str) -> tuple[bool, str | None]:
    try:
        return True, copy_search_results_between_output_keys(source_key, target_key)
    except FileNotFoundError:
        return False, None


def can_reuse_scraped_outputs(keyword: str) -> bool:
    try:
        validate_scraped_outputs(get_keyword_scraped_dir(keyword))
        return True
    except Exception:
        return False


def infer_resume_step(final_status: str, validation_error: str = "") -> str | None:
    if final_status == "success":
        return None
    if final_status in FINAL_STATUS_TO_STEP:
        return FINAL_STATUS_TO_STEP[final_status]
    if final_status == "pre_publish_validation_failed":
        issue = validation_error or ""
        if "未確定の事実" in issue or "生成指示文" in issue:
            return "sanitize_article"
        if "参考文献" in issue or "参照元" in issue:
            return "references"
        if "口コミ" in issue:
            return "fill_reviews"
        if "一覧ボックス" in issue:
            return "fill_list_box"
        if "ショートコード" in issue or "CTA" in issue:
            return "fill_final_cta"
        if "マップ" in issue:
            return "fill_maps"
        if "スクリーンショット" in issue or "公式サイト" in issue:
            return "screenshots"
        return "sanitize_article"
    if final_status == "resume_prerequisite_invalid":
        issue = validation_error or ""
        if issue.startswith("search:"):
            return "search"
        if issue.startswith("scrape:"):
            return "scrape"
        if issue.startswith("tag_structure:"):
            return "tag_structure"
        if issue.startswith("generate_html:"):
            return "generate_html"
        if issue.startswith("fill_list_box:"):
            return "fill_list_box"
        if issue.startswith("fact_check:"):
            return "fact_check"
        if issue.startswith("sanitize_article:"):
            return "sanitize_article"
        if issue.startswith("references:"):
            return "references"
        if issue.startswith("fill_reviews:"):
            return "fill_reviews"
        if issue.startswith("fill_final_cta:"):
            return "fill_final_cta"
        if issue.startswith("fill_maps:"):
            return "fill_maps"
        if issue.startswith("screenshots:"):
            return "screenshots"
        return "generate_html"
    return None


def infer_resume_step_from_log(log_data: dict | None) -> str | None:
    if not log_data:
        return None
    return infer_resume_step(
        log_data.get("final_status", ""),
        log_data.get("validation_error", ""),
    )


def infer_resume_step_from_runtime_state(state: dict | None) -> str | None:
    if not state:
        return None
    status = state.get("status", "")
    if status == "success":
        return None
    if state.get("current_step"):
        return state["current_step"]
    if state.get("suggested_resume_step"):
        return state["suggested_resume_step"]
    return infer_resume_step(state.get("final_status", ""), state.get("validation_error", ""))


def validate_search_results(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"検索結果JSONが見つかりません: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("filtered"):
        raise ValueError("検索結果の filtered が空です")


def validate_scraped_outputs(scraped_dir: str) -> None:
    summary_path = os.path.join(scraped_dir, "summary.json")
    if not os.path.exists(summary_path):
        raise FileNotFoundError(f"スクレイピング結果が見つかりません: {summary_path}")
    article_files = [os.path.join(scraped_dir, f"article_{i}_structure.md") for i in range(1, 4)]
    if not any(os.path.exists(path) for path in article_files):
        raise FileNotFoundError("スクレイピング構造ファイルが1件もありません")


def validate_text_output(path: str, label: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{label} が見つかりません: {path}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"{label} が空です: {path}")


def validate_tag_structure_output(path: str) -> None:
    validate_text_output(path, "タグ構成ファイル")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    remaining_placeholders = re.findall(r"\{\{(?:後で作成|後で挿入|TODO):[^}]*\}\}", content)
    if remaining_placeholders:
        preview = " / ".join(remaining_placeholders[:3])
        raise ValueError(f"タグ構成に未解決のプレースホルダーが残っています: {preview}")
    if content.count("[H2]") < 2:
        raise ValueError("タグ構成にH2見出しが不足しています")
    if content.count("[H3]") < 2:
        raise ValueError("タグ構成にH3見出しが不足しています")
    if "**検索意図**:" not in content or "**記事タイプ**:" not in content:
        raise ValueError("タグ構成に検索意図または記事タイプの宣言がありません")
    banned_patterns = [
        "※以下同様",
        "以下同様",
        "以下繰り返し",
        "テンプレートで",
        "同様のテンプレート",
    ]
    for pattern in banned_patterns:
        if pattern in content:
            raise ValueError(f"タグ構成に省略記法が残っています: {pattern}")
    article_type_match = re.search(r"\*\*記事タイプ\*\*:\s*(.+)", content)
    article_type = article_type_match.group(1) if article_type_match else ""
    if any(token in article_type for token in ["比較", "おすすめ"]):
        if content.count("#### [H3]") < 5:
            raise ValueError("比較記事としては個別H3具体化が不足しています")
        # NOTE: 「一覧ボックス必須」チェックは撤廃。
        # STEP2 プロンプトで「1位記事に一覧ボックスが無ければ配置しない」と指示しているのと矛盾するため。
        # 一覧ボックスが必要かどうかは Claude が 1位記事プロファイルを見て判断する。
    if re.search(r"\*\*titleタグ\*\*:\s*.*(名古屋|新宿|大阪|横浜|福岡|札幌|池袋|渋谷)", content):
        first_line = re.search(r"# 「(.+?)」タグ構成設計", content)
        keyword = first_line.group(1) if first_line else ""
        if keyword and not re.search(r"(東京|大阪|名古屋|新宿|渋谷|池袋|横浜|福岡|札幌|千葉|埼玉|神戸|京都)", keyword):
            raise ValueError("タグ構成が地域特化に寄りすぎています")


def extract_article_type_from_content(content: str) -> str:
    match = re.search(r"\*\*記事タイプ\*\*:\s*(.+)", content)
    return match.group(1).strip() if match else ""


def extract_article_type_from_tag_structure(tag_structure_path: str | None) -> str:
    if not tag_structure_path or not os.path.exists(tag_structure_path):
        return ""
    try:
        with open(tag_structure_path, encoding="utf-8") as f:
            return extract_article_type_from_content(f.read())
    except OSError:
        return ""


def is_comparison_article_type(article_type: str) -> bool:
    return any(token in (article_type or "") for token in COMPARISON_ARTICLE_TOKENS)


def collect_section_nodes(heading, *, stop_at_h3: bool = True) -> list:
    nodes = []
    node = heading.find_next_sibling()
    while node:
        tag_name = getattr(node, "name", None)
        if tag_name == "h2":
            break
        if stop_at_h3 and tag_name == "h3":
            break
        nodes.append(node)
        node = node.find_next_sibling()
    return nodes


def section_has_official_site_button(nodes: list) -> bool:
    official_tokens = ("公式サイト", "公式ページ", "公式HP", "公式ホームページ", "公式はこちら", "サイトはこちら")
    for node in nodes:
        tag_name = getattr(node, "name", None)
        if tag_name is None:
            continue
        classes = node.get("class") or []
        if "official-site-button-wrap" in classes:
            return True
        for anchor in node.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            if not href or href == "#":
                continue
            text = normalize_text(anchor.get_text(" ", strip=True))
            if href.startswith("http") and any(token.lower() in text.lower() for token in official_tokens):
                return True
    return False


def collect_screenshot_target_sections(soup: BeautifulSoup) -> list[tuple[object, list]]:
    """スクリーンショット対象は、比較記事の個別院/サービス見出しに限定する。"""
    targets = []
    for heading in iter_candidate_clinic_h3_tags(soup):
        targets.append((heading, collect_section_nodes(heading)))
    return targets


def build_article_profile(
    html_path: str,
    tag_structure_path: str | None = None,
    expected_shortcode: str = "",
) -> dict[str, object]:
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    soup = BeautifulSoup(html, "html.parser")
    article_type = extract_article_type_from_tag_structure(tag_structure_path)
    clinic_h3_tags = iter_candidate_clinic_h3_tags(soup)
    has_clinic_sections = bool(clinic_h3_tags)
    has_list_box = has_valid_intro_list_box(soup)
    has_list_placeholder = "{{後で作成:一覧ボックス" in html
    has_review_placeholder = html_needs_reviews(html)
    has_review_section = 'class="review-section"' in html
    has_map_block = 'class="clinic-map"' in html or "{{後で作成:マップ" in html
    screenshot_targets = collect_screenshot_target_sections(soup)
    has_official_button = any(
        section_has_official_site_button(section_nodes)
        for _, section_nodes in screenshot_targets
    )

    is_comparison = is_comparison_article_type(article_type)
    # 一覧ボックスはタグ構成設計書（または本文に既存・プレースホルダーあり）が要求した場合のみ必須化する。
    # 「比較系 + クリニックH3存在」だけで強制すると、競合（特に1位記事）が一覧ボックスを採用していなくても
    # システム都合で挿入してしまい、SEO上のノイズになるため。
    requires_list_box = has_list_box or has_list_placeholder
    # 口コミも同様、本文/プレースホルダーで明示されている場合のみ必須化する。
    requires_reviews = has_review_placeholder or has_review_section
    requires_final_cta = bool(expected_shortcode.strip()) and is_comparison
    requires_screenshots = bool(screenshot_targets)

    return {
        "article_type": article_type,
        "is_comparison": is_comparison,
        "has_clinic_sections": has_clinic_sections,
        "has_list_box": has_list_box,
        "has_review_section": has_review_section,
        "has_map_block": has_map_block,
        "has_official_button": has_official_button,
        "requires_list_box": requires_list_box,
        "requires_reviews": requires_reviews,
        "requires_final_cta": requires_final_cta,
        "requires_screenshots": requires_screenshots,
    }


def extract_expected_headings(tag_structure_path: str) -> tuple[list[str], list[str]]:
    with open(tag_structure_path, encoding="utf-8") as f:
        content = f.read()
    h2s = [m.strip() for m in re.findall(r"^### \[H2\] (.+)$", content, re.MULTILINE)]
    h3s = [m.strip() for m in re.findall(r"^#### \[H3\] (.+)$", content, re.MULTILINE)]
    return h2s, h3s


def validate_html_matches_tag_structure(html: str, tag_structure_path: str) -> None:
    expected_h2s, expected_h3s = extract_expected_headings(tag_structure_path)
    soup = BeautifulSoup(html, "html.parser")
    actual_h2s = [node.get_text(" ", strip=True) for node in soup.find_all("h2")]
    actual_h3s = [node.get_text(" ", strip=True) for node in soup.find_all("h3")]

    actual_h2_keys = {canonical_h2_key(heading) for heading in actual_h2s}
    actual_h3_keys = {canonical_heading_key(heading) for heading in actual_h3s}
    missing_h2s = [heading for heading in expected_h2s if canonical_h2_key(heading) not in actual_h2_keys]
    if missing_h2s:
        raise ValueError("タグ構成のH2が本文に不足しています: " + " / ".join(missing_h2s[:3]))

    missing_h3s = [heading for heading in expected_h3s if canonical_heading_key(heading) not in actual_h3_keys]
    if missing_h3s:
        raise ValueError("タグ構成のH3が本文に不足しています: " + " / ".join(missing_h3s[:3]))


def validate_html_output(path: str, keyword_slug: str, tag_structure_path: str | None = None) -> None:
    validate_text_output(path, "記事HTML")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if "<h2" not in html.lower():
        raise ValueError("本文HTMLにH2見出しがありません")
    if re.search(r"</?(?:html|body)\b", html, re.IGNORECASE):
        raise ValueError("本文HTMLに不要なhtml/bodyタグが残っています")
    if ".jpg" in html and f"{keyword_slug}_h2_" in html:
        raise ValueError("本文HTMLに旧H2画像拡張子(.jpg)が残っています")
    if "{{後で追加:口コミ" in html:
        raise ValueError("旧口コミプレースホルダー表記が残っています")
    if "※要確認" in html:
        raise ValueError("本文HTMLに未確定プレースホルダーが残っています")
    # いかなる {{後で作成:...}} 形式も許可しない
    remaining = re.findall(r"\{\{後で作成:[^}]*\}\}", html)
    if remaining:
        preview = " / ".join(remaining[:3])
        raise ValueError(f"未解決のプレースホルダーが残っています: {preview}")
    if tag_structure_path:
        validate_html_matches_tag_structure(html, tag_structure_path)
    issues = validate_article_html(html, keyword_slug)
    if issues:
        raise ValueError(" / ".join(issues))


def _rewrite_soup_fragment(soup: BeautifulSoup) -> str:
    if soup.body is not None:
        html = soup.body.decode_contents().strip()
    else:
        html = str(soup)
    return re.sub(r"\n{3,}", "\n\n", html)


def auto_fix_table_class_issues(path: str) -> dict[str, int]:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    soup = BeautifulSoup(html, "lxml")
    report = normalize_table_classes(soup)
    if any(report.values()):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_rewrite_soup_fragment(soup))
    return report


def validate_html_output_with_resume_autofix(
    path: str,
    keyword_slug: str,
    tag_structure_path: str | None = None,
    *,
    step_name: str = "resume",
) -> None:
    try:
        validate_html_output(path, keyword_slug, tag_structure_path)
        return
    except Exception as e:
        message = str(e)
        if "clinic-summary-table" not in message and "clinic-detail-table" not in message:
            raise

    report = auto_fix_table_class_issues(path)
    if any(report.values()):
        log(
            "  Resume autofix applied before "
            f"{step_name}: summary={report.get('clinic_summary_tables_classed', 0)}, "
            f"detail={report.get('clinic_detail_tables_classed', 0)}, "
            f"compare={report.get('compare_tables_classed', 0)}"
        )
    validate_html_output(path, keyword_slug, tag_structure_path)


def validate_generated_images(html_path: str, keyword_slug: str) -> None:
    html_dir = os.path.dirname(html_path)
    images_dir = os.path.join(html_dir, "images")
    top_image = os.path.join(images_dir, f"{keyword_slug}_top.png")
    if not os.path.exists(top_image):
        raise FileNotFoundError(f"トップ画像が見つかりません: {top_image}")

    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    h2_refs = re.findall(rf'images/({re.escape(keyword_slug)}_h2_\d+\.png)', html, re.IGNORECASE)
    missing = [name for name in h2_refs if not os.path.exists(os.path.join(images_dir, name))]
    if missing:
        raise FileNotFoundError("不足しているH2画像があります: " + ", ".join(missing[:5]))


def validate_reviews_output(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if not html_needs_reviews(html) and 'class="review-section"' not in html:
        return
    if "{{後で作成:口コミ" in html:
        raise ValueError("口コミプレースホルダーが残っています")
    if 'class="review-section"' not in html:
        raise ValueError("口コミセクションが見つかりません")
    if 'class="reviews"' in html or 'blockquote class="review"' in html:
        raise ValueError("旧口コミブロック(.reviews / blockquote.review)が残っています")


def validate_unresolved_facts(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    issues = []
    if "※要確認" in html:
        markers = re.findall(r".{0,20}※要確認.{0,40}", html)
        preview = " / ".join(markers[:3])
        issues.append(f"未確定の事実プレースホルダーが残っています: {preview}")
    if "記事末尾に" in html and "注意書き" in html:
        issues.append("生成指示文が本文に混入しています")
    if issues:
        raise ValueError(" / ".join(issues))


def article_requires_screenshots(path: str) -> bool:
    return bool(build_article_profile(path).get("requires_screenshots"))


def validate_screenshot_insertion(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if "{{後で作成:アフィカセット" in html:
        raise ValueError("旧アフィカセットプレースホルダーが残っています")
    if re.search(r'<a href="#"\s+target="_blank"[^>]*>[^<]+公式サイトを見る</a>', html):
        raise ValueError("公式サイトボタンに未確定URL(#)が残っています")
    if re.search(r'official-site-button-wrap.*?href="※要確認"', html, re.DOTALL):
        raise ValueError("公式サイトボタンのURLが未確定のままです")
    if re.search(r'<!--\s*※要確認:\s*公式サイトURL\s*-->', html):
        raise ValueError("公式サイトURLの要確認プレースホルダーが残っています")

    soup = BeautifulSoup(html, "html.parser")
    target_sections = collect_screenshot_target_sections(soup)

    if not target_sections:
        return

    missing_buttons = []
    for heading, section_nodes in target_sections:
        if not section_has_official_site_button(section_nodes):
            missing_buttons.append(heading.get_text(" ", strip=True))

    if missing_buttons:
        preview = ", ".join(missing_buttons[:5])
        raise ValueError(f"クリニック紹介セクションに公式サイトボタンがありません: {preview}")


def validate_list_box(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if "{{後で作成:一覧ボックス" in html:
        raise ValueError("一覧ボックスのプレースホルダーが残っています")
    if 'class="clinic-index-box"' not in html:
        if 'class="clinic-list-box"' not in html:
            return
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select(".clinic-index-box a[href^='#'], .clinic-list-box a[href^='#']")
    if not anchors:
        raise ValueError("一覧ボックス内にクリニックリンクが見つかりません")

    clinic_h3_tags = iter_candidate_clinic_h3_tags(soup)

    target_ids = []
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if href.startswith("#"):
            target_ids.append(href[1:])

    if not target_ids:
        raise ValueError("一覧ボックス内のリンク先IDが不正です")

    missing_targets = []
    for anchor, target_id in zip(anchors, target_ids):
        if soup.find(id=target_id) is not None:
            continue
        if match_anchor_to_heading(anchor, clinic_h3_tags) is not None:
            continue
        missing_targets.append(target_id)

    if missing_targets:
        raise ValueError("一覧ボックスのリンク先が本文内に存在しません: " + ", ".join(missing_targets[:5]))


def validate_final_cta(path: str, expected_shortcode: str) -> None:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if not expected_shortcode.strip():
        return
    if "<!-- final-cta:start -->" not in html or "<!-- final-cta:end -->" not in html:
        raise ValueError("末尾CTAが挿入されていません")
    tail = html[-4000:]
    expected_name = extract_shortcode_name(expected_shortcode)
    if expected_name:
        pattern = re.compile(r'\[sc name="' + re.escape(expected_name) + r'"\s*\]\[/sc\]')
        if not pattern.search(tail):
            embed_marker = get_variant_embed_marker_from_shortcode_name(expected_name)
            if not embed_marker or embed_marker not in tail:
                raise ValueError("末尾CTA付近に想定ショートコードまたは直埋め早見表が見つかりません")
        return
    if expected_shortcode not in tail:
        raise ValueError("末尾CTA付近に想定ショートコードが見つかりません")


def validate_map_queries(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    if "{{後で作成:マップ" in html:
        raise ValueError("マップのプレースホルダーが残っています")
    bad = re.findall(r'https://(?:www\.)?google\.com/maps\?q=([^"&]+)&amp;output=embed', html)
    generic = [q for q in bad if "%E6%9D%B1%E4%BA%AC%E9%83%BD" not in q and "%20" not in q and "%E6%96%B0%E5%AE%BF" not in q]
    if generic:
        raise ValueError("住所を含まない曖昧なマップクエリが残っています")
    legacy = re.findall(r'https://www\.google\.com/maps\?q=', html)
    if legacy:
        raise ValueError("旧式のGoogle Maps埋め込みURLが残っています")


def validate_pre_publish_html(
    path: str,
    keyword_slug: str,
    expected_shortcode: str,
    tag_structure_path: str | None = None,
    article_profile: dict[str, object] | None = None,
) -> None:
    article_profile = article_profile or build_article_profile(path, tag_structure_path, expected_shortcode)
    validate_html_output(path, keyword_slug, tag_structure_path)
    if not reference_report_allows_missing(path):
        with open(path, encoding="utf-8") as f:
            validate_reference_output(f.read())
    validate_reviews_output(path)
    if article_profile.get("requires_list_box"):
        validate_list_box(path)
    if article_profile.get("requires_final_cta"):
        validate_final_cta(path, expected_shortcode)
    if article_profile.get("has_map_block"):
        validate_map_queries(path)
    # nandemo は他ドメインへ転用するため、ショートコード参照が残ってはいけない
    output_key = infer_output_key_from_path(path)
    if is_nandemo_output_key(output_key):
        validate_no_shortcodes_for_nandemo(path)


def infer_output_key_from_path(path: str) -> str:
    """記事HTMLの絶対パスから出力キー（ディレクトリ名）を推定する。"""
    return os.path.basename(os.path.dirname(os.path.abspath(path)))


def validate_no_shortcodes_for_nandemo(path: str) -> None:
    """なんでも記事は他ドメインに流用される前提なので、`[sc name="..."]` は全てインライン展開済みで
    あるべき。残っている場合はエラー。"""
    from variant_utils import find_unresolved_shortcodes
    with open(path, encoding="utf-8") as f:
        html = f.read()
    leftovers = find_unresolved_shortcodes(html)
    if leftovers:
        unique = sorted(set(leftovers))
        raise ValueError(
            "なんでも記事に未展開のショートコードが残っています（インライン化必須）: "
            + ", ".join(unique)
        )


def validate_references_and_html(path: str, keyword_slug: str, tag_structure_path: str | None = None) -> None:
    validate_html_output(path, keyword_slug, tag_structure_path)
    if reference_report_allows_missing(path):
        return
    with open(path, encoding="utf-8") as f:
        validate_reference_output(f.read())


def reference_report_allows_missing(path: str) -> bool:
    html_dir = os.path.dirname(path)
    basename = os.path.basename(path)
    slug = os.path.splitext(basename)[0].replace("_記事", "")
    report_path = os.path.join(html_dir, f"{slug}_reference_report.json")
    if not os.path.exists(report_path):
        return False
    try:
        with open(report_path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return False
    return payload.get("status") == "skipped"


def explain_validation_error(issue: str) -> str:
    mapping = [
        ("プレースホルダー", "前段の生成物に未解決のテンプレートが残っているためです"),
        ("未確定の事実", "料金・住所・診療時間などを裏取りし切れず、仮置きの情報が残っています"),
        ("生成指示文", "LLMへの指示テキストが本文に混入しています"),
        ("参考文献", "参考文献の形式、リンク設定、または候補選定に不整合があります"),
        ("参照元", "参照元として弱いURLや不適切なリンクが混ざった可能性があります"),
        ("H2見出し", "構成設計か本文生成で見出し設計が崩れた可能性があります"),
        ("H3見出し", "比較対象の院数や見出し抽出が不足した可能性があります"),
        ("重複", "生成時に同じ院セクションを重ねて出力した可能性があります"),
        ("divタグ", "HTMLのラッパー構造が途中で壊れています"),
        ("口コミ", "スクレイピング元から口コミ抽出に失敗したか、差し込みが未完了です"),
        ("一覧ボックス", "本文のクリニック見出しと一覧生成結果が噛み合っていません"),
        ("ショートコード", "末尾CTA生成時の差し込み位置かジャンル設定が不整合です"),
        ("マップ", "地図の埋め込みURLが古いか、院の場所を一意に示せていません"),
        ("スクリーンショット", "公式サイトURL解決かスクリーンショット差し込みに失敗しています"),
        ("画像", "画像生成または本文側の画像参照が崩れています"),
        ("filtered が空", "検索結果のフィルタが厳しすぎるか、取得結果が弱い可能性があります"),
    ]
    for needle, reason in mapping:
        if needle in issue:
            return reason
    return "生成物の品質チェックで想定外の不整合が見つかりました"


def review_output(step_name: str, validator, results_bucket: dict) -> tuple[bool, str | None]:
    try:
        validator()
        results_bucket["review"] = {
            "status": "ok",
            "checked_at": datetime.now().isoformat(),
        }
        log(f"  ✓ {step_name} 品質チェック通過")
        return True, None
    except Exception as e:
        issue = str(e)
        reason = explain_validation_error(issue)
        results_bucket["review"] = {
            "status": "failed",
            "checked_at": datetime.now().isoformat(),
            "issue": issue,
            "reason": reason,
        }
        log(f"  ! {step_name} 品質チェック失敗: {issue}")
        log(f"    原因推定: {reason}")
        return False, issue


def set_step_warning(results: dict, step_key: str, warning: str, *, optional: bool = False) -> None:
    step_payload = dict(results["steps"].get(step_key) or {})
    step_payload["status"] = "warning"
    step_payload["warning"] = warning
    if optional:
        step_payload["optional"] = True
    results["steps"][step_key] = step_payload
    log(f"  ! {step_key} は警告扱いで継続します: {warning}")


def file_exists_with_content(path: str) -> bool:
    return os.path.exists(path) and os.path.getsize(path) > 0


def estimate_generate_html_timeout(tag_path: str) -> int:
    default_timeout = 900
    if not os.path.exists(tag_path):
        return default_timeout

    try:
        with open(tag_path, encoding="utf-8") as f:
            tag_structure = f.read()
    except OSError:
        return default_timeout

    h2_count = len(re.findall(r"^### \[H2\] ", tag_structure, re.MULTILINE))
    h3_count = len(re.findall(r"^#### \[H3\] ", tag_structure, re.MULTILINE))
    section_h3_counts = []
    current_h3_count = 0
    for line in tag_structure.splitlines():
        if line.startswith("### [H2] "):
            if current_h3_count:
                section_h3_counts.append(current_h3_count)
            current_h3_count = 0
        elif line.startswith("#### [H3] "):
            current_h3_count += 1
    if current_h3_count:
        section_h3_counts.append(current_h3_count)

    max_h3_per_h2 = max(section_h3_counts, default=0)
    timeout = default_timeout
    timeout += max(0, h3_count - 20) * 45
    timeout += max(0, h2_count - 8) * 60
    timeout += max(0, max_h3_per_h2 - 6) * 90
    timeout += max(0, len(tag_structure) - 6000) // 12

    return max(default_timeout, min(timeout, 3600))


def run_step_with_review(
    step_key: str,
    display_name: str,
    cmd: list[str],
    validator,
    results: dict,
    timeout: int | None = 300,
):
    max_reviews = 1 + len(QUALITY_RETRY_DELAYS)
    previous_issue: str | None = None

    for review_attempt in range(1, max_reviews + 1):
        step = run_step(display_name, cmd, timeout=timeout)
        step["quality_attempt"] = review_attempt
        step["quality_max_attempts"] = max_reviews
        results["steps"][step_key] = step

        if step["status"] != "ok":
            return step, False, None

        ok, issue = review_output(display_name, validator, step)
        if ok:
            return step, True, None

        # 同じ品質エラーが連続したら早期終了。プロンプトもデータも同じなので Claude API を
        # 何度叩いても結果は変わらない。クレジットの無駄遣いを避けるため停止する。
        normalized_issue = (issue or "").strip()
        if previous_issue is not None and normalized_issue == previous_issue:
            log(f"  {display_name} の品質チェックが2回連続で同一エラー。リトライを早期終了します（クレジット節約）")
            return step, False, issue
        previous_issue = normalized_issue

        if review_attempt < max_reviews:
            delay = QUALITY_RETRY_DELAYS[review_attempt - 1]
            log(f"  {display_name} を品質改善のため {delay}秒後に再実行します")
            time.sleep(delay)
            continue

        return step, False, issue

    return results["steps"][step_key], False, "quality_review_exhausted"


def run_step(name, cmd, timeout: int | None = 300):
    """パイプラインの各ステップを実行。失敗時は 5秒後、60秒後に再試行する。"""
    max_attempts = 1 + len(STEP_RETRY_DELAYS)
    log(f"▶ {name} 開始")

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            log(f"  ↻ {name} 再試行 {attempt}/{max_attempts}")

        start = time.time()

        try:
            result = subprocess.run(
                cmd,
                cwd=SCRIPT_DIR,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = round(time.time() - start, 1)

            if result.returncode == 0:
                log(f"  ✓ {name} 完了 ({elapsed}秒)")
                return {
                    "status": "ok",
                    "elapsed": elapsed,
                    "attempt_count": attempt,
                    "max_attempts": max_attempts,
                    "output": result.stdout,
                }

            error_text = result.stderr[:500] or result.stdout[:500]
            log(f"  ✗ {name} 失敗 (exit={result.returncode})")
            if error_text:
                log(f"    stderr: {error_text[:300]}")
            step_result = {
                "status": "error",
                "elapsed": elapsed,
                "attempt_count": attempt,
                "max_attempts": max_attempts,
                "error": error_text,
            }
            if is_anthropic_credit_error(error_text):
                step_result["non_retryable"] = True

        except subprocess.TimeoutExpired:
            timeout_label = f"{timeout}秒" if timeout is not None else "上限なし"
            log(f"  ✗ {name} タイムアウト ({timeout_label})")
            step_result = {
                "status": "timeout",
                "elapsed": timeout or 0,
                "attempt_count": attempt,
                "max_attempts": max_attempts,
            }
        except Exception as e:
            log(f"  ✗ {name} 例外: {e}")
            step_result = {
                "status": "exception",
                "attempt_count": attempt,
                "max_attempts": max_attempts,
                "error": str(e),
            }

        if step_result.get("non_retryable"):
            log(f"  {name} は再試行しないエラーのため停止します")
            return step_result

        if attempt < max_attempts:
            delay = STEP_RETRY_DELAYS[attempt - 1]
            log(f"  {name} を {delay}秒後に再試行します")
            time.sleep(delay)
            continue

        return step_result


def run_pipeline(
    keyword,
    site_config,
    genre_id,
    category="",
    title="",
    start_step=None,
    output_key=None,
    variant_index=1,
    variant_count=1,
    reference_url="",
):
    """パイプライン全体を実行"""

    resolved_output_key = resolve_output_key(keyword, output_key)
    keyword_slug = keyword_to_slug(resolved_output_key)
    keyword_output_dir = ensure_output_dir_for_key(resolved_output_key)
    keyword_scraped_dir = get_keyword_scraped_dir(keyword)
    site_config_data = {}
    try:
        with open(site_config, encoding="utf-8") as f:
            site_config_data = json.load(f)
    except Exception:
        site_config_data = {}
    skip_wordpress = bool(site_config_data.get("skip_wordpress"))
    wordpress_timeout_seconds = int(site_config_data.get("wordpress_timeout_seconds", 600) or 600)
    variant_profile = build_variant_profile(variant_index, variant_count)
    results = {
        "keyword": keyword,
        "output_key": resolved_output_key,
        "site_config": site_config,
        "variant_profile": variant_profile,
        "started_at": datetime.now().isoformat(),
        "steps": {},
    }
    start_index = STEP_SEQUENCE.index(start_step) if start_step in STEP_SEQUENCE else 0
    if start_step in STEP_SEQUENCE:
        results["resumed_from"] = start_step

    log(f"パイプライン開始: {keyword}")
    if resolved_output_key != keyword:
        log(f"出力キー: {resolved_output_key}")
    if variant_count > 1:
        log(f"編集版: {variant_index}/{variant_count}")
    log(f"サイト設定: {site_config}")
    if skip_wordpress:
        log("WordPress投稿: site config によりスキップ")
    else:
        log(f"WordPress投稿タイムアウト: {wordpress_timeout_seconds}秒")
    if start_step in STEP_SEQUENCE:
        log(f"再開ステップ: {start_step}")
    pipeline_start = time.time()
    last_completed_step = None

    def persist_runtime_state(
        status: str,
        current_step: str | None = None,
        suggested_resume_step: str | None = None,
        final_status: str | None = None,
        validation_error: str = "",
    ):
        save_runtime_state(
            keyword,
            {
                "keyword": keyword,
                "site_config": site_config,
                "genre_id": genre_id,
                "category": category,
                "title": title,
                "started_at": results["started_at"],
                "resumed_from": start_step if start_step in STEP_SEQUENCE else None,
                "status": status,
                "current_step": current_step,
                "last_completed_step": last_completed_step,
                "suggested_resume_step": suggested_resume_step,
                "final_status": final_status,
                "validation_error": validation_error,
            },
        )

    def finalize(status: str, **extra):
        resume_step = infer_resume_step(status, extra.get("validation_error", ""))
        checkpoint_status = "success" if status == "success" else "failed"
        persist_runtime_state(
            checkpoint_status,
            current_step=None,
            suggested_resume_step=resume_step,
            final_status=status,
            validation_error=extra.get("validation_error", ""),
        )
        total_elapsed = round(time.time() - pipeline_start, 1)
        results["total_elapsed"] = total_elapsed
        results["finished_at"] = datetime.now().isoformat()
        results["final_status"] = status
        results.update(extra)
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, f"{keyword_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        results["log_path"] = log_path
        log(f"ログ保存: {log_path}")
        return results

    def mark_step_start(step_key: str):
        persist_runtime_state(
            "running",
            current_step=step_key,
            suggested_resume_step=step_key,
        )

    def mark_step_done(step_key: str):
        nonlocal last_completed_step
        last_completed_step = step_key
        next_index = STEP_SEQUENCE.index(step_key) + 1
        next_step = STEP_SEQUENCE[next_index] if next_index < len(STEP_SEQUENCE) else None
        persist_runtime_state(
            "running",
            current_step=None,
            suggested_resume_step=next_step,
        )

    persist_runtime_state(
        "running",
        current_step=start_step if start_step in STEP_SEQUENCE else "search",
        suggested_resume_step=start_step if start_step in STEP_SEQUENCE else "search",
    )

    # ==========================================
    # Step 0: Google検索 → 上位記事URL取得
    # ==========================================
    search_json = os.path.join(keyword_output_dir, f"{keyword_slug}_search_results.json")
    if start_index <= STEP_SEQUENCE.index("search"):
        mark_step_start("search")
        # reference_url 指定があれば、その URL を search_keyword.py に渡し、
        # 結果リストの先頭に強制配置 → scrape の articles[0] になり、`top_article_profile` の対象になる
        search_cmd = [PYTHON, "search_keyword.py", keyword, "--count", "5", "--output-key", resolved_output_key]
        if reference_url:
            search_cmd.extend(["--reference-url", reference_url])
        step, ok, issue = run_step_with_review(
            "search",
            "Step 0: Google検索",
            search_cmd,
            lambda: validate_search_results(search_json),
            results,
        )
        if step["status"] != "ok":
            return finalize("failed_at_search")
        if not ok:
            return finalize("search_output_invalid", validation_error=issue)
        mark_step_done("search")
    else:
        results["steps"]["search"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_search_results(search_json)
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"search: {e}")
        mark_step_done("search")

    with open(search_json) as f:
        search_data = json.load(f)
    urls = [item["url"] for item in search_data.get("filtered", [])]
    log(f"  対象URL: {len(urls)}件")

    if not urls:
        return finalize("no_urls_found")

    # ==========================================
    # Step 1: 競合記事スクレイピング
    # ==========================================
    if start_index <= STEP_SEQUENCE.index("scrape"):
        mark_step_start("scrape")
        step, ok, issue = run_step_with_review(
            "scrape",
            "Step 1: 競合記事スクレイピング",
            [PYTHON, "scrape.py", keyword] + urls,
            lambda: validate_scraped_outputs(keyword_scraped_dir),
            results,
            timeout=120,
        )
        if step["status"] != "ok":
            return finalize("failed_at_scrape")
        if not ok:
            return finalize("scrape_output_invalid", validation_error=issue)
        mark_step_done("scrape")
    else:
        results["steps"]["scrape"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_scraped_outputs(keyword_scraped_dir)
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"scrape: {e}")
        mark_step_done("scrape")

    # ==========================================
    # Step 2: タグ構成設計（Claude API）
    # ==========================================
    tag_path = os.path.join(keyword_output_dir, f"{keyword_slug}_タグ構成.md")
    if start_index <= STEP_SEQUENCE.index("tag_structure"):
        mark_step_start("tag_structure")
        step, ok, issue = run_step_with_review(
            "tag_structure",
            "Step 2: タグ構成設計",
            [
                PYTHON, "generate_article.py", "--keyword", keyword, "--genre", genre_id,
                "--step", "2", "--scraped-dir", keyword_scraped_dir,
                "--output-key", resolved_output_key,
                "--variant-index", str(variant_index),
                "--variant-count", str(variant_count),
            ],
            lambda: validate_tag_structure_output(tag_path),
            results,
            timeout=300,
        )
        if step["status"] != "ok":
            if is_anthropic_credit_error(step.get("error", "")):
                return finalize(
                    "tag_structure_credit_exhausted",
                    validation_error="Anthropic API のクレジット不足でタグ構成生成に失敗しました",
                )
            if file_exists_with_content(tag_path):
                set_step_warning(
                    results,
                    "tag_structure",
                    "タグ構成生成は失敗しましたが、既存のタグ構成ファイルで継続します",
                )
            else:
                return finalize("failed_at_tag_structure")
        if not ok:
            set_step_warning(
                results,
                "tag_structure",
                issue or "タグ構成の品質チェックに失敗しました",
            )
        mark_step_done("tag_structure")
    else:
        results["steps"]["tag_structure"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_tag_structure_output(tag_path)
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"tag_structure: {e}")
        mark_step_done("tag_structure")

    # ==========================================
    # Step 3: 本文HTML生成（Claude API）
    # ==========================================
    html_path = os.path.join(keyword_output_dir, f"{keyword_slug}_記事.html")
    if start_index <= STEP_SEQUENCE.index("generate_html"):
        mark_step_start("generate_html")
        generate_html_timeout = estimate_generate_html_timeout(tag_path)
        log(f"  generate_html タイムアウト設定: {generate_html_timeout}秒")
        step, ok, issue = run_step_with_review(
            "generate_html",
            "Step 3: 本文HTML生成",
            [
                PYTHON, "generate_article.py", "--keyword", keyword, "--genre", genre_id,
                "--step", "3", "--scraped-dir", keyword_scraped_dir,
                "--output-key", resolved_output_key,
                "--variant-index", str(variant_index),
                "--variant-count", str(variant_count),
            ],
            lambda: validate_html_output(html_path, keyword_slug, tag_path),
            results,
            timeout=generate_html_timeout,
        )
        if step["status"] != "ok":
            if is_anthropic_credit_error(step.get("error", "")):
                return finalize(
                    "generate_html_credit_exhausted",
                    validation_error="Anthropic API のクレジット不足で本文生成に失敗しました",
                )
            if file_exists_with_content(html_path):
                set_step_warning(
                    results,
                    "generate_html",
                    "本文生成は失敗しましたが、既存のHTMLで継続します",
                )
            else:
                return finalize("failed_at_generate_html")
        log(f"  記事HTML: {html_path}")
        if not ok:
            set_step_warning(
                results,
                "generate_html",
                issue or "本文HTMLの品質チェックに失敗しました",
            )
        mark_step_done("generate_html")
    else:
        results["steps"]["generate_html"] = {"status": "skipped", "reason": "resume"}
        log(f"  記事HTML: {html_path}")
        try:
            validate_html_output(html_path, keyword_slug, tag_path)
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"generate_html: {e}")
        mark_step_done("generate_html")

    genre_path = os.path.join(SCRIPT_DIR, "genres", f"{genre_id}.json")
    genre_config = json.load(open(genre_path, encoding="utf-8"))
    expected_shortcode = resolve_variant_shortcode(
        genre_config.get("shortcodes", {}).get("早見表", ""),
        genre_id=genre_id,
        output_key=resolved_output_key,
        variant_index=variant_index,
    )

    def refresh_article_profile() -> dict[str, object]:
        return build_article_profile(html_path, tag_path, expected_shortcode)

    article_profile = refresh_article_profile()
    if article_profile.get("article_type"):
        log(f"  記事タイプ: {article_profile['article_type']}")

    # ==========================================
    # Step 3.5: 一覧ボックス差し込み
    # ==========================================
    article_profile = refresh_article_profile()
    if not article_profile.get("requires_list_box"):
        results["steps"]["fill_list_box"] = {"status": "skipped", "reason": "not_required"}
        mark_step_done("fill_list_box")
    elif start_index <= STEP_SEQUENCE.index("fill_list_box"):
        mark_step_start("fill_list_box")
        step, ok, issue = run_step_with_review(
            "fill_list_box",
            "Step 3.5: 一覧ボックス差し込み",
            [PYTHON, "fill_list_box.py", "--html", html_path],
            lambda: (
                validate_list_box(html_path),
                validate_html_output_with_resume_autofix(
                    html_path,
                    keyword_slug,
                    tag_path,
                    step_name="fill_list_box",
                ),
            ),
            results,
            timeout=120,
        )
        if step["status"] != "ok":
            return finalize("failed_at_fill_list_box")
        if not ok:
            return finalize("list_box_output_invalid", validation_error=issue)
        mark_step_done("fill_list_box")
    else:
        results["steps"]["fill_list_box"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_list_box(html_path)
            validate_html_output(html_path, keyword_slug, tag_path)
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"fill_list_box: {e}")
        mark_step_done("fill_list_box")

    # ==========================================
    # Step 4: 公式サイトベースのファクトチェック
    # ==========================================
    if start_index <= STEP_SEQUENCE.index("fact_check"):
        mark_step_start("fact_check")
        step = run_step(
            "Step 4: ファクトチェック",
            [PYTHON, "fact_check_article.py", "--html", html_path],
            timeout=900,
        )
        results["steps"]["fact_check"] = step
        if step["status"] not in ("ok", "skipped"):
            return finalize("failed_at_fact_check")
        ok, issue = review_output("Step 4: ファクトチェック", lambda: validate_html_output(html_path, keyword_slug, tag_path), step)
        if not ok:
            return finalize("fact_check_output_invalid", validation_error=issue)
        mark_step_done("fact_check")
    else:
        results["steps"]["fact_check"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_html_output_with_resume_autofix(
                html_path,
                keyword_slug,
                tag_path,
                step_name="fact_check",
            )
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"fact_check: {e}")
        mark_step_done("fact_check")

    # ==========================================
    # Step 4.3: 未確定情報の整形
    # ==========================================
    if start_index <= STEP_SEQUENCE.index("sanitize_article"):
        mark_step_start("sanitize_article")
        step = run_step(
            "Step 4.3: 未確定情報の整形",
            [PYTHON, "sanitize_article.py", "--html", html_path],
            timeout=120,
        )
        results["steps"]["sanitize_article"] = step
        if step["status"] != "ok":
            return finalize("failed_at_sanitize_article")
        ok, issue = review_output(
            "Step 4.3: 未確定情報の整形",
            lambda: validate_html_output(html_path, keyword_slug, tag_path),
            step,
        )
        if not ok:
            return finalize("sanitize_article_output_invalid", validation_error=issue)
        mark_step_done("sanitize_article")
    else:
        results["steps"]["sanitize_article"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_html_output_with_resume_autofix(
                html_path,
                keyword_slug,
                tag_path,
                step_name="sanitize_article",
            )
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"sanitize_article: {e}")
        mark_step_done("sanitize_article")

    # ==========================================
    # Step 4.4: 参考文献・公的情報の付与
    # ==========================================
    if start_index <= STEP_SEQUENCE.index("references"):
        mark_step_start("references")
        step, ok, issue = run_step_with_review(
            "references",
            "Step 4.4: 参考文献・公的情報の付与",
            [PYTHON, "add_references.py", "--html", html_path],
            lambda: validate_references_and_html(html_path, keyword_slug, tag_path),
            results,
            timeout=900,
        )
        if step["status"] != "ok":
            return finalize("failed_at_references")
        if not ok:
            set_step_warning(
                results,
                "references",
                issue or "参考文献・公的情報の品質チェックに失敗しました",
            )
        mark_step_done("references")
    else:
        results["steps"]["references"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_html_output_with_resume_autofix(
                html_path,
                keyword_slug,
                tag_path,
                step_name="references",
            )
            if not reference_report_allows_missing(html_path):
                with open(html_path, encoding="utf-8") as f:
                    validate_reference_output(f.read())
        except Exception as e:
            log(f"  references の前提チェックに失敗したため、Step 4.4 を再実行します: {e}")
            mark_step_start("references")
            step, ok, issue = run_step_with_review(
                "references",
                "Step 4.4: 参考文献・公的情報の付与",
                [PYTHON, "add_references.py", "--html", html_path],
                lambda: validate_references_and_html(html_path, keyword_slug, tag_path),
                results,
                timeout=900,
            )
            if step["status"] != "ok":
                return finalize("failed_at_references")
            if not ok:
                set_step_warning(
                    results,
                    "references",
                    issue or "参考文献・公的情報の品質チェックに失敗しました",
                )
        mark_step_done("references")

    # ==========================================
    # Step 4.5: 口コミ差し込み
    # ==========================================
    article_profile = refresh_article_profile()
    if not article_profile.get("requires_reviews"):
        results["steps"]["fill_reviews"] = {"status": "skipped", "reason": "not_required"}
        mark_step_done("fill_reviews")
    elif start_index <= STEP_SEQUENCE.index("fill_reviews"):
        mark_step_start("fill_reviews")
        step, ok, issue = run_step_with_review(
            "fill_reviews",
            "Step 4.5: 口コミ差し込み",
            [PYTHON, "fill_reviews.py", "--html", html_path, "--scraped-dir", keyword_scraped_dir],
            lambda: (validate_reviews_output(html_path), validate_html_output(html_path, keyword_slug, tag_path)),
            results,
            timeout=120,
        )
        if step["status"] != "ok":
            return finalize("failed_at_fill_reviews")
        if not ok:
            return finalize("reviews_output_invalid", validation_error=issue)
        mark_step_done("fill_reviews")
    else:
        results["steps"]["fill_reviews"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_reviews_output(html_path)
            validate_html_output_with_resume_autofix(
                html_path,
                keyword_slug,
                tag_path,
                step_name="fill_reviews",
            )
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"fill_reviews: {e}")
        mark_step_done("fill_reviews")

    # ==========================================
    # Step 4.6: 末尾CTA差し込み
    # ==========================================
    article_profile = refresh_article_profile()
    if not article_profile.get("requires_final_cta"):
        results["steps"]["fill_final_cta"] = {"status": "skipped", "reason": "not_required"}
        mark_step_done("fill_final_cta")
    elif start_index <= STEP_SEQUENCE.index("fill_final_cta"):
        mark_step_start("fill_final_cta")
        step, ok, issue = run_step_with_review(
            "fill_final_cta",
            "Step 4.6: 末尾CTA差し込み",
            [
                PYTHON, "fill_final_cta.py",
                "--html", html_path,
                "--keyword", keyword,
                "--genre-json", genre_path,
                "--variant-index", str(variant_index),
            ],
            lambda: validate_final_cta(html_path, expected_shortcode),
            results,
            timeout=120,
        )
        if step["status"] != "ok":
            return finalize("failed_at_fill_final_cta")
        if not ok:
            return finalize("final_cta_output_invalid", validation_error=issue)
        mark_step_done("fill_final_cta")
    else:
        results["steps"]["fill_final_cta"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_final_cta(html_path, expected_shortcode)
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"fill_final_cta: {e}")
        mark_step_done("fill_final_cta")

    # ==========================================
    # Step 4.7: Googleマップ補正
    # ==========================================
    article_profile = refresh_article_profile()
    if not article_profile.get("has_map_block"):
        results["steps"]["fill_maps"] = {"status": "skipped", "reason": "not_required"}
        mark_step_done("fill_maps")
    elif start_index <= STEP_SEQUENCE.index("fill_maps"):
        mark_step_start("fill_maps")
        step, ok, issue = run_step_with_review(
            "fill_maps",
            "Step 4.7: Googleマップ補正",
            [PYTHON, "fill_maps.py", "--html", html_path],
            lambda: validate_map_queries(html_path),
            results,
            timeout=120,
        )
        if step["status"] != "ok":
            return finalize("failed_at_fill_maps")
        if not ok:
            return finalize("map_output_invalid", validation_error=issue)
        mark_step_done("fill_maps")
    else:
        results["steps"]["fill_maps"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_map_queries(html_path)
        except Exception as e:
            return finalize("resume_prerequisite_invalid", validation_error=f"fill_maps: {e}")
        mark_step_done("fill_maps")

    # ==========================================
    # Step 5: 公式サイトスクリーンショット
    # ==========================================
    article_profile = refresh_article_profile()
    screenshots_required = bool(article_profile.get("requires_screenshots"))
    if not screenshots_required:
        results["steps"]["screenshots"] = {"status": "skipped", "reason": "not_required"}
        mark_step_done("screenshots")
    elif start_index <= STEP_SEQUENCE.index("screenshots"):
        mark_step_start("screenshots")
        step, ok, issue = run_step_with_review(
            "screenshots",
            "Step 5: スクリーンショット",
            [PYTHON, "capture_screenshots.py", "--html", html_path],
            lambda: validate_screenshot_insertion(html_path),
            results,
            timeout=300,
        )
        if step["status"] != "ok":
            if screenshots_required:
                return finalize("failed_at_screenshots")
            step["status"] = "warning"
            step["optional"] = True
            step["warning"] = "スクリーンショットは任意のため、失敗しても継続します"
            log("  ! Step 5 は失敗しましたが任意ステップのため継続します")
        elif not ok:
            if screenshots_required:
                return finalize("screenshot_output_invalid", validation_error=issue)
            step["status"] = "warning"
            step["optional"] = True
            step["warning"] = issue or "スクリーンショット品質チェック失敗"
            log("  ! Step 5 は品質チェック失敗でしたが任意ステップのため継続します")
        mark_step_done("screenshots")
    else:
        results["steps"]["screenshots"] = {"status": "skipped", "reason": "resume"}
        try:
            validate_screenshot_insertion(html_path)
        except Exception as e:
            if screenshots_required:
                return finalize("resume_prerequisite_invalid", validation_error=f"screenshots: {e}")
            results["steps"]["screenshots"] = {
                "status": "warning",
                "reason": "resume",
                "optional": True,
                "warning": str(e),
            }
        mark_step_done("screenshots")

    # ==========================================
    # Step 5.5: クリニックロゴ取得（clinic-list-tbl 用）
    # ==========================================
    # capture_logos.py で公式サイトからロゴ画像を抽出し、clinic-list-tbl の
    # cl-logo-text を <img> に差し替える。output/.logo_cache.json で全記事間共有。
    # ロゴはクリニック比較表の見栄え用なので任意扱い（失敗しても継続）。
    if start_index <= STEP_SEQUENCE.index("logos"):
        mark_step_start("logos")
        logo_cmd = [PYTHON, "capture_logos.py", "--html", html_path]
        urls_path = html_path.replace("_記事.html", "_urls.json")
        if os.path.exists(urls_path):
            logo_cmd.extend(["--urls", urls_path])
        step, ok, issue = run_step_with_review(
            "logos",
            "Step 5.5: クリニックロゴ取得",
            logo_cmd,
            lambda: True,  # ロゴは任意なのでバリデーションなし
            results,
            timeout=300,
        )
        if step["status"] != "ok":
            step["status"] = "warning"
            step["optional"] = True
            step["warning"] = "ロゴ取得は任意のため、失敗しても継続します"
            log("  ! Step 5.5 は失敗しましたが任意ステップのため継続します")
        mark_step_done("logos")
    else:
        results["steps"]["logos"] = {"status": "skipped", "reason": "resume"}
        mark_step_done("logos")

    # ==========================================
    # Step 6: AI画像生成 → 廃止（Geminiでの自動画像生成は時間コストの割に品質が出ないため停止）
    # ==========================================
    # 旧コード: generate_images.py を呼び出してトップ画像とH2見出し画像を生成していた
    # クリニック紹介セクションのスクリーンショット（capture_screenshots.py）は別ステップで継続

    try:
        validate_pre_publish_html(
            html_path,
            keyword_slug,
            expected_shortcode,
            tag_path,
            article_profile=refresh_article_profile(),
        )
    except Exception as e:
        results["steps"]["pre_publish_validation"] = {
            "status": "warning",
            "warning": str(e),
            "checked_at": datetime.now().isoformat(),
        }
        log(f"  ! 公開前チェックは警告扱いで継続します: {e}")

    # ==========================================
    # Step 7: WordPress下書き投稿
    # ==========================================
    if skip_wordpress:
        results["steps"]["wordpress"] = {
            "status": "skipped",
            "reason": "site_config_skip_wordpress",
        }
        mark_step_done("wordpress")
    else:
        wp_cmd = [
            PYTHON, "wp_post.py",
            "--html", html_path,
            "--site", site_config,
            "--status", "draft",
        ]
        if title:
            wp_cmd.extend(["--title", title])
        if category:
            wp_cmd.extend(["--category", category])

        if start_index <= STEP_SEQUENCE.index("wordpress"):
            mark_step_start("wordpress")
            step = run_step(
                "Step 7: WordPress下書き投稿",
                wp_cmd,
                timeout=wordpress_timeout_seconds,
            )
            results["steps"]["wordpress"] = step
            if step["status"] == "ok":
                mark_step_done("wordpress")
            else:
                set_step_warning(
                    results,
                    "wordpress",
                    step.get("error") or "WordPress下書き投稿に失敗しました",
                    optional=True,
                )
                mark_step_done("wordpress")
        else:
            results["steps"]["wordpress"] = {"status": "skipped", "reason": "resume"}
            mark_step_done("wordpress")

    # ==========================================
    # 完了
    # ==========================================
    failed_steps = [k for k, v in results["steps"].items() if v.get("status") not in ("ok", "skipped", "warning")]
    if failed_steps:
        final_status = f"completed_with_errors: {', '.join(failed_steps)}"
    else:
        final_status = "success"

    log(f"パイプライン完了: {final_status} ({round(time.time() - pipeline_start, 1)}秒)")
    return finalize(final_status)


def run_nandemo_variant_batch(keyword, site_config, genre_id, category="", title="", variant_count=1, reference_url=""):
    variant_count = normalize_variant_count(variant_count)
    batch_started_at = datetime.now().isoformat()
    variant_results = []
    first_output_key = build_variant_output_key(keyword, 1)

    for index in range(1, variant_count + 1):
        output_key = build_variant_output_key(keyword, index)
        already_completed, completed_result = variant_is_already_completed(keyword, index)
        if already_completed:
            variant_results.append({
                "variant_index": index,
                "output_key": output_key,
                "result": completed_result or {"final_status": "success"},
                "skipped_existing": True,
            })
            continue

        start_step = None
        if index > 1:
            copied, _ = try_copy_search_results_between_output_keys(first_output_key, output_key)
            if copied or can_reuse_scraped_outputs(keyword):
                start_step = "tag_structure"

        # reference_url は v1 で検索を走らせる時のみ使う。v2-v5 は v1 の検索結果を流用するため不要。
        variant_reference_url = reference_url if index == 1 else ""

        result = run_pipeline(
            keyword=keyword,
            site_config=site_config,
            genre_id=genre_id,
            category=category,
            title=title,
            start_step=start_step,
            output_key=output_key,
            variant_index=index,
            variant_count=variant_count,
            reference_url=variant_reference_url,
        )
        variant_results.append({
            "variant_index": index,
            "output_key": output_key,
            "result": result,
        })
        if index == 1 and not is_success_like_final_status(result.get("final_status")):
            break

    failed = [item for item in variant_results if not is_success_like_final_status(item["result"].get("final_status"))]
    final_status = "success" if not failed else ("completed_with_errors" if len(failed) < len(variant_results) else "failed_at_variant_batch")
    return {
        "keyword": keyword,
        "site_config": site_config,
        "variant_count": variant_count,
        "started_at": batch_started_at,
        "finished_at": datetime.now().isoformat(),
        "final_status": final_status,
        "variants": variant_results,
    }


def main():
    parser = argparse.ArgumentParser(description="SEO記事自動生成パイプライン")
    parser.add_argument("--keyword", required=True, help="検索キーワード")
    parser.add_argument("--genre", required=True, help="ジャンルID（例: aga, ed, hair_removal, phimosis, diet）")
    parser.add_argument("--site", required=True, help="サイト設定JSONのパス")
    parser.add_argument("--category", default="", help="WordPressカテゴリ")
    parser.add_argument("--title", default="", help="記事タイトル（省略時は自動）")
    parser.add_argument("--variant-count", type=int, default=1, help="なんでも向けの生成数（1〜5）")
    parser.add_argument(
        "--reference-url",
        default="",
        help="構造参照する競合記事URL（指定するとそのURLが Step 0 検索結果の先頭に強制配置され、Step 2 のタグ構成設計の主軸になる）",
    )
    args = parser.parse_args()

    if is_nandemo_site(args.site) and normalize_variant_count(args.variant_count) > 1:
        results = run_nandemo_variant_batch(
            keyword=args.keyword,
            site_config=args.site,
            genre_id=args.genre,
            category=args.category,
            title=args.title,
            variant_count=args.variant_count,
            reference_url=args.reference_url,
        )
    else:
        output_key = build_variant_output_key(args.keyword, 1) if is_nandemo_site(args.site) else None
        results = run_pipeline(
            keyword=args.keyword,
            site_config=args.site,
            genre_id=args.genre,
            category=args.category,
            title=args.title,
            output_key=output_key,
            variant_index=1,
            variant_count=normalize_variant_count(args.variant_count) if is_nandemo_site(args.site) else 1,
            reference_url=args.reference_url,
        )

    # 結果サマリー
    print("\n" + "=" * 60)
    print("パイプライン結果サマリー")
    print("=" * 60)
    if results.get("variants"):
        for item in results["variants"]:
            result = item["result"]
            print(f"  variant {item['variant_index']} ({item['output_key']}): {result.get('final_status', '?')}")
    else:
        for step_name, step_result in results.get("steps", {}).items():
            status = step_result.get("status", "?")
            elapsed = step_result.get("elapsed", "")
            elapsed_str = f" ({elapsed}秒)" if elapsed else ""
            print(f"  {step_name}: {status}{elapsed_str}")
    print(f"\n  最終結果: {results.get('final_status', '?')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
