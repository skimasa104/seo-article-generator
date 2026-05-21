#!/usr/bin/env python3
"""
SEO記事作成用スクレイピングツール
Google検索上位記事のタグ構成・コンテンツブロック・レイアウトを分析して保存する
"""

import sys
import os
import json
import time
import random
import re
import requests
from bs4 import BeautifulSoup, NavigableString

from output_utils import ensure_keyword_scraped_dir

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}

# サイドバー・広告系の除外パターン（本文外のノイズ）
NOISE_SELECTORS = [
    ".sidebar", "#sidebar", ".widget", ".related-posts",
    ".recommend", ".ranking", ".ad", ".advertisement",
    '[role="complementary"]', ".breadcrumb",
]


def clean_soup(soup):
    """不要な要素を除去（iframe・imgは残す）"""
    for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                               "aside", "noscript", "svg", "button",
                               "input", "select"]):
        tag.decompose()
    for selector in NOISE_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()
    return soup


def find_main_content(soup):
    """メインコンテンツのコンテナを取得（最も深い本文コンテナを探す）"""
    for selector in [".post_content", ".post-content", ".entry-content",
                     ".article-content", ".article-body", ".content-area",
                     "article", "main", '[role="main"]', ".content",
                     "#content", "#main-content", ".post-body"]:
        candidate = soup.select_one(selector)
        if candidate and len(candidate.get_text(strip=True)) > 200:
            return candidate
    return soup.find("body")


def classify_block(element):
    """HTML要素のコンテンツブロックタイプを判定"""
    if not hasattr(element, "name") or element.name is None:
        return None

    tag = element.name
    classes = " ".join(element.get("class", []))
    text = element.get_text(strip=True)

    # 子要素のタグ種類を取得
    child_tags = set()
    for sub in element.find_all(True, recursive=True):
        child_tags.add(sub.name)

    # --- ブロックタイプ判定 ---

    # テーブル（比較表・料金表・早見表）
    if tag == "table" or tag == "figure" and "table" in child_tags:
        has_img = "img" in child_tags
        has_link = bool(element.find("a", href=True))
        if has_img and has_link:
            return "比較表（画像・リンク付き）"
        elif has_link:
            return "テーブル（リンク付き）"
        elif has_img:
            return "テーブル（画像付き）"
        return "テーブル"

    # 地図（iframe）
    if tag == "iframe":
        src = element.get("src", "")
        if "google.com/maps" in src or "maps" in src.lower():
            return "Googleマップ埋め込み"
        return "iframe埋め込み"
    if "iframe" in child_tags:
        iframe = element.find("iframe")
        if iframe:
            src = iframe.get("src", "")
            if "google.com/maps" in src or "maps" in src.lower():
                return "Googleマップ埋め込み"

    # 目次
    if "toc" in classes or "table-of-contents" in classes or "p-toc" in classes:
        return "目次"

    # アフィリエイト系ボタン・CTA
    if "jslink" in classes or "cta" in classes or "cv-box" in classes:
        return "アフィリエイトボタン"
    if "＞＞" in text or "詳しくみる" in text or "公式サイト" in text:
        if len(text) < 100:
            return "CTAリンク"

    # ボックス・キャプションボックス（一覧・特徴まとめ等）
    if "capbox" in classes or "cap_box" in classes:
        if "li" in child_tags:
            return "一覧ボックス（リスト型）"
        return "キャプションボックス"

    # 口コミ・引用
    if tag == "blockquote" or "border" in classes:
        return "引用・口コミ"

    # バルーン・吹き出し
    if "balloon" in classes or "speech" in classes or "voice" in classes:
        return "吹き出し"

    # 画像
    if tag == "figure" or tag == "img":
        if "img" in child_tags or tag == "img":
            return "画像"

    # アコーディオン（折りたたみ）
    if tag == "details" or "accordion" in classes:
        return "アコーディオン（折りたたみ）"

    # スクロールヒント
    if "scrollHint" in classes or "scroll" in classes:
        return None  # ただのUIヒント、スキップ

    # 通常のテキスト段落
    if tag == "p":
        if len(text) > 10:
            return "テキスト"
        return None

    # リスト
    if tag in ("ul", "ol"):
        if "li" in child_tags:
            return "リスト"

    # div等でテキストがある場合
    if tag == "div" and len(text) > 20:
        if "li" in child_tags:
            return "リスト型ブロック"
        return "テキストブロック"

    return None


