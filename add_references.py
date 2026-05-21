#!/usr/local/bin/python3.12
"""
記事HTMLに参考文献・公的情報を自動付与し、参照元の妥当性を二重チェックする。

二重チェック:
1. ドメイン/タイトル/パスに基づく機械判定で参照候補を絞る
2. Claude に参照元として適切かを再審査させる
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from env_utils import load_project_env
from generate_article import call_claude, load_api_key
from search_keyword import canonicalize_result_url, search_google

load_project_env()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_TIMEOUT = 20
MAX_SOURCE_CHARS = 2500
MIN_REFERENCE_LINKS = 2
MAX_TARGETED_RESULTS = 5
MAX_PROMPT_SOURCES = 8

TARGETED_QUERIES = [
    "{keyword} PMDA",
    "{keyword} ガイドライン",
    "{keyword} 添付文書",
    "{keyword} 厚生労働省",
    "{keyword} 公式",
    "{keyword} PubMed",
]

PUBLIC_DOMAIN_PATTERNS = (
    "pmda.go.jp",
    "mhlw.go.jp",
    "e-gov.go.jp",
    "gov-online.go.jp",
    "amed.go.jp",
    "ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov",
    "dailymed.nlm.nih.gov",
    "jstage.jst.go.jp",
)

LIKELY_BAD_TITLE_TOKENS = (
    "ランキング",
    "比較",
    "おすすめ",
    "口コミ",
    "評判",
    "まとめ",
)

LIKELY_BAD_PATH_TOKENS = (
    "/ranking",
    "/hikaku",
    "/compare",
    "/matome",
)

OFFICIAL_TITLE_TOKENS = (
    "公式",
    "公式サイト",
    "使用上の注意",
    "製品情報",
    "添付文書",
    "インタビューフォーム",
    "医薬品等安全性情報",
)

REFERENCE_INSERTION_SYSTEM = """あなたは医療SEO記事の編集者です。
既存HTMLに、必要最小限の参考文献・公的情報を追加してください。

厳守ルール:
- 出力はHTMLのみ
- H2/H3の数、順序、id、画像src、表の列構造を変えない
- 参考文献を足すために本文全体を書き換えない
- 根拠が必要な箇所だけに、簡潔な参考リンクを入れる
- 参考リンクの形式は `<p><small>参考：...</small></p>` を基本とする
- 記事末尾には `H2` を作らず、`<p><strong>参考文献・公的情報</strong></p>` の直後に `<ul>` で参照先一覧を置く
- 参考リンクは必ず `<a href="..." target="_blank" rel="noopener noreferrer">...</a>` にする
- 比較サイト、ランキング、まとめ記事、口コミ記事を参考文献として使わない
- 国内の公的情報・学会・公式情報を優先し、海外文献は補足的にのみ使う
- 参考文献を本文の全段落には入れない
- 末尾の注意書きや既存の結論は壊さない
- 参照候補として渡したURL以外は使わない
"""

REFERENCE_REVIEW_SYSTEM = """あなたは医療SEO記事の参照元レビュアーです。
記事に使われた参考文献URLが、参照元として適切かを厳格に判定してください。

判定基準:
- 公的機関、学会、査読DB、公式製品情報を優先
- 比較記事、ランキング記事、まとめ記事、口コミ記事は不適切
- 海外文献は、日本での承認状況や国内制度の結論には使えない。補足用途なら可
- 公式サイトでも、広告色の強い比較LPや一般コラムは不適切

