#!/usr/local/bin/python3.12
"""
公式サイトの情報を使って記事HTMLをファクトチェックし、必要な修正を反映する。
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from article_audit import extract_clinic_names, validate_article_html
from env_utils import load_project_env
from generate_article import call_claude, load_api_key
from official_site_utils import find_official_url, normalize_clinic_lookup_name

load_project_env()

FETCH_TIMEOUT = 20
MAX_SOURCE_CHARS = 6000

SYSTEM_PROMPT = """あなたは医療系SEO記事の校正・ファクトチェッカーです。
与えられたHTML記事を、各クリニックの公式サイト抜粋に照らして確認し、確実に裏取りできる範囲だけを修正してください。

厳守ルール:
- HTML以外は出力しない
- セクション構成、見出し数、id、class、表の列構造を変えない
- 新しいクリニックを追加しない
- 同じクリニックを重複させない
- 不明な情報は無理に断定しない。`※要確認` は残さず、その文・行・セルを削除する
- 公式サイトURLボタンの href は、裏取りできたURLだけにする
- スクリーンショット画像の src は変更しない
- 文章全体の言い回しを大きく書き換えず、事実修正を優先する
- 生成指示文、プレースホルダー、説明コメントを本文に残さない
"""


def fetch_visible_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    }
    response = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    soup = BeautifulSoup(response.text, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    return text[:MAX_SOURCE_CHARS]


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


def serialize_html_fragment(soup: BeautifulSoup) -> str:
    """BeautifulSoup全体ではなく、本文断片だけを書き戻す。"""
    if soup.body is not None:
        return soup.body.decode_contents().strip()
    return str(soup).strip()


def extract_factcheck_targets(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")

    list_box_names = []
    for anchor in soup.select(".clinic-index-box a"):
        name = anchor.get_text(" ", strip=True)
        if name:
            list_box_names.append(name)
    if list_box_names:
        return list(dict.fromkeys(list_box_names))

    h2 = soup.find("h2")
    if h2 is None:
        return []

    names = []
    node = h2.find_next_sibling()
    while node:
        if getattr(node, "name", None) == "h2":
            break
        if getattr(node, "name", None) == "h3":
            name = node.get_text(" ", strip=True)
            if name:
                names.append(name)
        node = node.find_next_sibling()
    return list(dict.fromkeys(names))


def resolve_existing_url(url_map: dict[str, str], target_name: str) -> str | None:
    if url_map.get(target_name):
        return url_map[target_name]

    normalized_target = normalize_clinic_lookup_name(target_name)
    normalized_pairs = [
        (key, normalize_clinic_lookup_name(key), value)
        for key, value in url_map.items()
        if value
    ]
    for key, normalized_key, value in normalized_pairs:
        if normalized_key == normalized_target:
            return value
    for key, normalized_key, value in normalized_pairs:
        if normalized_target in normalized_key or normalized_key in normalized_target:
            return value
    return None


def load_or_discover_urls(html_path: str) -> tuple[dict[str, str], list[str]]:
    html_dir = os.path.dirname(html_path)
    keyword_slug = os.path.splitext(os.path.basename(html_path))[0].replace("_記事", "")
    urls_path = os.path.join(html_dir, f"{keyword_slug}_urls.json")
    url_map: dict[str, str] = {}
    if os.path.exists(urls_path):
        with open(urls_path, encoding="utf-8") as f:
            url_map = json.load(f)

    updated = False
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    target_names = extract_factcheck_targets(html)
    for name in target_names:
        existing_url = resolve_existing_url(url_map, name)
        if existing_url:
            url_map[name] = existing_url
            continue
        url, _ = find_official_url(name)
        if url:
            url_map[name] = url
            updated = True

    if updated:
        with open(urls_path, "w", encoding="utf-8") as f:
            json.dump(url_map, f, ensure_ascii=False, indent=2)

    return url_map, target_names


def build_sources(target_names: list[str], url_map: dict[str, str]) -> tuple[list[dict], list[str], list[str]]:
    sources = []
    errors = []
    unresolved = []
    for name in target_names:
        url = url_map.get(name)
        if not url:
            unresolved.append(name)
            continue
        try:
            visible_text = fetch_visible_text(url)
            sources.append({
                "name": name,
                "url": url,
                "source_text": visible_text,
            })
        except Exception as e:
            errors.append(f"{name}: {e}")
            unresolved.append(name)
    return sources, errors, unresolved


def extract_primary_clinic_sections(html: str) -> tuple[BeautifulSoup, list[dict]]:
    soup = BeautifulSoup(html, "lxml")
    first_h2 = soup.find("h2")
    if first_h2 is None:
        return soup, []

    sections = []
    node = first_h2.find_next_sibling()
    while node:
        tag_name = getattr(node, "name", None)
        if tag_name == "h2":
            break
        if tag_name == "h3":
            heading = node
            name = heading.get_text(" ", strip=True)
            section_nodes = []
            cursor = heading.next_sibling
            while cursor:
                cursor_name = getattr(cursor, "name", None)
                if cursor_name in {"h2", "h3"}:
                    break
                current = cursor
                cursor = cursor.next_sibling
                section_nodes.append(current)
            sections.append({
                "heading": heading,
                "name": name,
                "nodes": section_nodes,
            })
            node = cursor
            continue
        node = node.next_sibling
    return soup, sections


def section_to_html(section: dict) -> str:
    return "".join(str(node) for node in [section["heading"], *section["nodes"]]).strip()


def normalize_section_fragment(fragment: str, expected_name: str) -> str:
    fragment = strip_code_fences(fragment)
    wrapped = f'<div id="__fact_root__">{fragment}</div>'
    soup = BeautifulSoup(wrapped, "html.parser")
    root = soup.find(id="__fact_root__")
    if root is None:
        raise ValueError("セクションHTMLを解析できませんでした")

    h2 = root.find("h2")
    if h2 is not None:
        raise ValueError("セクション補正なのにH2が含まれています")

    h3_tags = root.find_all("h3")
    if len(h3_tags) != 1:
        raise ValueError("補正セクションにH3が1つだけ含まれていません")
    if h3_tags[0].get_text(" ", strip=True) != expected_name:
        raise ValueError("補正セクションのH3名が一致しません")

    return root.decode_contents().strip()


def replace_section(section: dict, section_html: str) -> None:
    heading = section["heading"]
    for node in section["nodes"]:
        node.extract()

    fragment_soup = BeautifulSoup(f'<div id="__fact_root__">{section_html}</div>', "html.parser")
    root = fragment_soup.find(id="__fact_root__")
    for child in list(root.contents):
        heading.insert_before(child.extract())
    heading.extract()


def build_section_factcheck_prompt(section_html: str, source: dict) -> str:
    return f"""以下のクリニック比較セクションだけを、公式サイト情報に照らして修正してください。