def analyze_blocks_between_headings(heading):
    """見出しの直属コンテンツブロックを分析（次の見出しが来たら終了）"""
    blocks = []
    text_parts = []

    for sibling in heading.find_next_siblings():
        # 次の見出しタグが来たら終了（レベル問わず）
        if sibling.name and sibling.name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
            break

        block_type = classify_block(sibling)
        if block_type:
            blocks.append(block_type)

        if not hasattr(sibling, "get_text"):
            continue
        text = sibling.get_text(strip=True)
        if len(text) > 10:
            text_parts.append(text)

    return blocks, "\n".join(text_parts)


def analyze_intro_blocks(main_content):
    """導入部分（最初のH2の前）のブロック構成とテキストを分析"""
    first_h2 = main_content.find("h2")
    blocks = []
    text_parts = []

    if not first_h2:
        return blocks, ""

    for child in main_content.children:
        if child == first_h2:
            break
        if not hasattr(child, "name") or child.name is None:
            continue
        # H1はスキップ
        if child.name == "h1":
            continue

        block_type = classify_block(child)
        if block_type:
            blocks.append(block_type)

        # テキスト取得（テーブル・目次・CTAは除外）
        if block_type and block_type.startswith(("テーブル", "比較表", "目次", "CTA", "アフィ")):
            continue
        text = child.get_text(strip=True)
        if len(text) > 20 and "＞＞" not in text and "詳しくみる" not in text:
            text_parts.append(text)

    return blocks, "\n".join(text_parts)


def extract_article(url):
    """記事ページからSEO分析用データを抽出"""
    print(f"\n📄 記事を取得中: {url}")

    try:
        time.sleep(random.uniform(1, 3))
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
    except Exception as e:
        print(f"   ❌ 取得失敗: {e}")
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # titleタグ
    title_tag = ""
    t = soup.find("title")
    if t:
        title_tag = t.get_text(strip=True)

    # メタディスクリプション
    meta_desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        meta_desc = meta.get("content", "")

    # 不要要素を除去
    soup = clean_soup(soup)

    # メインコンテンツ取得
    main_content = find_main_content(soup)
    if not main_content:
        print("   ❌ コンテンツが見つかりません")
        return None

    # --- 導入部分の分析 ---
    intro_blocks, intro_text = analyze_intro_blocks(main_content)

    # --- タグ構成を取得（H2/H3/H4 + 配下ブロック分析）---
    tag_structure = []
    all_headings = main_content.find_all(["h2", "h3", "h4"])

    for h in all_headings:
        level = int(h.name[1])
        heading_text = h.get_text(strip=True)
        if not heading_text:
            continue

        blocks, content = analyze_blocks_between_headings(h)

        tag_structure.append({
            "tag": h.name,
            "level": level,
            "heading": heading_text,
            "blocks": blocks,
            "content": content,
        })

    total_chars = len(intro_text) + sum(len(s["content"]) for s in tag_structure)
    print(f"   ✅ 取得成功: {title_tag[:50]}... ({total_chars}文字, 見出し{len(tag_structure)}個)")

    return {
        "url": url,
        "title_tag": title_tag,
        "meta_description": meta_desc,
        "intro_text": intro_text,
        "intro_blocks": intro_blocks,
        "tag_structure": tag_structure,
        "total_chars": total_chars,
    }


def _normalize_for_match(text: str) -> str:
    """ゆるい文字列マッチ用に空白記号を取り除いた小文字テキストに変換"""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[\s　]", "", text)
    return text


def _keyword_tokens(keyword: str) -> list[str]:
    """キーワードを空白で分割しつつ全体も含めた検索トークンを返す"""
    if not keyword:
        return []
    parts = [p for p in re.split(r"[\s　]+", keyword) if p]
    tokens = parts.copy()
    if len(parts) > 1:
        tokens.append("".join(parts))
    seen = set()
    out = []
    for t in tokens:
        n = _normalize_for_match(t)
        if n and n not in seen:
            seen.add(n)
            out.append(t)
    return out


