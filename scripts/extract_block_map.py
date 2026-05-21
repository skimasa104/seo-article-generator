#!/usr/local/bin/python3.12
"""
1位記事の生HTMLから全ブロック (h2/h3/h4/p/ul/ol/table/blockquote/img/section) を
順番通り機械抽出してブロック列マップを生成する。

ARTICLE_RULES.md §1-6-A の「ブロック列マップ」を漏れなく作るための機械化スクリプト。
WebFetch の要約に頼らず、生HTMLから DOM 走査で構造を抽出する。

出力: 構造マップ (Markdown)。原文テキストは含まない。
- 段落の文字数だけは記録 (字数比較トレースのため)
- 見出し・ラベル・テーブルキャプションのテキストは記録 (どこのブロックか同定するため)
- 段落本文の全文転載はしない

使い方:
  python3 scripts/extract_block_map.py --url https://example.com/article
  python3 scripts/extract_block_map.py --url URL --out path/to/output.md

依存: requests, beautifulsoup4
"""

import argparse
import re
import sys
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


# 抽出対象のメインコンテンツ判定: <article>, <main>, または id=content/entry-content など
MAIN_CONTENT_SELECTORS = [
    "article",
    "main",
    "#content",
    ".entry-content",
    ".post-content",
    ".article-body",
    ".article-content",
    ".blog-post",
    ".blog-content",
]

# 除外するブロック (ナビ・サイドバー・フッター・関連記事など)
EXCLUDE_SELECTORS = [
    "nav",
    "footer",
    "header",
    ".sidebar",
    ".widget",
    ".related",
    ".breadcrumb",
    ".comment",
    ".share",
    ".author-box",
    ".pagination",
    "aside",
]