必ずJSONのみを返すこと。
"""


def derive_keyword_and_slug(html_path: str) -> tuple[str, str, str]:
    html_dir = os.path.dirname(html_path)
    basename = os.path.basename(html_path)
    slug = os.path.splitext(basename)[0].replace("_記事", "")
    search_results_path = os.path.join(html_dir, f"{slug}_search_results.json")
    keyword = slug.replace("_", " ")
    if os.path.exists(search_results_path):
        try:
            with open(search_results_path, encoding="utf-8") as f:
                data = json.load(f)
            keyword = (data.get("keyword") or keyword).strip()
        except Exception:
            pass
    return keyword, slug, search_results_path


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


def load_api_key_optional() -> str | None:
    try:
        return load_api_key()
    except SystemExit:
        return None


def extract_json_block(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSONが見つかりません")
    return json.loads(text[start:end + 1])


def load_existing_search_results(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("filtered") or []) + list(data.get("all_results") or [])


def hostname_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def path_of(url: str) -> str:
    try:
        return urlparse(url).path.lower()
    except Exception:
        return ""


def classify_candidate(item: dict[str, Any]) -> dict[str, Any]:
    url = canonicalize_result_url(item.get("url", ""))
    hostname = hostname_of(url)
    path = path_of(url)
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    combined = f"{title} {snippet}"

    if not url or not hostname:
        return {
            **item,
            "url": url,
            "approved": False,
            "score": -999,
            "source_type": "invalid",
            "reason": "URL不正",
        }

    if any(token in combined for token in LIKELY_BAD_TITLE_TOKENS) or any(token in path for token in LIKELY_BAD_PATH_TOKENS):
        return {
            **item,
            "url": url,
            "approved": False,
            "score": -50,
            "source_type": "media",
            "reason": "比較/ランキング/まとめ記事寄り",
        }

    if any(hostname == pattern or hostname.endswith(f".{pattern}") for pattern in PUBLIC_DOMAIN_PATTERNS):
        source_type = "public"
        score = 100
        if "pubmed" in hostname or "ncbi.nlm.nih.gov" in hostname or "jstage" in hostname:
            source_type = "academic"
            score = 92
        elif "dailymed" in hostname:
            source_type = "official_label"
            score = 88
        return {
            **item,
            "url": url,
            "approved": True,
            "score": score,
            "source_type": source_type,
            "reason": "公的情報/学術DB",
        }

    if hostname.endswith(".go.jp"):
        return {
            **item,
            "url": url,
            "approved": True,
            "score": 95,
            "source_type": "public",
            "reason": "政府系ドメイン",
        }

    if hostname.endswith(".or.jp") and any(token in combined for token in ("学会", "ガイドライン", "指針", "診療")):
        return {
            **item,
            "url": url,
            "approved": True,
            "score": 90,
            "source_type": "guideline",
            "reason": "学会/ガイドライン",
        }

    has_official_signal = any(token in combined for token in OFFICIAL_TITLE_TOKENS)
    bad_editorial_path = any(token in path for token in ("/column", "/blog", "/media", "/article", "/news"))
    if has_official_signal:
        return {
            **item,
            "url": url,
            "approved": True,
            "score": 76 if not bad_editorial_path else 60,
            "source_type": "official",
            "reason": "公式情報シグナルあり",
        }

    if hostname.endswith((".co.jp", ".jp", ".clinic", ".com")) and not bad_editorial_path and title and " - " in title:
        return {
            **item,
            "url": url,
            "approved": True,
            "score": 68,
            "source_type": "official",
            "reason": "公式サイト候補",
        }

    return {
        **item,
        "url": url,
        "approved": False,
        "score": 0,
        "source_type": "other",
        "reason": "参照元として弱い",
    }


def collect_candidates(keyword: str, search_results_path: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_items(items: list[dict[str, Any]], source_label: str) -> None:
        for raw in items:
            url = canonicalize_result_url(raw.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            candidates.append({
                "title": raw.get("title", ""),
                "url": url,
                "snippet": raw.get("snippet", ""),
                "origin": source_label,
            })

    add_items(load_existing_search_results(search_results_path), "search_results")

    serper_api_key = os.environ.get("SERPER_API_KEY")
    if serper_api_key:
        for template in TARGETED_QUERIES:
            query = template.format(keyword=keyword)
            try:
                add_items(search_google(query, serper_api_key, max_results=MAX_TARGETED_RESULTS), f"targeted:{query}")
            except Exception:
                continue

    classified = [classify_candidate(item) for item in candidates]
    classified.sort(key=lambda item: (-item["score"], item["url"]))

    high_tier = [item for item in classified if item["approved"] and item["score"] >= 70]
    low_tier = [item for item in classified if item["approved"] and 50 <= item["score"] < 70]
    selected = high_tier[:MAX_PROMPT_SOURCES]
    if len(selected) < MAX_PROMPT_SOURCES:
        selected.extend(low_tier[:MAX_PROMPT_SOURCES - len(selected)])
    return selected


def attach_source_text(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    enriched: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            source_text = fetch_visible_text(candidate["url"])
        except Exception as e:
            errors.append({"url": candidate["url"], "error": str(e)})
            continue
        enriched.append({
            **candidate,
            "source_text": source_text,
        })
    return enriched, errors


def ensure_minimum_sources(
    candidates: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(sources) >= MIN_REFERENCE_LINKS:
        return sources

    selected = list(sources)
    seen_urls = {source["url"] for source in selected}
    for candidate in candidates:
        if candidate["url"] in seen_urls:
            continue
        selected.append({
            **candidate,
            "source_text": candidate.get("snippet") or candidate.get("title") or candidate["url"],
        })
        seen_urls.add(candidate["url"])
        if len(selected) >= max(MIN_REFERENCE_LINKS, 4):
            break
    return selected


def remove_existing_reference_blocks(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for p in list(soup.find_all("p")):
        small = p.find("small")
        if small and small.get_text(" ", strip=True).startswith("参考："):
            p.decompose()
            continue

        strong = p.find("strong")
        if strong and strong.get_text(" ", strip=True) == "参考文献・公的情報":
            next_node = p.find_next_sibling()
            p.decompose()
            if next_node and getattr(next_node, "name", None) == "ul":
                next_node.decompose()

    if soup.body is not None:
        return soup.body.decode_contents().strip()
    return str(soup).strip()


def build_insertion_prompt(keyword: str, html: str, sources: list[dict[str, Any]]) -> str:
    compact_sources = []
    for source in sources:
        compact_sources.append({
            "title": source["title"],
            "url": source["url"],
            "source_type": source["source_type"],
            "reason": source["reason"],
            "snippet": source["snippet"],
            "excerpt": source["source_text"],
        })
    return f"""以下の記事HTMLに、必要最小限の参考文献を追加してください。

