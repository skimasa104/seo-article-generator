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