## 重要ルール
- 出力はこの1セクションのHTMLだけ
- 最初の見出し `<h3>` はそのまま維持する
- セクション外の要素を足さない
- 公式サイトにない情報は無理に補わない
- `※要確認` を残さない
- 不明な行や文は削除してよい
- 公式サイトボタンのURLを書く場合は、このURLだけを使う

## 修正対象セクション
{section_html}

## 公式サイトURL
{source["url"]}

## 公式サイト抜粋
{source["source_text"]}
"""


def fact_check_html(html_path: str) -> dict:
    with open(html_path, encoding="utf-8") as f:
        original_html = f.read()

    url_map, target_names = load_or_discover_urls(html_path)
    sources, fetch_errors, unresolved_targets = build_sources(target_names, url_map)
    if not sources:
        return {
            "status": "skipped",
            "reason": "公式サイトの取得に成功したソースがありません",
            "fetch_errors": fetch_errors,
            "target_count": len(target_names),
            "unresolved_targets": unresolved_targets,
        }

    api_key = load_api_key()
    soup, sections = extract_primary_clinic_sections(original_html)
    source_map = {source["name"]: source for source in sources}
    usage_totals: dict[str, int] = {}
    corrected_targets: list[str] = []

    for section in sections:
        source = source_map.get(section["name"])
        if source is None:
            continue
        prompt = build_section_factcheck_prompt(section_to_html(section), source)
        corrected_section, _, usage = call_claude(SYSTEM_PROMPT, prompt, api_key, max_tokens=4096)
        normalized_section = normalize_section_fragment(corrected_section, section["name"])
        replace_section(section, normalized_section)
        corrected_targets.append(section["name"])
        for key, value in usage.items():
            if isinstance(value, int):
                usage_totals[key] = usage_totals.get(key, 0) + value

    corrected_html = serialize_html_fragment(soup)

    keyword_slug = os.path.splitext(os.path.basename(html_path))[0].replace("_記事", "")
    issues = validate_article_html(corrected_html, keyword_slug)
    if issues:
        return {
            "status": "error",
            "reason": "ファクトチェック後のHTMLが構造検証に失敗しました",
            "issues": issues,
            "fetch_errors": fetch_errors,
        }

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(corrected_html)

    report = {
        "status": "ok",
        "checked_at": datetime.now().isoformat(),
        "target_count": len(target_names),
        "source_count": len(sources),
        "resolved_targets": [source["name"] for source in sources],
        "corrected_targets": corrected_targets,
        "unresolved_targets": unresolved_targets,
        "fetch_errors": fetch_errors,
        "usage": usage_totals,
    }
    report_path = os.path.join(
        os.path.dirname(html_path),
        os.path.splitext(os.path.basename(html_path))[0].replace("_記事", "") + "_factcheck_report.json",
    )
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="公式サイトベースのファクトチェック")
    parser.add_argument("--html", required=True, help="記事HTMLファイル")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    result = fact_check_html(args.html)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") not in {"ok", "skipped"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