def _heading_contains_any_token(heading: str, tokens: list[str]) -> bool:
    n = _normalize_for_match(heading)
    if not n:
        return False
    for t in tokens:
        if _normalize_for_match(t) in n:
            return True
    return False


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def build_top_article_profile(article: dict) -> dict:
    """上位記事1本（articles[0]: スコア最上位、おおむねSERP上位）の構造プロファイルを返す。
    Claude が「1位記事に揃える」判断をするための材料として渡す。"""
    if not article:
        return {}

    intro_blocks = article.get("intro_blocks", []) or []
    intro_features = sorted(set(intro_blocks))

    tag_structure = article.get("tag_structure") or []
    h2_list = [s for s in tag_structure if s.get("tag") == "h2"]
    h3_list = [s for s in tag_structure if s.get("tag") == "h3"]

    # H2 ごとに、配下の H3 数 / そのH2セクションの本文文字数 / ブロック型を集計
    # 1位記事の「H2 単位の構造」を Claude に渡して、本記事もその構造に合わせさせる
    h2_sections: list[dict] = []
    h2_index_by_obj_id: dict[int, int] = {}
    for s in tag_structure:
        if s.get("tag") == "h2":
            h2_sections.append({
                "heading": s.get("heading", ""),
                "content_length": int(s.get("content_length", 0) or 0),
                "blocks": list(s.get("blocks", []) or []),
                "h3_headings": [],
                "h3_count": 0,
                "h3_text_lengths": [],
                "h3_table_counts": [],
            })
            h2_index_by_obj_id[id(s)] = len(h2_sections) - 1
    # 直前の H2 に H3 を紐付ける（並びはスクレイプ順）
    current_h2_idx = -1
    for s in tag_structure:
        if s.get("tag") == "h2":
            current_h2_idx = h2_index_by_obj_id.get(id(s), -1)
        elif s.get("tag") == "h3" and current_h2_idx >= 0:
            sec = h2_sections[current_h2_idx]
            blocks = s.get("blocks", []) or []
            sec["h3_headings"].append(s.get("heading", ""))
            sec["h3_count"] += 1
            sec["h3_text_lengths"].append(int(s.get("content_length", 0) or 0))
            sec["h3_table_counts"].append(
                sum(1 for b in blocks if "テーブル" in (b or "") or "比較表" in (b or ""))
            )

    # H3 全体の構造特徴（既存の集計）
    h3_table_counts: list[int] = []
    h3_text_lengths: list[int] = []
    h3_has_reviews = 0
    h3_has_official_button = 0
    h3_with_table = 0
    h3_with_map = 0

    for h3 in h3_list:
        blocks = h3.get("blocks", []) or []
        table_count = sum(1 for b in blocks if "テーブル" in (b or "") or "比較表" in (b or ""))
        h3_table_counts.append(table_count)
        if table_count > 0:
            h3_with_table += 1
        if any("口コミ" in (b or "") or "引用" in (b or "") or "吹き出し" in (b or "") for b in blocks):
            h3_has_reviews += 1
        if any("CTA" in (b or "") or "アフィリエイト" in (b or "") for b in blocks):
            h3_has_official_button += 1
        if any("マップ" in (b or "") or "iframe" in (b or "") for b in blocks):
            h3_with_map += 1
        h3_text_lengths.append(int(h3.get("content_length", 0) or 0))

    # 記事内の機能ブロック総出現回数
    feature_freq_in_article: dict[str, int] = {}
    for b in intro_blocks:
        feature_freq_in_article[b] = feature_freq_in_article.get(b, 0) + 1
    for s in (article.get("tag_structure") or []):
        for b in s.get("blocks", []) or []:
            feature_freq_in_article[b] = feature_freq_in_article.get(b, 0) + 1

    # H2 セクションの簡易ビュー（プロンプトで読みやすい形）
    h2_section_view = []
    for sec in h2_sections:
        # ブロックの種類を集約（"テーブル: 2 / リスト: 1 / 段落: 多" のような形に）
        block_summary: dict[str, int] = {}
        for b in sec["blocks"]:
            key = (b or "").strip() or "その他"
            block_summary[key] = block_summary.get(key, 0) + 1
        h2_section_view.append({
            "heading": sec["heading"],
            "content_length": sec["content_length"],
            "h3_count": sec["h3_count"],
            "h3_headings_sample": sec["h3_headings"][:3],
            "h3_text_length_avg": (sum(sec["h3_text_lengths"]) // len(sec["h3_text_lengths"])) if sec["h3_text_lengths"] else 0,
            "block_summary": block_summary,
        })

    return {
        "url": article.get("url", ""),
        "title_tag": article.get("title_tag", ""),
        "label": "上位記事（検索/フィルタ最上位 = 概ね SERP 1位)",
        "total_chars": article.get("total_chars", 0),
        "h2_count": len(h2_list),
        "h3_count": len(h3_list),
        "h2_headings": [h.get("heading", "") for h in h2_list],
        "h2_sections": h2_section_view,  # H2 単位の構造データ（H3数・本文長・ブロック種別）
        "intro_features": intro_features,
        "intro_has_index_box": any("一覧" in (b or "") or "ボックス" in (b or "") for b in intro_blocks),
        "intro_has_table": any("テーブル" in (b or "") or "比較表" in (b or "") for b in intro_blocks),
        "intro_has_toc": any("目次" in (b or "") for b in intro_blocks),
        "h3_table_count_avg": round(sum(h3_table_counts) / len(h3_table_counts), 2) if h3_table_counts else 0,
        "h3_table_count_max": max(h3_table_counts) if h3_table_counts else 0,
        "h3_with_table_pct": round(h3_with_table / len(h3_list) * 100, 1) if h3_list else 0,
        "h3_text_length_avg": round(sum(h3_text_lengths) / len(h3_text_lengths), 0) if h3_text_lengths else 0,
        "h3_text_length_median": round(_percentile(h3_text_lengths, 0.5), 0) if h3_text_lengths else 0,
        "h3_with_reviews_pct": round(h3_has_reviews / len(h3_list) * 100, 1) if h3_list else 0,
        "h3_with_official_button_pct": round(h3_has_official_button / len(h3_list) * 100, 1) if h3_list else 0,
        "h3_with_map_pct": round(h3_with_map / len(h3_list) * 100, 1) if h3_list else 0,
        "feature_freq": dict(sorted(feature_freq_in_article.items(), key=lambda x: -x[1])),
    }


def build_competitor_summary(keyword: str, articles: list[dict]) -> dict:
    """全競合記事を横断集計して、tag_structure 設計に渡す統計データを作る"""
    n = len(articles)
    tokens = _keyword_tokens(keyword)

    char_values: list[int] = []
    h2_count_values: list[int] = []
    h3_count_values: list[int] = []

    in_title = 0
    in_meta = 0
    in_h2_any = 0  # キーワードがH2のいずれかに含まれる記事数
    in_h3_any = 0
    in_intro = 0
    h2_kw_ratio_per_article: list[float] = []
    h3_kw_ratio_per_article: list[float] = []

    feature_freq: dict[str, int] = {}        # 機能ブロックが「N記事のうち何件に登場したか」
    h2_phrase_counter: dict[str, int] = {}   # H2見出しの正規化テキスト → 記事数

    for a in articles:
        char_values.append(a.get("total_chars") or 0)
        if _heading_contains_any_token(a.get("title_tag", ""), tokens):
            in_title += 1
        if _heading_contains_any_token(a.get("meta_description", ""), tokens):
            in_meta += 1
        if _heading_contains_any_token(a.get("intro_text", ""), tokens):
            in_intro += 1

        # 記事内の機能ブロック頻度（記事単位で1回カウント、重複を抑える）
        seen_features = set()
        for b in a.get("intro_blocks", []) or []:
            seen_features.add(b)
        h2_list = [s for s in a.get("tag_structure", []) if s.get("tag") == "h2"]
        h3_list = [s for s in a.get("tag_structure", []) if s.get("tag") == "h3"]
        h2_count_values.append(len(h2_list))
        h3_count_values.append(len(h3_list))

        for s in a.get("tag_structure", []) or []:
            for b in s.get("blocks", []) or []:
                seen_features.add(b)
        for ft in seen_features:
            feature_freq[ft] = feature_freq.get(ft, 0) + 1

        # H2/H3 のキーワード含有
        h2_hits = sum(1 for h in h2_list if _heading_contains_any_token(h.get("heading", ""), tokens))
        h3_hits = sum(1 for h in h3_list if _heading_contains_any_token(h.get("heading", ""), tokens))
        if h2_list:
            h2_kw_ratio_per_article.append(h2_hits / len(h2_list))
        if h3_list:
            h3_kw_ratio_per_article.append(h3_hits / len(h3_list))
        if h2_hits > 0:
            in_h2_any += 1
        if h3_hits > 0:
            in_h3_any += 1

        # 共通H2フレーズ集計（文字単位ではなく見出しテキストの正規化）
        for h in h2_list:
            key = _normalize_for_match(h.get("heading", ""))
            if not key:
                continue
            h2_phrase_counter[key] = h2_phrase_counter.get(key, 0) + 1

    def _stats(values: list[float]) -> dict:
        if not values:
            return {"avg": 0, "median": 0, "min": 0, "max": 0, "p25": 0, "p75": 0}
        return {
            "avg": round(sum(values) / len(values), 1),
            "median": round(_percentile(values, 0.5), 1),
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "p25": round(_percentile(values, 0.25), 1),
            "p75": round(_percentile(values, 0.75), 1),
        }

    # SEO的に意味の薄い汎用ブロックは除外（必ずある or ノイズになりやすい）
    GENERIC_BLOCKS = {"テキスト", "テキストブロック", "リスト", "リスト型ブロック", "画像"}

    # 必須機能（70%以上の競合に登場）— 構造的SEOシグナルになる機能だけを残す
    must_include = sorted(
        [
            ft for ft, c in feature_freq.items()
            if n > 0 and c / n >= 0.7 and ft not in GENERIC_BLOCKS
        ],
        key=lambda x: -feature_freq[x],
    )
    # 多くの競合に出ている機能（50%以上）
    common_features = sorted(
        [
            ft for ft, c in feature_freq.items()
            if n > 0 and c / n >= 0.5 and ft not in GENERIC_BLOCKS
        ],
        key=lambda x: -feature_freq[x],
    )

    # 共通H2フレーズの上位（複数記事に出現するもの）
    common_h2_topics = [
        {"phrase": phrase, "count": count}
        for phrase, count in sorted(h2_phrase_counter.items(), key=lambda x: -x[1])
        if count >= 2
    ][:15]

    return {
        "competitor_count": n,
        "keyword": keyword,
        "char_stats": _stats(char_values),
        "h2_count_stats": _stats(h2_count_values),
        "h3_count_stats": _stats(h3_count_values),
        "keyword_coverage": {
            "in_title": in_title,
            "in_meta": in_meta,
            "in_intro": in_intro,
            "articles_with_keyword_in_h2": in_h2_any,
            "articles_with_keyword_in_h3": in_h3_any,
            "h2_keyword_ratio_avg": round(sum(h2_kw_ratio_per_article)/len(h2_kw_ratio_per_article), 2) if h2_kw_ratio_per_article else 0.0,
            "h3_keyword_ratio_avg": round(sum(h3_kw_ratio_per_article)/len(h3_kw_ratio_per_article), 2) if h3_kw_ratio_per_article else 0.0,
        },
        "feature_freq": dict(sorted(feature_freq.items(), key=lambda x: -x[1])),
        "must_include_features": must_include,  # 70%以上の競合に出現
        "common_features": common_features,      # 50%以上
        "common_h2_topics": common_h2_topics,
        # 1位記事（スコア最上位）の構造プロファイル
        "top_article_profile": build_top_article_profile(articles[0]) if articles else {},
    }


def save_results(keyword, articles):
    """結果をファイルに保存"""
    output_dir = ensure_keyword_scraped_dir(keyword)

    for i, article in enumerate(articles, 1):
        filename = os.path.join(output_dir, f"article_{i}_structure.md")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# 競合記事 {i} タグ構成分析\n\n")
            f.write(f"**URL**: {article['url']}\n")
            f.write(f"**titleタグ**: {article['title_tag']}\n")
            f.write(f"**メタディスクリプション**: {article['meta_description']}\n")
            f.write(f"**総文字数**: {article['total_chars']}\n")
            f.write(f"**見出し数**: {len(article['tag_structure'])}\n\n")
            f.write(f"---\n\n")

            # 導入部分
            f.write(f"## 導入部分\n\n")
            if article["intro_blocks"]:
                f.write("**ブロック構成:**\n")
                for b in article["intro_blocks"]:
                    f.write(f"- 📦 {b}\n")
                f.write("\n")
            if article["intro_text"]:
                f.write("**テキスト:**\n")
                for line in article["intro_text"].split("\n"):
                    if line.strip():
                        f.write(f"> {line.strip()}\n")
                f.write("\n")
            if not article["intro_blocks"] and not article["intro_text"]:
                f.write("（導入文なし）\n")
            f.write(f"\n---\n\n")

            # タグ構成
            f.write(f"## タグ構成と内容\n\n")
            for item in article["tag_structure"]:
                tag = item["tag"].upper()
                indent = "  " * (item["level"] - 2)
                f.write(f"{indent}### [{tag}] {item['heading']}\n\n")

                # ブロック構成
                if item["blocks"]:
                    f.write(f"{indent}**ブロック構成:**\n")
                    for b in item["blocks"]:
                        f.write(f"{indent}- 📦 {b}\n")
                    f.write(f"\n")

                # テキスト内容
                if item["content"]:
                    f.write(f"{indent}**テキスト:**\n")
                    for line in item["content"].split("\n"):
                        if line.strip():
                            f.write(f"{indent}> {line.strip()}\n")
                    f.write(f"\n")
                elif not item["blocks"]:
                    f.write(f"{indent}> （コンテンツなし）\n\n")

        print(f"   💾 保存: {filename}")

    # サマリーJSON
    summary_file = os.path.join(output_dir, "summary.json")
    summary = {
        "keyword": keyword,
        "competitor_summary": build_competitor_summary(keyword, articles),
        "articles": [
            {
                "url": a["url"],
                "title_tag": a["title_tag"],
                "meta_description": a["meta_description"],
                "total_chars": a["total_chars"],
                "heading_count": len(a["tag_structure"]),
                "intro_blocks": a["intro_blocks"],
                "tag_structure": [
                    {
                        "tag": s["tag"],
                        "heading": s["heading"],
                        "blocks": s["blocks"],
                        "content_length": len(s["content"]),
                    }
                    for s in a["tag_structure"]
                ],
            }
            for a in articles
        ],
    }
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"   💾 サマリー保存: {summary_file}")


