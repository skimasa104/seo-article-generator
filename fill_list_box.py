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
    text = re.split(r"\s*[|｜]\s*", text or "", maxsplit=1)[0]
    text = re.sub(r"[（(].*?[）)]", "", text or "")
    text = re.sub(r"\b(?:men's|mens|メンズ)\b", "", text, flags=re.IGNORECASE)
    return normalize_text(text)


GENERIC_TOPIC_KEYWORDS = (
    "口コミ",
    "評判",
    "効果",
    "副作用",
    "費用",
    "料金",
    "治療",
    "診療",
    "オンライン診療",
    "対面診療",
    "メリット",
    "デメリット",
    "流れ",
    "特徴",
    "内容",
    "比較",
    "ポイント",
    "制度",
    "安全性",
    "プラン",
    "オプション",
)

GENERIC_CLINIC_HEADING_PATTERNS = (
    "おすすめのクリニック",
    "クリニックガイド",
    "エリア別",
    "治療方法別",
    "選ぶとき",
    "費用相場",
    "よくある質問",
)


def looks_like_clinic_entity_name(text: str) -> bool:
    base_text = normalize_base_name(text or "")
    if not base_text:
        return False
    if any(keyword in base_text for keyword in GENERIC_TOPIC_KEYWORDS):
        return False
    if any(keyword in base_text for keyword in ("クリニック", "医院", "病院", "皮膚科", "皮ふ科", "内科", "院", "センター")):
        return True
    if re.search(r"(とは|できる|する|した|して|方法|内容|流れ|ポイント|比較|まとめ|おすすめ)", base_text):
        return False
    if "の" in base_text and len(base_text) > 8:
        return False
    return len(base_text) <= 18


def is_generic_clinic_heading(text: str) -> bool:
    normalized = normalize_base_name(text or "")
    if not normalized:
        return True
    if any(pattern in normalized for pattern in GENERIC_CLINIC_HEADING_PATTERNS):
        return True
    if "おすすめ" in normalized and normalized.endswith("クリニック"):
        return True
    if any(keyword in normalized for keyword in ("エリア", "方法", "ポイント", "相場", "質問", "ガイド")):
        return True
    return False


def iter_intro_anchor_boxes(soup: BeautifulSoup) -> list:
    boxes = []
    for selector in (".clinic-index-box", ".clinic-list-box"):
        boxes.extend(soup.select(selector))
    return boxes


def find_intro_list_box(soup: BeautifulSoup):
    for box in iter_intro_anchor_boxes(soup):
        if box.select_one(".clinic-index-title") is not None:
            return box
        if box.select_one("a[href^='#']") is not None:
            return box
    return None


def has_valid_intro_list_box(soup: BeautifulSoup) -> bool:
    box = find_intro_list_box(soup)
    if box is None:
        return False
    return bool(box.select_one("a[href^='#']"))


def intro_list_box_has_only_valid_targets(soup: BeautifulSoup) -> bool:
    box = find_intro_list_box(soup)
    if box is None:
        return False

    anchors = box.select("a[href^='#']")
    if not anchors:
        return False

    clinic_h3_tags = iter_candidate_clinic_h3_tags(soup)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href.startswith("#"):
            return False
        target_id = href[1:]
        if target_id and soup.find(id=target_id) is not None:
            continue
        if match_anchor_to_heading(anchor, clinic_h3_tags) is None:
            return False
    return True


def intro_list_box_needs_rebuild(soup: BeautifulSoup) -> bool:
    boxes = iter_intro_anchor_boxes(soup)
    if not boxes:
        return False
    if any("clinic-list-box" in (box.get("class") or []) for box in boxes):
        return True
    return not intro_list_box_has_only_valid_targets(soup)


def _has_clinic_section_markers(tag) -> bool:
    node = tag.find_next_sibling()
    while node:
        if getattr(node, "name", None) in ("h2", "h3"):
            break
        if getattr(node, "name", None) == "table":
            return True
        classes = node.get("class") or []
        if any(cls in ("clinic-screenshot", "clinic-map", "official-site-button-wrap") for cls in classes):
            return True
        if getattr(node, "name", None) is not None and node.find("table") is not None:
            return True
        node = node.find_next_sibling()
    return False


def _has_strong_clinic_section_markers(tag) -> bool:
    node = tag.find_next_sibling()
    while node:
        if getattr(node, "name", None) in ("h2", "h3"):
            break
        classes = node.get("class") or []
        if any(cls in ("clinic-screenshot", "clinic-map", "official-site-button-wrap") for cls in classes):
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
    if is_generic_clinic_heading(text):
        return False
    if text.endswith(("?", "？")):
        return False
    if any(token in text for token in ("口コミ", "評判")) and not _has_clinic_section_markers(tag):
        return False
    entity_text = re.split(r"\s*[|｜]\s*", text, maxsplit=1)[0].strip()
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
    has_keyword = any(keyword in entity_text for keyword in clinic_keywords)
    has_section_markers = _has_clinic_section_markers(tag)
    has_strong_markers = _has_strong_clinic_section_markers(tag)
    has_generic_topic = any(keyword in entity_text for keyword in GENERIC_TOPIC_KEYWORDS)
    existing_clinic_id = (tag.get("id") or "").startswith("clinic-")
    has_comparison_label = "｜" in text or "|" in text
    entity_like = looks_like_clinic_entity_name(entity_text)

    # 比較記事の個別セクションだけを拾う。
    # 「表がある一般解説見出し」を clinic 候補にすると、Know記事で誤って
    # スクリーンショット必須扱いになってしまうため、条件をかなり絞る。
    if existing_clinic_id and (has_strong_markers or entity_like or (has_keyword and not has_generic_topic)):
        return True
    if has_strong_markers and (entity_like or has_comparison_label or (has_keyword and not has_generic_topic)):
        return True
    if has_section_markers and (entity_like or (has_keyword and not has_generic_topic)):
        return True
    if has_section_markers and has_comparison_label and not has_generic_topic:
        return True
    return False


