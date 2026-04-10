import re
from collections import Counter

from bs4 import BeautifulSoup, Tag


def extract_clinic_h3_tags(html: str) -> list[Tag]:
    soup = BeautifulSoup(html, "lxml")
    return [
        tag for tag in soup.find_all("h3")
        if (tag.get("id") or "").startswith("clinic-")
    ]


def extract_clinic_names(html: str) -> list[str]:
    return [tag.get_text(" ", strip=True) for tag in extract_clinic_h3_tags(html)]


def extract_h4_names_inside_clinic_lists(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    names = []
    for tag in soup.select(".clinic-list .clinic-item h4"):
        names.append(tag.get_text(" ", strip=True))
    return names


def validate_article_html(html: str, keyword_slug: str) -> list[str]:
    issues: list[str] = []
    soup = BeautifulSoup(html, "lxml")
    clinic_names = extract_clinic_names(html)
    duplicate_clinics = [name for name, count in Counter(clinic_names).items() if count > 1]
    if duplicate_clinics:
        issues.append("H3クリニック見出しが重複しています: " + ", ".join(duplicate_clinics))

    h4_names = extract_h4_names_inside_clinic_lists(html)
    repeated_between_h3_h4 = sorted(set(name for name in h4_names if name in clinic_names))
    if repeated_between_h3_h4:
        issues.append("H3セクションとclinic-list内H4で同じクリニックが重複しています: " + ", ".join(repeated_between_h3_h4))

    if html.count("<div") != html.count("</div>"):
        issues.append("divタグの開始数と終了数が一致していません")

    if re.search(rf'images/{re.escape(keyword_slug)}_top\.(png|jpg|jpeg|webp)', html, re.IGNORECASE):
        issues.append("本文内にトップ画像タグが残っています")

    if re.search(rf'images/{re.escape(keyword_slug)}_h2_\d+\.jpg', html, re.IGNORECASE):
        issues.append("旧H2画像プレースホルダー(.jpg)が残っています")

    screenshot_block_count = html.count('class="clinic-screenshot"')
    if clinic_names and screenshot_block_count > len(clinic_names):
        issues.append("スクリーンショットブロック数がクリニック数を超えています")

    if 'class="reviews"' in html or 'blockquote class="review"' in html:
        issues.append("旧口コミブロック(.reviews / blockquote.review)が残っています")

    for heading in soup.find_all("h3"):
        if not (heading.get("id") or "").startswith("clinic-"):
            continue
        regular_tables = []
        node = heading.find_next_sibling()
        while node and not (
            getattr(node, "name", None) == "h3" and (node.get("id") or "").startswith("clinic-")
        ) and getattr(node, "name", None) != "h2":
            if getattr(node, "name", None) is not None:
                regular_tables.extend(table for table in node.find_all("table") if not table.find("thead"))
            node = node.find_next_sibling()

        if regular_tables:
            first_classes = regular_tables[0].get("class") or []
            if "clinic-summary-table" not in first_classes:
                issues.append(f"H3セクションの最初のテーブルに clinic-summary-table がありません: {heading.get_text(' ', strip=True)}")
        if len(regular_tables) >= 2:
            second_classes = regular_tables[1].get("class") or []
            if "clinic-detail-table" not in second_classes:
                issues.append(f"H3セクションの2番目のテーブルに clinic-detail-table がありません: {heading.get_text(' ', strip=True)}")

    return issues