def fetch_html(url: str) -> str:
    """1位記事のHTMLを取得。User-Agent を付けてブロック回避。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding == "ISO-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def find_main_content(soup: BeautifulSoup) -> Tag:
    """記事本体のコンテナを探す。見つからなければ body 全体を返す。"""
    for sel in MAIN_CONTENT_SELECTORS:
        node = soup.select_one(sel)
        if node:
            return node
    return soup.body or soup


def remove_excluded(container: Tag) -> None:
    """ナビ・サイドバーなど不要なブロックを除去。"""
    for sel in EXCLUDE_SELECTORS:
        for node in container.select(sel):
            node.decompose()


def count_chars(text: str) -> int:
    """空白除去後の文字数を数える。"""
    return len(re.sub(r"\s+", "", text))


def short_label(text: str, max_chars: int = 30) -> str:
    """テキストを短いラベル化 (見出しやキャプションの識別用)。原文転載は禁止なので30字までに切る。"""
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) > max_chars:
        return clean[:max_chars] + "…"
    return clean


def table_summary(tbl: Tag) -> str:
    """テーブルのカラム名と行数を抽出。原文セルの内容は出さず構造だけ。"""
    # thead から列名を取る
    columns: list[str] = []
    thead = tbl.find("thead")
    if thead:
        for th in thead.find_all("th"):
            columns.append(short_label(th.get_text(strip=True), 20))
    else:
        # thead がない場合は最初の tr の th を見る
        first_tr = tbl.find("tr")
        if first_tr:
            ths = first_tr.find_all("th")
            if ths:
                columns = [short_label(th.get_text(strip=True), 20) for th in ths]
    # 行数: tbody の tr 数、なければ全 tr 数 - 1 (ヘッダ行)
    tbody = tbl.find("tbody")
    if tbody:
        rows = len(tbody.find_all("tr", recursive=False))
    else:
        all_trs = tbl.find_all("tr")
        rows = max(0, len(all_trs) - (1 if columns else 0))
    col_str = " / ".join(columns) if columns else "(列名不明)"
    return f"TBL: カラム[{col_str}] / {rows}行"


def list_summary(lst: Tag) -> str:
    """リストの種別と項目数。"""
    tag = "OL" if lst.name == "ol" else "UL"
    items = lst.find_all("li", recursive=False)
    return f"{tag}×{len(items)}項目"


def heading_summary(h: Tag) -> str:
    """見出しの種別とテキスト (30字まで)。"""
    text = short_label(h.get_text(strip=True), 50)
    return f"[{h.name.upper()}] {text}"


def quote_summary(q: Tag) -> str:
    """引用ブロックの簡易ラベル。出典文字列を探す。"""
    text = q.get_text(strip=True)
    # 「出典」「引用元」などのキーワード周辺を探す
    m = re.search(r"出典[:：]?\s*([^\s。、]+)", text)
    if m:
        return f"QUOTE: 出典={short_label(m.group(1), 30)}"
    m = re.search(r"参照[:：]?\s*([^\s。、]+)", text)
    if m:
        return f"QUOTE: 参照={short_label(m.group(1), 30)}"
    chars = count_chars(text)
    return f"QUOTE: ({chars}字)"


def img_summary(img: Tag) -> str:
    """画像の alt またはファイル名で識別。"""
    alt = img.get("alt", "").strip()
    if alt:
        return f"IMG: alt='{short_label(alt, 30)}'"
    src = img.get("src", "")
    filename = src.split("/")[-1] if src else ""
    return f"IMG: file={short_label(filename, 30)}"


def is_inside(node: Tag, ancestor_names: set[str]) -> bool:
    """ノードが特定のタグの内側にいるか。"""
    for parent in node.parents:
        if parent.name in ancestor_names:
            return True
    return False


def walk_blocks(container: Tag) -> list[dict]:
    """
    container 配下の全ブロックを順番通り走査して、構造リストを返す。
    各要素は {type, summary, depth} の dict。
    """
    blocks: list[dict] = []

    # まず段落の連続をまとめるためのバッファ
    p_buffer: list[Tag] = []

    def flush_paragraphs():
        nonlocal p_buffer
        if not p_buffer:
            return
        total_chars = sum(count_chars(p.get_text()) for p in p_buffer)
        # 最初の段落の冒頭ラベル
        first_label = short_label(p_buffer[0].get_text(strip=True), 30)
        blocks.append({
            "type": "P",
            "summary": f"P×{len(p_buffer)} ({total_chars}字, 冒頭:'{first_label}')",
        })
        p_buffer = []

    # 走査対象のタグ
    target_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "blockquote", "img", "figure", "section", "div"}

    # 走査: container の直下から再帰的に深さ優先で見るが、
    # 親が table/ul/ol/blockquote の場合は内側の p/img はネストしている扱いなのでスキップする
    nested_skip_ancestors = {"table", "ul", "ol", "blockquote"}

    def visit(node):
        nonlocal p_buffer
        if isinstance(node, NavigableString):
            return
        if not isinstance(node, Tag):
            return

        name = node.name
        if name not in target_tags:
            # ターゲット外でも子を辿る (span/strong などはスキップして子へ)
            for child in node.children:
                visit(child)
            return

        # 段落バッファとの境界: 段落以外が出たら flush
        if name != "p":
            flush_paragraphs()

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            blocks.append({"type": name.upper(), "summary": heading_summary(node)})
            return

        if name == "p":
            # 段落直下に img だけが入っている場合は IMG として扱う
            imgs = node.find_all("img", recursive=False)
            text = node.get_text(strip=True)
            if imgs and not text:
                flush_paragraphs()
                for img in imgs:
                    blocks.append({"type": "IMG", "summary": img_summary(img)})
                return
            # 通常の段落
            if count_chars(text) > 0:
                p_buffer.append(node)
            return

        if name in ("ul", "ol"):
            blocks.append({"type": name.upper(), "summary": list_summary(node)})
            return

        if name == "table":
            blocks.append({"type": "TBL", "summary": table_summary(node)})
            return

        if name == "blockquote":
            blocks.append({"type": "QUOTE", "summary": quote_summary(node)})
            return

        if name == "img":
            # 親が p や a の場合、上の p 処理で拾うので skip
            if is_inside(node, {"p", "a"}):
                return
            blocks.append({"type": "IMG", "summary": img_summary(node)})
            return

        if name == "figure":
            # figure 内の img と figcaption をまとめて1ブロック
            img = node.find("img")
            cap = node.find("figcaption")
            label_parts = []
            if img:
                label_parts.append(img_summary(img))
            if cap:
                label_parts.append(f"caption='{short_label(cap.get_text(strip=True), 30)}'")
            blocks.append({"type": "FIGURE", "summary": " / ".join(label_parts) or "FIGURE"})
            return

        if name in ("section", "div"):
            # コンテナ系は子を辿るだけ。ただし「ボックス」っぽいクラス名は記録
            cls = " ".join(node.get("class", []))
            box_keywords = ["box", "callout", "summary", "highlight", "alert", "info", "note", "matome", "conclusion"]
            if any(kw in cls.lower() for kw in box_keywords):
                # ボックスとして記録
                flush_paragraphs()
                # 内側に見出し的なものがあればラベルに
                inner_label = ""
                first_h = node.find(["h2", "h3", "h4", "h5", "h6", "strong"])
                if first_h:
                    inner_label = short_label(first_h.get_text(strip=True), 30)
                blocks.append({
                    "type": "BOX",
                    "summary": f"BOX class='{cls}'" + (f" label='{inner_label}'" if inner_label else "")
                })
                # ボックス内は更に走査しない (内部要素を別ブロックにすると重複)
                return
            # それ以外は子を辿る
            for child in node.children:
                visit(child)
            return

    for child in container.children:
        visit(child)

    # 最後の段落バッファを flush
    flush_paragraphs()

    return blocks


def format_block_map(blocks: list[dict], url: str) -> str:
    """ブロックリストを Markdown 形式で整形。"""
    lines = []
    lines.append(f"# ブロック列マップ (機械抽出)")
    lines.append("")
    lines.append(f"**取得元**: {url}")
    lines.append(f"**抽出時のブロック総数**: {len(blocks)}")
    lines.append("")
    lines.append("> 本マップは `scripts/extract_block_map.py` で生HTMLから機械抽出した結果。")
    lines.append("> ARTICLE_RULES.md §1-6-A の必須要素として、タグ構成設計の前にこのマップを生成する。")
    lines.append("> 段落本文の転載はせず、構造情報 (見出しテキスト・カラム名・行数・項目数・文字数) のみ含む。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("| # | 種別 | 詳細 |")
    lines.append("|---|---|---|")
    for i, b in enumerate(blocks, 1):
        # マークダウンテーブル内のパイプはエスケープ
        summary = b["summary"].replace("|", "\\|")
        lines.append(f"| {i} | {b['type']} | {summary} |")

    # サマリー: H2/H3/H4 などの数
    type_counts: dict[str, int] = {}
    for b in blocks:
        type_counts[b["type"]] = type_counts.get(b["type"], 0) + 1
    lines.append("")
    lines.append("## ブロック種別サマリー")
    lines.append("")
    for t in sorted(type_counts.keys()):
        lines.append(f"- **{t}**: {type_counts[t]}個")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="1位記事のURL")
    ap.add_argument("--out", help="出力先パス (省略時は標準出力)")
    args = ap.parse_args()

    print(f"Fetching: {args.url}", file=sys.stderr)
    html = fetch_html(args.url)
    print(f"  HTML size: {len(html):,} chars", file=sys.stderr)

    soup = BeautifulSoup(html, "html.parser")
    container = find_main_content(soup)
    remove_excluded(container)
    print(f"  Container: <{container.name}>", file=sys.stderr)

    blocks = walk_blocks(container)
    print(f"  Extracted blocks: {len(blocks)}", file=sys.stderr)

    out_text = format_block_map(blocks, args.url)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_text)
        print(f"  Written: {args.out}", file=sys.stderr)
    else:
        print(out_text)


if __name__ == "__main__":
    main()
