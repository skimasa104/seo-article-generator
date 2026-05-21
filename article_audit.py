import re
from collections import Counter

from bs4 import BeautifulSoup, Tag
from fill_list_box import is_probable_clinic_heading, iter_candidate_clinic_h3_tags, match_text_to_heading


def extract_clinic_h3_tags(html: str) -> list[Tag]:
    soup = BeautifulSoup(html, "lxml")
    return iter_candidate_clinic_h3_tags(soup)


def extract_clinic_names(html: str) -> list[str]:
    return [tag.get_text(" ", strip=True) for tag in extract_clinic_h3_tags(html)]


def extract_h4_names_inside_clinic_lists(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    names = []
    for tag in soup.select(".clinic-list .clinic-item h4"):
        names.append(tag.get_text(" ", strip=True))
    return names


def count_clinic_like_h3_sections(soup: BeautifulSoup) -> int:
    count = 0
    for heading in soup.find_all("h3"):
        tag_id = (heading.get("id") or "").strip()
        if tag_id.startswith("clinic-"):
            count += 1
            continue

        node = heading.find_next_sibling()
        while node and getattr(node, "name", None) not in {"h2", "h3"}:
            classes = node.get("class") or []
            if "clinic-screenshot" in classes or "official-site-button-wrap" in classes:
                count += 1
                break
            node = node.find_next_sibling()
    return count


def section_nodes_until_next_heading(heading: Tag) -> list[Tag]:
    nodes = []
    node = heading.find_next_sibling()
    while node and getattr(node, "name", None) not in {"h2", "h3"}:
        nodes.append(node)
        node = node.find_next_sibling()
    return nodes


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
    clinic_section_count = count_clinic_like_h3_sections(soup)
    if clinic_section_count and screenshot_block_count > clinic_section_count:
        issues.append("スクリーンショットブロック数がクリニック数を超えています")

    if 'class="reviews"' in html or 'blockquote class="review"' in html:
        issues.append("旧口コミブロック(.reviews / blockquote.review)が残っています")

    if re.search(r"\*\*[^*\n][^*\n]*\*\*", html):
        issues.append("Markdown の強調記法(**...**)がHTML内に残っています")

    clinic_h3_tags = iter_candidate_clinic_h3_tags(soup)
    for heading in soup.find_all("h3"):
        if is_probable_clinic_heading(heading):
            continue
        nodes = section_nodes_until_next_heading(heading)
        for node in nodes:
            classes = set(node.get("class") or [])
            if classes & {"clinic-screenshot", "official-site-button-wrap", "clinic-map", "review-section"}:
                issues.append(f"カテゴリH3に個別クリニック装飾が混入しています: {heading.get_text(' ', strip=True)}")
                break

    for h2 in soup.find_all("h2"):
        heading_text = h2.get_text(" ", strip=True)
        if "クリニック" not in heading_text:
            continue
        # 「比較」「一覧」系のH2だけを比較表必須として扱う。
        # 「おすすめクリニック13選」のような選定リスト章は、H3列挙が主構造なので除外する。
        if not any(token in heading_text for token in ("比較", "一覧", "早見表")):
            continue
        # 「比較ポイント」「比較で注意」「比較する際」など、比較を題材とした
        # 説明見出しは比較表が無くても自然なので除外する。
        if any(
            token in heading_text
            for token in (
                "ガイド",
                "エリア別",
                "よくある質問",
                "おすすめな人",
                "向いている人",
                "向かない人",
                "選ぶべき人",
                "比較ポイント",
                "比較する際",
                "比較するとき",
                "比較で注意",
                "比較の注意",
                "比較するうえ",
                "選び方",
                "違い",
                "注意点",
                "メリット",
                "デメリット",
            )
        ):
            continue
        section_nodes = []
        node = h2.find_next_sibling()
        while node and getattr(node, "name", None) != "h2":
            section_nodes.append(node)
            node = node.find_next_sibling()
        if not any(
            getattr(node, "name", None) == "table" and "treatment-compare-table" in (node.get("class") or [])
            or (
                getattr(node, "name", None) is not None
                and node.find("table", class_="treatment-compare-table") is not None
            )
            for node in section_nodes
        ):
            issues.append(f"比較系H2直下に比較表がありません: {heading_text}")

    for heading in clinic_h3_tags:
        regular_tables = []
        node = heading.find_next_sibling()
        while node and not (
            getattr(node, "name", None) == "h3" and is_probable_clinic_heading(node)
        ) and getattr(node, "name", None) != "h2":
            if getattr(node, "name", None) is not None:
                if getattr(node, "name", None) == "table" and not node.find("thead"):
                    regular_tables.append(node)
                regular_tables.extend(table for table in node.find_all("table") if not table.find("thead"))
            node = node.find_next_sibling()

        if not regular_tables:
            issues.append(f"個別クリニックH3配下に詳細テーブルがありません: {heading.get_text(' ', strip=True)}")
        if regular_tables:
            first_classes = regular_tables[0].get("class") or []
            if len(regular_tables) == 1:
                if not ({"clinic-summary-table", "clinic-detail-table"} & set(first_classes)):
                    issues.append(f"H3セクションの最初のテーブルに clinic-summary-table / clinic-detail-table がありません: {heading.get_text(' ', strip=True)}")
            elif "clinic-summary-table" not in first_classes:
                issues.append(f"H3セクションの最初のテーブルに clinic-summary-table がありません: {heading.get_text(' ', strip=True)}")
        if len(regular_tables) >= 2:
            second_classes = regular_tables[1].get("class") or []
            if "clinic-detail-table" not in second_classes:
                issues.append(f"H3セクションの2番目のテーブルに clinic-detail-table がありません: {heading.get_text(' ', strip=True)}")

    for table in soup.find_all("table", class_="treatment-compare-table"):
        for anchor in table.select("td a[href^='#']"):
            target_id = (anchor.get("href") or "").strip()[1:]
            if target_id and soup.find(id=target_id) is not None:
                continue
            if match_text_to_heading(anchor.get_text(" ", strip=True), clinic_h3_tags) is not None:
                continue
            issues.append(f"比較テーブル内リンクの飛び先が見つかりません: {anchor.get_text(' ', strip=True)}")

    return issues
