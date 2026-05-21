#!/usr/local/bin/python3.12
"""
未確定情報や生成指示文が残った記事HTMLを、安全側に倒して整形する。

- `※要確認` を含む詳細表の行は削除
- 比較表セル内の `※要確認` は「公式サイトをご確認ください」に置換
- `※要確認` を含む段落やリストは削除
- 生成指示文の混入を削除
"""

import argparse
import os
import re
import sys

from bs4 import BeautifulSoup, NavigableString
from fill_list_box import is_generic_clinic_heading, is_probable_clinic_heading
from variant_utils import inline_variant_shortcodes_in_html


INSTRUCTION_PATTERNS = [
    r"記事末尾に.*?注意書き.*",
    r"ジャンル設定の.*?参照して出力する.*",
    r"タグ構成設計書.*?従い.*",
]


def infer_variant_index_from_html_path(path: str) -> int:
    match = re.search(r"__nandemo_v(\d+)", path or "")
    if not match:
        return 1
    try:
        return max(1, min(5, int(match.group(1))))
    except ValueError:
        return 1


def is_compare_table(table) -> bool:
    classes = table.get("class") or []
    return "treatment-compare-table" in classes


def sanitize_compare_table(table) -> int:
    changed = 0
    for cell in table.find_all(["td", "th"]):
        text = cell.get_text(" ", strip=True)
        if "※要確認" not in text:
            continue
        cell.clear()
        cell.append("公式サイトをご確認ください")
        changed += 1
    return changed


def sanitize_regular_table(table) -> int:
    changed = 0
    for row in list(table.find_all("tr")):
        text = row.get_text(" ", strip=True)
        if "※要確認" not in text:
            continue
        row.decompose()
        changed += 1
    return changed


def is_item_content_table(table) -> bool:
    thead = table.find("thead")
    if thead is None:
        return False
    headers = [cell.get_text(" ", strip=True) for cell in thead.find_all(["th", "td"])]
    normalized = [header.replace(" ", "") for header in headers]
    if len(normalized) < 2:
        return False
    return normalized[:2] in (["項目", "内容"], ["比較項目", "内容"])


def convert_item_content_table_to_detail(table) -> bool:
    if not is_item_content_table(table):
        return False
    tbody = table.find("tbody")
    if tbody is None:
        return False
    thead = table.find("thead")
    if thead is not None:
        thead.decompose()
    for row in tbody.find_all("tr", recursive=False):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) < 2:
            continue
        first = cells[0]
        if first.name != "th":
            first.name = "th"
        for link in first.find_all("a", href=True):
            link.unwrap()
    table["class"] = [cls for cls in (table.get("class") or []) if cls != "treatment-compare-table"]
    if "clinic-detail-table" not in table["class"]:
        table["class"].append("clinic-detail-table")
    return True


def normalize_table_classes(soup: BeautifulSoup) -> dict[str, int]:
    report = {
        "clinic_summary_tables_classed": 0,
        "clinic_detail_tables_classed": 0,
        "compare_tables_classed": 0,
        "detail_tables_converted": 0,
    }

    for heading in soup.find_all("h3"):
        heading_text = heading.get_text(" ", strip=True)
        heading_id = heading.get("id") or ""
        if not (
            heading_id.startswith("clinic-")
            or (is_probable_clinic_heading(heading) and not is_generic_clinic_heading(heading_text))
        ):
            continue
        node = heading.find_next_sibling()
        while node and not (
            getattr(node, "name", None) == "h3"
            and (
                ((node.get("id") or "").startswith("clinic-"))
                or (is_probable_clinic_heading(node) and not is_generic_clinic_heading(node.get_text(" ", strip=True)))
            )
        ) and getattr(node, "name", None) != "h2":
            if getattr(node, "name", None) is not None:
                if getattr(node, "name", None) == "table":
                    if convert_item_content_table_to_detail(node):
                        report["detail_tables_converted"] += 1
                for table in node.find_all("table"):
                    if convert_item_content_table_to_detail(table):
                        report["detail_tables_converted"] += 1
            node = node.find_next_sibling()

    for table in soup.find_all("table"):
        classes = table.get("class") or []
        if table.find("thead") and "treatment-compare-table" not in classes and not is_item_content_table(table):
            table["class"] = [cls for cls in classes if cls not in {"clinic-summary-table", "clinic-detail-table"}]
            table["class"].append("treatment-compare-table")
            report["compare_tables_classed"] += 1

    for heading in soup.find_all("h3"):
        heading_text = heading.get_text(" ", strip=True)
        heading_id = heading.get("id") or ""
        if not (
            heading_id.startswith("clinic-")
            or (is_probable_clinic_heading(heading) and not is_generic_clinic_heading(heading_text))
        ):
            continue

        section_tables = []
        node = heading.find_next_sibling()
        while node and not (
            getattr(node, "name", None) == "h3"
            and (
                ((node.get("id") or "").startswith("clinic-"))
                or (is_probable_clinic_heading(node) and not is_generic_clinic_heading(node.get_text(" ", strip=True)))
            )
        ) and getattr(node, "name", None) != "h2":
            if getattr(node, "name", None) is not None:
                if getattr(node, "name", None) == "table":
                    section_tables.append(node)
                section_tables.extend(node.find_all("table"))
            node = node.find_next_sibling()

        regular_tables = [table for table in section_tables if not table.find("thead")]
        for index, table in enumerate(regular_tables[:2]):
            expected_class = "clinic-summary-table" if len(regular_tables) > 1 and index == 0 else "clinic-detail-table"
            classes = [cls for cls in (table.get("class") or []) if cls not in {"clinic-summary-table", "clinic-detail-table"}]
            if expected_class not in classes:
                classes.append(expected_class)
                table["class"] = classes
                if expected_class == "clinic-summary-table":
                    report["clinic_summary_tables_classed"] += 1
                else:
                    report["clinic_detail_tables_classed"] += 1

    return report