def main():
    print("=" * 60)
    print("SEO記事スクレイピングツール")
    print("=" * 60)

    # キーワード入力
    if len(sys.argv) >= 2:
        keyword = sys.argv[1]
    else:
        keyword = input("\n🔑 キーワードを入力: ").strip()
        if not keyword:
            print("❌ キーワードが入力されていません")
            sys.exit(1)

    print(f"キーワード: {keyword}")

    # URL入力
    if len(sys.argv) >= 3:
        urls = sys.argv[2:]
    else:
        print("\n📋 Google検索上位記事のURLを1つずつ貼ってください（空Enterで入力終了）")
        urls = []
        while True:
            url = input(f"  URL {len(urls) + 1}: ").strip()
            if not url:
                break
            if url.startswith("http"):
                urls.append(url)
            else:
                print("    ⚠️ httpから始まるURLを入力してください")

    if not urls:
        print("❌ URLが入力されていません")
        sys.exit(1)

    print(f"\n対象URL: {len(urls)} 件")

    # 各記事のタグ構成を取得
    articles = []
    for url in urls:
        article = extract_article(url)
        if article and article["total_chars"] > 500:
            articles.append(article)
        elif article:
            print(f"   ⚠️ コンテンツが少なすぎるためスキップ ({article['total_chars']}文字)")

    if not articles:
        print("\n❌ 記事の内容を取得できませんでした")
        sys.exit(1)

    # 結果を保存
    print(f"\n💾 結果を保存中...")
    save_results(keyword, articles)

    print(f"\n✅ 完了！ {len(articles)} 記事を取得しました")
    print(f"   保存先: {ensure_keyword_scraped_dir(keyword)}/")


if __name__ == "__main__":
    main()
