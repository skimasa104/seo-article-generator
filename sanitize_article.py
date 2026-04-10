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


INSTRUCTION_PATTERNS = [
    r"記事末尾に.*?注意書き.*",
    r"ジャンル設定の.*?参照して出力する.*",
    r"タグ構成設計書.*?従い.*",
]


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


def normalize_table_classes(soup: BeautifulSoup) -> dict[str, int]:
    report = {
        "clinic_summary_tables_classed": 0,
        "clinic_detail_tables_classed": 0,
        "compare_tables_classed": 0,
    }

    for table in soup.find_all("table"):
        classes = table.get("class") or []
        if table.find("thead") and "treatment-compare-table" not in classes:
            table["class"] = [cls for cls in classes if cls not in {"clinic-summary-table", "clinic-detail-table"}]
            table["class"].append("treatment-compare-table")
            report["compare_tables_classed"] += 1

    for heading in soup.find_all("h3"):
        if not (heading.get("id") or "").startswith("clinic-"):
            continue

        section_tables = []
        node = heading.find_next_sibling()
        while node and not (
            getattr(node, "name", None) == "h3" and (node.get("id") or "").startswith("clinic-")
        ) and getattr(node, "name", None) != "h2":
            if getattr(node, "name", None) is not None:
                section_tables.extend(node.find_all("table"))
            node = node.find_next_sibling()

        regular_tables = [table for table in section_tables if not table.find("thead")]
        for index, table in enumerate(regular_tables[:2]):
            expected_class = "clinic-summary-table" if index == 0 else "clinic-detail-table"
            classes = [cls for cls in (table.get("class") or []) if cls not in {"clinic-summary-table", "clinic-detail-table"}]
            if expected_class not in classes:
                classes.append(expected_class)
                table["class"] = classes
                if expected_class == "clinic-summary-table":
                    report["clinic_summary_tables_classed"] += 1
                else:
                    report["clinic_detail_tables_classed"] += 1

    return report


def sanitize_article_html(html: str) -> tuple[str, dict]:
    soup = BeautifulSoup(html, "lxml")
    report = {
        "removed_table_rows": 0,
        "replaced_compare_cells": 0,
        "removed_nodes": 0,
        "removed_instruction_nodes": 0,
    }
    report.update(normalize_table_classes(soup))

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

    cleaned, report = sanitize_article_html(html)

    with open(args.html, "w", encoding="utf-8") as f:
        f.write(cleaned)

    print(report)


if __name__ == "__main__":
    main()