def normalize_legacy_review_blocks(soup: BeautifulSoup) -> int:
    converted = 0
    for block in list(soup.find_all("div", class_="review-block")):
        if block.find(class_="review-section") is not None:
            continue
        review_texts = []
        paragraphs = block.find_all("p", class_="review-text") or block.find_all("p")
        for paragraph in paragraphs:
            text = paragraph.get_text(" ", strip=True)
            if text:
                review_texts.append(text.strip("「」"))
        if not review_texts:
            block.decompose()
            converted += 1
            continue
        section = soup.new_tag("div", attrs={"class": "review-section"})
        heading = soup.new_tag("h4")
        heading.string = "口コミ・評判"
        section.append(heading)
        for index, text in enumerate(review_texts, start=1):
            bubble = soup.new_tag("div", attrs={"class": "review-bubble"})
            avatar = soup.new_tag("div", attrs={"class": "review-avatar"})
            avatar.string = " "
            body = soup.new_tag("div", attrs={"class": "review-body"})
            meta = soup.new_tag("div", attrs={"class": "review-meta"})
            stars = soup.new_tag("span", attrs={"class": "review-stars"})
            stars.string = "★★★★★"
            label = soup.new_tag("span")
            label.string = f"口コミ {index}"
            meta.append(stars)
            meta.append(label)
            body.append(meta)
            body.append(text)
            bubble.append(avatar)
            bubble.append(body)
            section.append(bubble)
        block.replace_with(section)
        converted += 1
    return converted


def strip_links_from_clinic_tables(soup: BeautifulSoup) -> int:
    removed = 0
    for table in soup.find_all("table"):
        classes = set(table.get("class") or [])
        if not classes & {"clinic-summary-table", "clinic-detail-table"}:
            continue
        for link in list(table.find_all("a", href=True)):
            link.unwrap()
            removed += 1
    return removed


def is_responsive_table_target(table) -> bool:
    classes = set(table.get("class") or [])
    return bool(classes & {"treatment-compare-table", "clinic-summary-table", "clinic-detail-table"})


def ensure_table_wrappers(soup: BeautifulSoup) -> int:
    wrapped = 0
    allowed_wrapper_classes = {
        "table-scroll",
        "comparison-table",
        "comparison-table-container",
        "ndm-aga-v1__tablewrap",
        "ndm-aga-v2__tablewrap",
        "ndm-aga-v3__tablewrap",
        "ndm-aga-v4__tablewrap",
        "ndm-aga-v5__tablewrap",
    }

    for table in soup.find_all("table"):
        if not is_responsive_table_target(table):
            continue
        parent = table.parent
        if getattr(parent, "name", None) == "div":
            parent_classes = set(parent.get("class") or [])
            parent_style = (parent.get("style") or "").replace(" ", "").lower()
            if parent_classes & allowed_wrapper_classes or "overflow-x:auto" in parent_style:
                continue

        wrapper = soup.new_tag("div", attrs={"class": "table-scroll"})
        table.wrap(wrapper)
        wrapped += 1

    return wrapped


def normalize_official_site_buttons(soup: BeautifulSoup) -> int:
    changed = 0
    button_style = (
        "display:inline-flex;align-items:center;justify-content:center;"
        "width:min(100%,420px);max-width:100%;"
        "background:#0f6cbd;color:#fff;padding:14px 24px;border-radius:999px;"
        "text-decoration:none;font-size:15px;font-weight:bold;line-height:1.5;text-align:center;"
    )
    for wrap in soup.find_all(class_="official-site-button-wrap"):
        links = wrap.find_all("a", href=True)
        for link in links:
            classes = set(link.get("class") or [])
            if "official-site-button" not in classes:
                link["class"] = list(classes | {"official-site-button"})
            link.string = "公式サイトを見る"
            link["style"] = button_style
            changed += 1
    return changed


