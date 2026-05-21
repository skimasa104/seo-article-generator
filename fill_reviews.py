#!/usr/local/bin/python3.12
"""
スクレイピングデータから口コミを抽出し、記事HTMLに口コミセクションを補完する。
旧プレースホルダー表記が残っている記事にも後方互換で対応する。
"""

import argparse
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from fill_list_box import iter_candidate_clinic_h3_tags

REVIEW_PLACEHOLDER_PATTERN = re.compile(r"\{\{後で作成:口コミ\s*[—\-]\s*(.+?)\s*\}\}")


def normalize_name(text: str) -> str:
    text = re.split(r"\s*[|｜]\s*", text or "", maxsplit=1)[0]
    text = re.sub(r"[（(].*?[）)]", "", text).strip()
    text = re.sub(r"\s+", "", text)
    return text.strip()


def find_best_review_key(clinic_name: str, review_map: dict[str, list[str]]) -> str:
    normalized = normalize_name(clinic_name)
    if not normalized:
        return ""
    if normalized in review_map:
        return normalized

    candidates = []
    for key in review_map:
        if not key:
            continue
        if key in normalized or normalized in key:
            candidates.append(key)
    if not candidates:
        return ""
    return max(candidates, key=len)


def extract_reviews_from_structure(structure_text: str) -> dict[str, list[str]]:
    reviews: dict[str, list[str]] = {}
    current_name = None

    for raw_line in structure_text.splitlines():
        line = raw_line.strip()
        heading_match = re.match(r"^>\s*(.+?)の口コミ$", line.replace("  ", ""))
        if heading_match:
            current_name = normalize_name(heading_match.group(1))
            reviews.setdefault(current_name, [])
            continue

        if current_name is None:
            continue

        quote_match = re.match(r"^>\s*(.+?)（.+?の口コミ）$", line.replace("  ", ""))
        if quote_match:
            review_text = quote_match.group(1).strip()
            review_text = re.sub(r"\s+", " ", review_text)
            if review_text and review_text not in reviews[current_name]:
                reviews[current_name].append(review_text)
                if len(reviews[current_name]) >= 2:
                    current_name = None
            continue

        if line.startswith("> スクロールできます→"):
            inline_reviews = line.replace("> スクロールできます→", "", 1).strip()
            segments = re.findall(r"([^\s。]{1,20}?院)(?!内)(.*?)(?=[^\s。]{1,20}?院(?!内)|$)", inline_reviews)
            for _location, body in segments:
                review_text = re.sub(r"\s+", " ", body).strip(" 。")
                if not review_text:
                    continue
                if review_text not in reviews[current_name]:
                    reviews[current_name].append(review_text)
                if len(reviews[current_name]) >= 2:
                    current_name = None
                    break
            continue

        if line.startswith("### [H3]") or line.startswith("### [H2]"):
            current_name = None

    return reviews


def extract_reviews_from_scraped_dir(scraped_dir: str) -> dict[str, list[str]]:
    review_map: dict[str, list[str]] = {}
    for path in sorted(Path(scraped_dir).glob("article_*_structure.md")):
        structure_text = path.read_text(encoding="utf-8")
        extracted = extract_reviews_from_structure(structure_text)
        for name, review_texts in extracted.items():
            bucket = review_map.setdefault(name, [])
            for review_text in review_texts:
                if review_text not in bucket:
                    bucket.append(review_text)
    return review_map


def build_review_html(clinic_name: str, review_texts: list[str]) -> str:
    blocks = []
    for idx, text in enumerate(review_texts[:2], start=1):
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        blocks.append(
            '<div class="review-bubble">'
            '<div class="review-avatar"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#999" width="22" height="22"><path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/></svg></div>'
            '<div class="review-body"><div class="review-meta"><span class="review-stars">★★★★★</span>'
            f"<span>Google口コミ {idx}</span></div>{safe_text}</div></div>"
        )
    if not blocks:
        return ""

    return '<div class="review-section"><h4>口コミ・評判</h4>' + "".join(blocks) + "</div>"


def html_needs_reviews(html: str) -> bool:
    return bool(REVIEW_PLACEHOLDER_PATTERN.search(html))