def iter_candidate_clinic_h3_tags(soup: BeautifulSoup) -> list:
    return [tag for tag in soup.find_all("h3") if is_probable_clinic_heading(tag)]


def match_anchor_to_heading(anchor, clinic_h3_tags: list):
    return match_text_to_heading(anchor.get_text(" ", strip=True), clinic_h3_tags)


def match_text_to_heading(text: str, clinic_h3_tags: list):
    anchor_text = normalize_text(text)
    anchor_base = normalize_base_name(text)

    for tag in clinic_h3_tags:
        full = normalize_text(tag.get_text(" ", strip=True))
        base = normalize_base_name(tag.get_text(" ", strip=True))
        if anchor_text and (anchor_text == full or anchor_text in full or full in anchor_text):
            return tag
        if anchor_base and (anchor_base == base or anchor_base in base or base in anchor_base):
            return tag
    return None


def ensure_heading_id(soup: BeautifulSoup, tag, fallback_text: str) -> str:
    existing_id = (tag.get("id") or "").strip()
    if existing_id:
        return existing_id
    slug_base = "clinic-" + slugify_heading(normalize_base_name(fallback_text) or fallback_text)
    candidate = slug_base
    suffix = 2
    while soup.find(id=candidate) is not None:
        candidate = f"{slug_base}-{suffix}"
        suffix += 1
    tag["id"] = candidate
    return candidate


def ensure_existing_list_box_targets(soup: BeautifulSoup) -> bool:
    boxes = iter_intro_anchor_boxes(soup)
    if not boxes:
        return False

    anchors = []
    for box in boxes:
        anchors.extend(box.select("a[href^='#']"))
    if not anchors:
        return False

    clinic_h3_tags = iter_candidate_clinic_h3_tags(soup)
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href.startswith("#"):
            continue
        target_id = href[1:]
        if target_id and soup.find(id=target_id) is not None:
            continue

        matched = match_anchor_to_heading(anchor, clinic_h3_tags)
        if matched is None:
            continue

        if target_id:
            matched["id"] = target_id
        else:
            target_id = ensure_heading_id(soup, matched, anchor.get_text(" ", strip=True))
            anchor["href"] = f"#{target_id}"

    return True


def ensure_compare_table_links(soup: BeautifulSoup) -> None:
    clinic_h3_tags = iter_candidate_clinic_h3_tags(soup)
    if not clinic_h3_tags:
        return

    for table in soup.find_all("table"):
        classes = table.get("class") or []
        if "treatment-compare-table" not in classes:
            continue
        header_cells = table.find("thead").find_all(["th", "td"]) if table.find("thead") else []
        header_texts = [normalize_text(cell.get_text(" ", strip=True)) for cell in header_cells]
        if header_texts:
            first_header = header_texts[0]
            if first_header not in {"クリニック名", "院名", "医院名", "比較項目"}:
                continue
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if not cells:
                continue
            first_cell = cells[0]
            label = first_cell.get_text(" ", strip=True)
            existing_link = first_cell.find("a", href=True)
            if not looks_like_clinic_entity_name(label):
                if existing_link is not None:
                    existing_link.unwrap()
                continue
            matched = match_text_to_heading(label, clinic_h3_tags)
            if matched is None:
                if existing_link is not None:
                    existing_link.unwrap()
                continue
            target_id = ensure_heading_id(soup, matched, label)
            if existing_link is not None:
                existing_link["href"] = f"#{target_id}"
                continue
            first_cell.clear()
            link = soup.new_tag("a", href=f"#{target_id}")
            link.string = label
            first_cell.append(link)


def dedupe_intro_list_boxes(soup: BeautifulSoup) -> None:
    boxes = iter_intro_anchor_boxes(soup)
    if len(boxes) <= 1:
        return
    keep = next((box for box in boxes if "clinic-index-box" in (box.get("class") or [])), boxes[0])
    for box in boxes:
        if box is keep:
            continue
        box.decompose()


def build_list_box(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    ensure_existing_list_box_targets(soup)
    ensure_compare_table_links(soup)
    dedupe_intro_list_boxes(soup)
    if has_valid_intro_list_box(soup) and not intro_list_box_needs_rebuild(soup):
        return str(soup), ""

    if intro_list_box_needs_rebuild(soup):
        for box in iter_intro_anchor_boxes(soup):
            box.decompose()

    items = []
    clinic_section_h2 = None
    best_score = (-1, -1)
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
            heading_text = h2.get_text(" ", strip=True)
            score = (
                len(section_items),
                1 if "おすすめ" in heading_text and "クリニック" in heading_text else 0,
            )
            if score <= best_score:
                continue
            best_score = score
            clinic_section_h2 = h2
            items = section_items

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

    soup = BeautifulSoup(html, "html.parser")
    if has_valid_intro_list_box(soup):
        return html

    existing_box = find_intro_list_box(soup)
    if existing_box is not None:
        return html.replace(str(existing_box), list_box_html, 1)

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