def is_inside_ndm_hayamihyou(node) -> bool:
    current = getattr(node, "parent", None)
    while current is not None:
        classes = current.get("class") or []
        if any(cls.startswith("ndm-aga-v") for cls in classes):
            return True
        current = getattr(current, "parent", None)
    return False


def flatten_compare_table_linebreaks(soup: BeautifulSoup) -> int:
    changed = 0
    for table in soup.find_all("table", class_="treatment-compare-table"):
        if is_inside_ndm_hayamihyou(table):
            continue
        for br in list(table.find_all("br")):
            br.replace_with(NavigableString(" / "))
            changed += 1
    return changed


def remove_generic_heading_decoration(soup: BeautifulSoup) -> dict[str, int]:
    report = {
        "removed_generic_heading_ids": 0,
        "removed_generic_heading_blocks": 0,
    }

    removable_classes = {"clinic-screenshot", "clinic-map", "review-section", "official-site-button-wrap"}

    for heading in soup.find_all("h3"):
        heading_text = heading.get_text(" ", strip=True)
        if is_probable_clinic_heading(heading) and not is_generic_clinic_heading(heading_text):
            continue

        if (heading.get("id") or "").startswith("clinic-"):
            del heading["id"]
            report["removed_generic_heading_ids"] += 1

        node = heading.find_next_sibling()
        while node and getattr(node, "name", None) not in {"h2", "h3"}:
            next_node = node.find_next_sibling()
            classes = set(node.get("class") or [])
            if classes & removable_classes:
                node.decompose()
                report["removed_generic_heading_blocks"] += 1
            node = next_node

    return report


def sanitize_article_html(html: str, html_path: str = "") -> tuple[str, dict]:
    html = inline_variant_shortcodes_in_html(
        html,
        genre_id="aga",
        output_key=html_path,
        variant_index=infer_variant_index_from_html_path(html_path),
    )
    soup = BeautifulSoup(html, "lxml")
    report = {
        "removed_table_rows": 0,
        "replaced_compare_cells": 0,
        "removed_nodes": 0,
        "removed_instruction_nodes": 0,
    }
    report.update(normalize_table_classes(soup))
    report["legacy_review_blocks_converted"] = normalize_legacy_review_blocks(soup)
    report["clinic_table_links_removed"] = strip_links_from_clinic_tables(soup)
    report["compare_tables_wrapped"] = ensure_table_wrappers(soup)
    report["official_site_buttons_normalized"] = normalize_official_site_buttons(soup)
    report["compare_table_linebreaks_flattened"] = flatten_compare_table_linebreaks(soup)
    report.update(remove_generic_heading_decoration(soup))

    for table in soup.find_all("table"):
        if is_compare_table(table):
            report["replaced_compare_cells"] += sanitize_compare_table(table)
        else:
            report["removed_table_rows"] += sanitize_regular_table(table)

    removable_tags = ["p", "li", "div", "span", "small"]
    for tag in list(soup.find_all(removable_tags)):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        if "※要確認" in text:
            tag.decompose()
            report["removed_nodes"] += 1
            continue
        if any(re.search(pattern, text) for pattern in INSTRUCTION_PATTERNS):
            tag.decompose()
            report["removed_instruction_nodes"] += 1

    for text_node in list(soup.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue
        text = str(text_node)
        if "※要確認" in text:
            text_node.extract()
            report["removed_nodes"] += 1
            continue
        if any(re.search(pattern, text) for pattern in INSTRUCTION_PATTERNS):
            text_node.extract()
            report["removed_instruction_nodes"] += 1

    if soup.body is not None:
        cleaned = soup.body.decode_contents().strip()
    else:
        cleaned = str(soup)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    # {{後で作成:...}} 形式のプレースホルダーを完全除去（タグの内外問わず）
    placeholder_count = len(re.findall(r"\{\{後で作成:[^}]*\}\}", cleaned))
    cleaned = re.sub(r"\{\{後で作成:[^}]*\}\}", "", cleaned)
    if placeholder_count:
        report["removed_placeholder_nodes"] = placeholder_count

    return cleaned, report


def main() -> None:
    parser = argparse.ArgumentParser(description="未確定情報の整形")
    parser.add_argument("--html", required=True, help="記事HTMLファイル")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    cleaned, report = sanitize_article_html(html, args.html)

    with open(args.html, "w", encoding="utf-8") as f:
        f.write(cleaned)

    print(report)


if __name__ == "__main__":
    main()