def iter_clinic_sections(soup: BeautifulSoup):
    for h3 in iter_candidate_clinic_h3_tags(soup):
        name = h3.get_text(" ", strip=True)
        if not name:
            continue
        nodes = []
        node = h3.next_sibling
        while node:
            tag_name = getattr(node, "name", None)
            if tag_name == "h2":
                break
            if tag_name == "h3":
                break
            nodes.append(node)
            node = node.next_sibling
        yield h3, name, nodes


def section_has_review(nodes) -> bool:
    for node in nodes:
        if getattr(node, "name", None) == "div" and (
            "review-section" in (node.get("class") or []) or
            "review-block" in (node.get("class") or [])
        ):
            return True
    return False


def remove_legacy_reviews(nodes) -> int:
    removed = 0
    for node in list(nodes):
        if getattr(node, "name", None) != "div":
            continue
        classes = node.get("class") or []
        if "reviews" not in classes:
            continue
        node.decompose()
        removed += 1
    return removed


def insert_review_into_section(soup: BeautifulSoup, nodes, review_html: str) -> bool:
    fragment = BeautifulSoup(review_html, "html.parser")
    review_tag = fragment.find("div", class_="review-section")
    if review_tag is None:
        return False

    insert_before = None
    for node in nodes:
        if getattr(node, "name", None) == "div" and "clinic-map" in (node.get("class") or []):
            insert_before = node
            break
        if getattr(node, "name", None) == "p" and "official-site-button-wrap" in (node.get("class") or []):
            insert_before = node
            break

    if insert_before is not None:
        insert_before.insert_before(review_tag)
        insert_before.insert_before("\n\n")
        return True

    anchor = None
    for node in reversed(nodes):
        if getattr(node, "name", None) in {"table", "p", "div"}:
            anchor = node
            break
    if anchor is not None:
        anchor.insert_after(review_tag)
        anchor.insert_after("\n\n")
        return True
    return False


def fill_reviews(html_path: str, scraped_dir: str) -> dict[str, int]:
    if not os.path.isdir(scraped_dir):
        raise FileNotFoundError(f"口コミ抽出元ディレクトリが見つかりません: {scraped_dir}")
    review_map = extract_reviews_from_scraped_dir(scraped_dir)
    if not review_map:
        raise FileNotFoundError(f"口コミ抽出元が見つかりません: {scraped_dir}")

    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    replaced = 0
    missing = 0
    removed_legacy = 0

    placeholders = REVIEW_PLACEHOLDER_PATTERN.findall(html)
    if placeholders:
        for placeholder_name in placeholders:
            review_key = find_best_review_key(placeholder_name, review_map)
            review_html = build_review_html(placeholder_name, review_map.get(review_key, []))
            pattern = re.compile(r"\{\{後で作成:口コミ\s*[—\-]\s*" + re.escape(placeholder_name) + r"\s*\}\}")
            if review_html:
                html = pattern.sub(review_html, html)
                replaced += 1
            else:
                html = pattern.sub("", html)
                missing += 1
    else:
        soup = BeautifulSoup(html, "html.parser")
        for _h3, clinic_name, nodes in iter_clinic_sections(soup):
            removed_legacy += remove_legacy_reviews(nodes)
            if section_has_review(nodes):
                continue
            review_key = find_best_review_key(clinic_name, review_map)
            review_html = build_review_html(clinic_name, review_map.get(review_key, []))
            if not review_html:
                continue
            if insert_review_into_section(soup, nodes, review_html):
                replaced += 1
        html = str(soup)

    if placeholders:
        soup = BeautifulSoup(html, "html.parser")
        for _h3, _clinic_name, nodes in iter_clinic_sections(soup):
            removed_legacy += remove_legacy_reviews(nodes)
        html = str(soup)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    return {
        "replaced": replaced,
        "missing": missing,
        "available": len(review_map),
        "removed_legacy": removed_legacy,
        "skipped": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="口コミプレースホルダー置換")
    parser.add_argument("--html", required=True)
    parser.add_argument("--scraped-dir", required=True)
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    result = fill_reviews(args.html, args.scraped_dir)
    print(result)


if __name__ == "__main__":
    main()
