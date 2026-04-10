#!/usr/local/bin/python3.12
"""
記事内のH3構造から一覧ボックスHTMLを生成する。
旧プレースホルダー表記が残っている記事にも後方互換で対応する。
"""

import argparse
import os
import re
import sys

from bs4 import BeautifulSoup
from official_site_utils import normalize_text


PLACEHOLDER_PATTERN = re.compile(r"\{\{後で作成:一覧ボックス[^}]*\}\}")


def slugify_heading(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug or "section"


def normalize_base_name(text: str) -> str:
    text = re.sub(r"[（(].*?[）)]", "", text or "")
    text = re.sub(r"\b(?:men's|mens|メンズ)\b", "", text, flags=re.IGNORECASE)
    return normalize_text(text)


def _has_clinic_section_markers(tag) -> bool:
    node = tag.find_next_sibling()
    while node:
        if getattr(node, "name", None) in ("h2", "h3"):
            break
        if getattr(node, "name", None) == "table":
            return True
        classes = node.get("class") or []
        if any(cls in ("clinic-screenshot", "clinic-map", "review-section", "official-site-button-wrap") for cls in classes):
            return True
        if getattr(node, "name", None) is not None and node.find("table") is not None:
            return True
        node = node.find_next_sibling()
    return False


def is_probable_clinic_heading(tag) -> bool:
    if getattr(tag, "name", None) != "h3":
        return False
    if tag.find_parent("div", class_="clinic-index-box") is not None:
        return False
    classes = tag.get("class") or []
    if any(cls in ("faq-question",) for cls in classes):
        return False
    text = tag.get_text(" ", strip=True)
    if not text:
        return False
    if text.endswith(("?", "？")):
        return False
    if (tag.get("id") or "").startswith("clinic-"):
        return True
    clinic_keywords = (
        "クリニック",
        "医院",
        "皮膚科",
        "皮ふ科",
        "内科",
        "外来",
        "センター",
        "院",
    )
    has_keyword = any(keyword in text for keyword in clinic_keywords)
    has_section_markers = _has_clinic_section_markers(tag)
    return has_section_markers or has_keyword and has_section_markers


def iter_candidate_clinic_h3_tags(soup: BeautifulSoup) -> list:
    return [tag for tag in soup.find_all("h3") if is_probable_clinic_heading(tag)]


def match_anchor_to_heading(anchor, clinic_h3_tags: list):
    anchor_text = normalize_text(anchor.get_text(" ", strip=True))
    anchor_base = normalize_base_name(anchor.get_text(" ", strip=True))

    for tag in clinic_h3_tags:
        full = normalize_text(tag.get_text(" ", strip=True))
        base = normalize_base_name(tag.get_text(" ", strip=True))
        if anchor_text and (anchor_text == full or anchor_text in full or full in anchor_text):
            return tag
        if anchor_base and (anchor_base == base or anchor_base in base or base in anchor_base):
            return tag
    return None


def ensure_existing_list_box_targets(soup: BeautifulSoup) -> bool:
    existing_box = soup.find("div", class_="clinic-index-box")
    if existing_box is None:
        return False

    anchors = existing_box.select("a[href^='#clinic-']")
    if not anchors:
        return True

    clinic_h3_tags = iter_candidate_clinic_h3_tags(soup)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href.startswith("#clinic-"):
            continue
        target_id = href[1:]
        if soup.find("h3", id=target_id) is not None:
            continue

        matched = match_anchor_to_heading(anchor, clinic_h3_tags)
        if matched is not None and (matched.get("id") or "").strip() != target_id:
            matched["id"] = target_id

    return True


def build_list_box(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if ensure_existing_list_box_targets(soup):
        return str(soup), ""

    items = []
    clinic_section_h2 = None
    for h2 in soup.find_all("h2"):
        section_items = []
        node = h2.find_next_sibling()
        while node:
            if getattr(node, "name", None) == "h2":
                break
            if getattr(node, "name", None) == "h3":
                name = node.get_text(" ", strip=True)
                if name and is_probable_clinic_heading(node):
                    clinic_id = (node.get("id") or "").strip()
                    if not clinic_id:
                        clinic_id = "clinic-" + slugify_heading(name)
                        node["id"] = clinic_id
                    section_items.append((clinic_id, name))
            node = node.find_next_sibling()
        if section_items:
            clinic_section_h2 = h2
            items = section_items
            break

    if clinic_section_h2 is None:
        raise ValueError("一覧ボックス化できるクリニック見出しが見つかりません")

    if not items:
        raise ValueError("一覧ボックス化できるクリニック見出しが見つかりません")

    list_items = "\n".join(
        f'  <li><a href="#{clinic_id}">{name}</a></li>'
        for clinic_id, name in items
    )

    list_box = (
        '<div class="clinic-index-box">\n'
        '  <p class="clinic-index-title">気になるクリニックをすぐチェック</p>\n'
        '  <ul class="clinic-index-list">\n'
        f"{list_items}\n"
        "  </ul>\n"
        "</div>"
    )
    return str(soup), list_box


def insert_list_box(html: str, list_box_html: str) -> str:
    if not list_box_html.strip():
        return html

    if 'class="clinic-index-box"' in html:
        return html

    placeholder_updated, count = PLACEHOLDER_PATTERN.subn(list_box_html, html, count=1)
    if count > 0:
        return placeholder_updated

    marker = "<!-- ↑ここまでが導入部分。この直後にWordPress自動生成の目次が入る -->"
    if marker in html:
        return html.replace(marker, list_box_html + "\n\n" + marker, 1)

    shortcode_pattern = re.compile(r"(\[sc name=\"[^\"]+\"\s*\]\[/sc\])", re.IGNORECASE)
    if shortcode_pattern.search(html):
        return shortcode_pattern.sub(r"\1\n\n" + list_box_html, html, count=1)

    intro_close = re.search(r"</div>\s*(?:<!-- ↑ここまでが導入部分.*?-->)?", html, re.DOTALL)
    if intro_close:
        end = intro_close.end()
        return html[:end] + "\n\n" + list_box_html + html[end:]

    return list_box_html + "\n\n" + html


def main():
    parser = argparse.ArgumentParser(description="一覧ボックス自動生成")
    parser.add_argument("--html", required=True, help="記事HTMLファイル")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    normalized_html, list_box_html = build_list_box(html)
    updated_html = insert_list_box(normalized_html, list_box_html)
    if updated_html == html and normalized_html == html:
        print("一覧ボックスはすでに存在していました。")
        return

    with open(args.html, "w", encoding="utf-8") as f:
        f.write(updated_html if updated_html != html else normalized_html)

    print(f"一覧ボックスを生成しました: {args.html}")


if __name__ == "__main__":
    main()
