#!/usr/local/bin/python3.12
"""
スクレイピングデータから口コミを抽出し、記事HTMLに口コミセクションを補完する。
旧プレースホルダー表記が残っている記事にも後方互換で対応する。
"""

import argparse
import os
import re
import sys

from bs4 import BeautifulSoup

REVIEW_PLACEHOLDER_PATTERN = re.compile(r"\{\{後で作成:口コミ\s*[—\-]\s*(.+?)\s*\}\}")


def normalize_name(text: str) -> str:
    text = text or ""
    aliases = {
        "イースト駅前クリニック": "イースト駅前クリニック新宿東口院・新宿西口院",
        "AGAヘアクリニック": "AHCメディカルサロン新宿（AGAヘアクリニック新宿）",
        "AGAスキンクリニック": "AGAスキンクリニック新宿駅前院",
        "Dクリニック": "Dクリニック新宿",
        "ゴリラクリニック": "ゴリラクリニック新宿本院",
        "湘南AGAクリニック": "湘南AGAクリニック新宿本院",
        "AGAメディカルケアクリニック": "AGAメディカルケアクリニック新宿院",
        "ウィルAGAクリニック": "ウィルAGAクリニック新宿院",
        "スマイルAGAクリニック": "スマイルAGAクリニック新宿院",
    }
    return aliases.get(text.strip(), text.strip())


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

        if line.startswith("### [H3]") or line.startswith("### [H2]"):
            current_name = None

    return reviews


def build_review_html(clinic_name: str, review_texts: list[str]) -> str:
    blocks = []
    for idx, text in enumerate(review_texts[:2], start=1):
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        blocks.append(
            '<div class="review-bubble">'
            '<div class="review-avatar"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#999" width="22" height="22"><path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/></svg></div>'
            '<div class="review-body"><div class="review-meta"><span class="review-stars">Google口コミ</span>'
            f"<span>口コミ{idx}</span></div>{safe_text}</div></div>"
        )
    if not blocks:
        return ""

    return '<div class="review-section"><h4>口コミ・評判</h4>' + "".join(blocks) + "</div>"


def html_needs_reviews(html: str) -> bool:
    return bool(REVIEW_PLACEHOLDER_PATTERN.search(html))


def iter_clinic_sections(soup: BeautifulSoup):
    for h3 in soup.find_all("h3"):
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
    structure_path = os.path.join(scraped_dir, "article_1_structure.md")
    if not os.path.exists(structure_path):
        raise FileNotFoundError(f"口コミ抽出元が見つかりません: {structure_path}")

    with open(structure_path, encoding="utf-8") as f:
        structure_text = f.read()
    review_map = extract_reviews_from_structure(structure_text)

    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    replaced = 0
    missing = 0
    removed_legacy = 0

    placeholders = REVIEW_PLACEHOLDER_PATTERN.findall(html)
    if placeholders:
        for placeholder_name in placeholders:
            canonical_name = normalize_name(placeholder_name)
            review_html = build_review_html(canonical_name, review_map.get(canonical_name, []))
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
            canonical_name = normalize_name(clinic_name)
            if section_has_review(nodes):
                continue
            review_html = build_review_html(canonical_name, review_map.get(canonical_name, []))
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