## キーワード
{keyword}

## 使ってよい参照候補
```json
{json.dumps(compact_sources, ensure_ascii=False, indent=2)}
```

## 現在の記事HTML
{html}
"""


def insert_references(html: str, keyword: str, sources: list[dict[str, Any]], api_key: str) -> str:
    prompt = build_insertion_prompt(keyword, html, sources)
    content, _, _ = call_claude(REFERENCE_INSERTION_SYSTEM, prompt, api_key, max_tokens=8192)
    return strip_code_fences(content)


def repair_references(html: str, keyword: str, sources: list[dict[str, Any]], issue: str, api_key: str) -> str:
    prompt = (
        build_insertion_prompt(keyword, html, sources)
        + "\n\n## 前回出力の修正指示\n"
        + f"- 前回の出力には次の不備がありました: {issue}\n"
        + "- この不備を解消し、本文中の参考リンクと末尾の参考文献一覧を必ず入れてください。\n"
        + "- 参考文献見出しは `<p><strong>参考文献・公的情報</strong></p>` を使ってください。\n"
    )
    content, _, _ = call_claude(REFERENCE_INSERTION_SYSTEM, prompt, api_key, max_tokens=8192)
    return strip_code_fences(content)


def extract_reference_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []

    for p in soup.find_all("p"):
        small = p.find("small")
        if not small or not small.get_text(" ", strip=True).startswith("参考："):
            continue
        for a in p.find_all("a", href=True):
            urls.append(canonicalize_result_url(a["href"]))

    for p in soup.find_all("p"):
        strong = p.find("strong")
        if not strong or strong.get_text(" ", strip=True) != "参考文献・公的情報":
            continue
        ul = p.find_next_sibling("ul")
        if not ul:
            continue
        for a in ul.find_all("a", href=True):
            urls.append(canonicalize_result_url(a["href"]))

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def enforce_reference_link_attributes(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    def set_attrs(anchor) -> None:
        anchor["target"] = "_blank"
        anchor["rel"] = ["noopener", "noreferrer"]

    for p in soup.find_all("p"):
        small = p.find("small")
        if small and small.get_text(" ", strip=True).startswith("参考："):
            for a in p.find_all("a", href=True):
                set_attrs(a)

        strong = p.find("strong")
        if strong and strong.get_text(" ", strip=True) == "参考文献・公的情報":
            ul = p.find_next_sibling("ul")
            if ul:
                for a in ul.find_all("a", href=True):
                    set_attrs(a)

    if soup.body is not None:
        return soup.body.decode_contents().strip()
    return str(soup).strip()


def build_reference_link_tag(soup: BeautifulSoup, source: dict[str, Any]):
    anchor = soup.new_tag("a", href=source["url"])
    anchor["target"] = "_blank"
    anchor["rel"] = ["noopener", "noreferrer"]
    anchor.string = source["title"] or source["url"]
    return anchor


def ensure_reference_blocks(html: str, sources: list[dict[str, Any]]) -> str:
    soup = BeautifulSoup(html, "html.parser")

    heading_p = None
    for p in soup.find_all("p"):
        strong = p.find("strong")
        if strong and strong.get_text(" ", strip=True) == "参考文献・公的情報":
            heading_p = p
            break

    if heading_p is None:
        heading_p = soup.new_tag("p")
        strong = soup.new_tag("strong")
        strong.string = "参考文献・公的情報"
        heading_p.append(strong)

        last_small_note = None
        for p in soup.find_all("p"):
            small = p.find("small")
            if small:
                last_small_note = p
        if last_small_note is not None:
            last_small_note.insert_before(heading_p)
        elif soup.find_all():
            soup.find_all()[-1].insert_after(heading_p)
        else:
            soup.append(heading_p)

    reference_list = heading_p.find_next_sibling("ul")
    if reference_list is None:
        reference_list = soup.new_tag("ul")
        heading_p.insert_after(reference_list)

    if not reference_list.find_all("a", href=True):
        for source in sources[:max(MIN_REFERENCE_LINKS, 4)]:
            li = soup.new_tag("li")
            li.append(build_reference_link_tag(soup, source))
            reference_list.append(li)

    has_inline_reference = False
    for p in soup.find_all("p"):
        small = p.find("small")
        if small and small.get_text(" ", strip=True).startswith("参考："):
            has_inline_reference = True
            break

    if not has_inline_reference:
        inline_p = soup.new_tag("p")
        small = soup.new_tag("small")
        small.append("参考：")
        inline_sources = sources[:max(MIN_REFERENCE_LINKS, 2)]
        for index, source in enumerate(inline_sources):
            if index > 0:
                small.append("、")
            small.append(build_reference_link_tag(soup, source))
        inline_p.append(small)
        heading_p.insert_before(inline_p)

    if soup.body is not None:
        return soup.body.decode_contents().strip()
    return str(soup).strip()


def build_deterministic_reference_html(html: str, sources: list[dict[str, Any]]) -> str:
    return enforce_reference_link_attributes(ensure_reference_blocks(html, sources))


def extract_heading_signature(html: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    h2s = [tag.get_text(" ", strip=True) for tag in soup.find_all("h2")]
    h3s = [tag.get_text(" ", strip=True) for tag in soup.find_all("h3")]
    return h2s, h3s


def preserve_heading_structure(original_html: str, candidate_html: str, sources: list[dict[str, Any]]) -> str:
    if extract_heading_signature(original_html) == extract_heading_signature(candidate_html):
        return candidate_html
    return ensure_reference_blocks(original_html, sources)


def select_review_approved_sources(
    sources: list[dict[str, Any]],
    review: dict[str, Any],
) -> list[dict[str, Any]]:
    approved_urls = set(review.get("approved_urls") or [])
    rejected_urls = {item.get("url") for item in (review.get("rejected") or []) if item.get("url")}

    if approved_urls:
        filtered = [source for source in sources if source["url"] in approved_urls]
        if filtered:
            return filtered

    if rejected_urls:
        filtered = [source for source in sources if source["url"] not in rejected_urls]
        if filtered:
            return filtered

    return sources


def validate_reference_output(html: str, allowed_urls: set[str] | None = None) -> None:
    soup = BeautifulSoup(html, "html.parser")

    heading = None
    for p in soup.find_all("p"):
        strong = p.find("strong")
        if strong and strong.get_text(" ", strip=True) == "参考文献・公的情報":
            heading = p
            break

    if heading is None:
        raise ValueError("参考文献・公的情報の見出しがありません")
    if soup.find("h2", string=re.compile("参考文献|公的情報")):
        raise ValueError("参考文献・公的情報がH2になっています")

    reference_list = heading.find_next_sibling("ul")
    if reference_list is None:
        raise ValueError("参考文献一覧のulがありません")

    links = reference_list.find_all("a", href=True)
    if len(links) < MIN_REFERENCE_LINKS:
        raise ValueError("参考文献リンク数が不足しています")

    for a in links:
        if a.get("target") != "_blank":
            raise ValueError("参考文献リンクが別タブ設定になっていません")
        rel_values = a.get("rel") or []
        rel_joined = " ".join(rel_values) if isinstance(rel_values, list) else str(rel_values)
        if "noopener" not in rel_joined or "noreferrer" not in rel_joined:
            raise ValueError("参考文献リンクのrel属性が不足しています")
        if allowed_urls is not None and canonicalize_result_url(a["href"]) not in allowed_urls:
            raise ValueError(f"許可されていない参考URLが含まれています: {a['href']}")

    inline_refs = 0
    for p in soup.find_all("p"):
        small = p.find("small")
        if not small or not small.get_text(" ", strip=True).startswith("参考："):
            continue
        inline_refs += 1
        for a in p.find_all("a", href=True):
            if a.get("target") != "_blank":
                raise ValueError("本文中の参考リンクが別タブ設定になっていません")
            rel_values = a.get("rel") or []
            rel_joined = " ".join(rel_values) if isinstance(rel_values, list) else str(rel_values)
            if "noopener" not in rel_joined or "noreferrer" not in rel_joined:
                raise ValueError("本文中の参考リンクのrel属性が不足しています")
            if allowed_urls is not None and canonicalize_result_url(a["href"]) not in allowed_urls:
                raise ValueError(f"許可されていない本文参考URLが含まれています: {a['href']}")

    if inline_refs == 0:
        raise ValueError("本文中の参考リンクが1件もありません")


def build_review_prompt(keyword: str, html: str, sources: list[dict[str, Any]], used_urls: list[str]) -> str:
    used_sources = []
    by_url = {source["url"]: source for source in sources}
    for url in used_urls:
        source = by_url.get(url)
        if not source:
            continue
        used_sources.append({
            "title": source["title"],
            "url": source["url"],
            "source_type": source["source_type"],
            "reason": source["reason"],
            "snippet": source["snippet"],
        })

    return f"""以下の記事に使われている参考文献URLが、参照元として適切かレビューしてください。

## キーワード
{keyword}

## 記事HTML
{html}

## 使用された参考URL候補
```json
{json.dumps(used_sources, ensure_ascii=False, indent=2)}
```

以下のJSONのみを返してください:
{{
  "overall_ok": true,
  "approved_urls": ["..."],
  "rejected": [{{"url": "...", "reason": "..."}}],
  "notes": ["..."]
}}
"""


def review_reference_suitability(keyword: str, html: str, sources: list[dict[str, Any]], used_urls: list[str], api_key: str) -> dict[str, Any]:
    prompt = build_review_prompt(keyword, html, sources, used_urls)
    content, _, _ = call_claude(REFERENCE_REVIEW_SYSTEM, prompt, api_key, max_tokens=3000)
    return extract_json_block(content)


def report_path_for(html_path: str, slug: str) -> str:
    return os.path.join(os.path.dirname(html_path), f"{slug}_reference_report.json")


def write_reference_report(html_path: str, slug: str, report: dict[str, Any]) -> None:
    with open(report_path_for(html_path, slug), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="記事HTMLに参考文献・公的情報を追加")
    parser.add_argument("--html", required=True, help="対象の記事HTML")
    parser.add_argument("--dry-run", action="store_true", help="HTMLを書き戻さずに検証のみ行う")
    args = parser.parse_args()

    html_path = args.html
    if not os.path.exists(html_path):
        print(f"Error: HTMLが見つかりません: {html_path}")
        sys.exit(1)

    keyword, slug, search_results_path = derive_keyword_and_slug(html_path)
    api_key = load_api_key_optional()

    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    cleaned_html = remove_existing_reference_blocks(html)

    candidates = collect_candidates(keyword, search_results_path)
    if not candidates:
        report = {
            "status": "skipped",
            "reason": "no_reference_candidates",
            "checked_at": datetime.now().isoformat(),
            "keyword": keyword,
            "html_path": html_path,
            "candidate_count": 0,
            "source_count": 0,
            "used_reference_urls": [],
            "fetch_errors": [],
            "review": {
                "overall_ok": True,
                "approved_urls": [],
                "rejected": [],
                "notes": ["参考文献候補を収集できなかったため、参考文献なしで継続"],
            },
            "generation_mode": "skipped",
        }
        write_reference_report(html_path, slug, report)
        print(json.dumps({
            "status": "skipped",
            "reason": "no_reference_candidates",
            "keyword": keyword,
            "source_count": 0,
            "used_reference_urls": [],
        }, ensure_ascii=False, indent=2))
        return

    sources, fetch_errors = attach_source_text(candidates)
    sources = ensure_minimum_sources(candidates, sources)
    if len(sources) < MIN_REFERENCE_LINKS:
        report = {
            "status": "skipped",
            "reason": "insufficient_reference_sources",
            "checked_at": datetime.now().isoformat(),
            "keyword": keyword,
            "html_path": html_path,
            "candidate_count": len(candidates),
            "source_count": len(sources),
            "used_reference_urls": [],
            "fetch_errors": fetch_errors,
            "review": {
                "overall_ok": True,
                "approved_urls": [],
                "rejected": [],
                "notes": ["利用可能な参考文献候補が不足したため、参考文献なしで継続"],
            },
            "generation_mode": "skipped",
        }
        write_reference_report(html_path, slug, report)
        print(json.dumps({
            "status": "skipped",
            "reason": "insufficient_reference_sources",
            "keyword": keyword,
            "source_count": len(sources),
            "used_reference_urls": [],
        }, ensure_ascii=False, indent=2))
        return

    allowed_urls = {source["url"] for source in sources}
    updated_html = ""
    generation_mode = "deterministic"
    last_issue = ""

    if api_key:
        try:
            updated_html = preserve_heading_structure(
                cleaned_html,
                enforce_reference_link_attributes(
                    insert_references(cleaned_html, keyword, sources, api_key)
                ),
                sources,
            )
            generation_mode = "claude"
            for _ in range(2):
                try:
                    validate_reference_output(updated_html, allowed_urls=allowed_urls)
                    break
                except Exception as e:
                    last_issue = str(e)
                    updated_html = preserve_heading_structure(
                        cleaned_html,
                        enforce_reference_link_attributes(
                            repair_references(updated_html, keyword, sources, last_issue, api_key)
                        ),
                        sources,
                    )
            else:
                raise ValueError(f"参考文献整形に失敗しました: {last_issue}")
        except BaseException as e:
            print(f"Warning: LLMによる参考文献挿入に失敗したため、機械フォールバックに切り替えます: {e}")
            updated_html = build_deterministic_reference_html(cleaned_html, sources)
            generation_mode = "fallback"
    else:
        print("Warning: ANTHROPIC_API_KEY がないため、参考文献は機械フォールバックで挿入します。")
        updated_html = build_deterministic_reference_html(cleaned_html, sources)

    validate_reference_output(updated_html, allowed_urls=allowed_urls)

    used_urls = extract_reference_urls(updated_html)
    if len(used_urls) < MIN_REFERENCE_LINKS:
        report = {
            "status": "skipped",
            "reason": "insufficient_reference_urls",
            "checked_at": datetime.now().isoformat(),
            "keyword": keyword,
            "html_path": html_path,
            "candidate_count": len(candidates),
            "source_count": len(sources),
            "used_reference_urls": used_urls,
            "fetch_errors": fetch_errors,
            "review": {
                "overall_ok": True,
                "approved_urls": [],
                "rejected": [],
                "notes": ["参考文献URLが不足したため、参考文献なしで継続"],
            },
            "generation_mode": "skipped",
        }
        write_reference_report(html_path, slug, report)
        print(json.dumps({
            "status": "skipped",
            "reason": "insufficient_reference_urls",
            "keyword": keyword,
            "source_count": len(sources),
            "used_reference_urls": used_urls,
        }, ensure_ascii=False, indent=2))
        return

    review: dict[str, Any] = {
        "overall_ok": True,
        "approved_urls": used_urls,
        "rejected": [],
        "notes": [],
    }
    if api_key:
        try:
            review = review_reference_suitability(keyword, updated_html, sources, used_urls, api_key)
            rejected = review.get("rejected") or []
            approved_urls = set(review.get("approved_urls") or [])

            if not review.get("overall_ok", False):
                note = "参考文献レビューがNGだったため、機械判定済み候補で継続します"
                print(f"Warning: {note}")
                review["overall_ok"] = True
                review.setdefault("notes", []).append(note)

            if rejected or (approved_urls and any(url not in approved_urls for url in used_urls)):
                approved_sources = select_review_approved_sources(sources, review)
                if len(approved_sources) >= MIN_REFERENCE_LINKS:
                    updated_html = build_deterministic_reference_html(
                        remove_existing_reference_blocks(updated_html),
                        approved_sources,
                    )
                    validate_reference_output(
                        updated_html,
                        allowed_urls={source["url"] for source in approved_sources},
                    )
                    used_urls = extract_reference_urls(updated_html)
                else:
                    note = "レビュー結果のみでは候補が不足したため、機械判定済み候補を維持しました"
                    print(f"Warning: {note}")
                    review.setdefault("notes", []).append(note)
        except BaseException as e:
            print(f"Warning: 参考文献レビューに失敗したため、機械判定済み候補で継続します: {e}")
            review = {
                "overall_ok": True,
                "approved_urls": used_urls,
                "rejected": [],
                "notes": [f"review_skipped: {e}"],
            }
    else:
        review["notes"].append("review_skipped: API key unavailable")

    if not args.dry_run:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(updated_html)

    report = {
        "status": "ok",
        "checked_at": datetime.now().isoformat(),
        "keyword": keyword,
        "html_path": html_path,
        "candidate_count": len(candidates),
        "source_count": len(sources),
        "used_reference_urls": used_urls,
        "fetch_errors": fetch_errors,
        "review": review,
        "generation_mode": generation_mode,
    }
    write_reference_report(html_path, slug, report)

    print(json.dumps({
        "status": "ok",
        "keyword": keyword,
        "source_count": len(sources),
        "used_reference_urls": used_urls,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
