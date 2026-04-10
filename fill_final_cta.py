#!/usr/local/bin/python3.12
"""
記事末尾のCTAショートコードを自然な導線付きで差し込む。
"""

import argparse
import json
import os
import re
import sys


CTA_START = "<!-- final-cta:start -->"
CTA_END = "<!-- final-cta:end -->"


def load_genre(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_final_cta(keyword: str, genre: dict, html: str) -> str:
    shortcode = genre.get("shortcodes", {}).get("早見表", "").strip()
    if not shortcode:
        raise ValueError("ジャンル設定に早見表ショートコードがありません")

    genre_name = genre.get("genre_name", "クリニック")
    note = genre.get("notes", "").strip()
    has_online = "オンライン診療" in html

    lead_1 = (
        f"ここまで{keyword}で比較してきた内容を踏まえると、まずは"
        f"<strong>料金・通いやすさ・治療内容</strong>を一覧で見比べながら、"
        f"自分に合う{genre_name}の候補を2〜3件に絞るのがおすすめです。"
    )
    if has_online:
        lead_2 = (
            f"通院のしやすさだけでなく、<strong>オンライン診療の有無や続けやすさ</strong>まで含めて確認すると、"
            f"無理なく続けられる候補を選びやすくなります。"
        )
    else:
        lead_2 = (
            f"価格だけでなく、<strong>診療体制やアクセス、治療の続けやすさ</strong>まであわせて確認すると、"
            f"後悔の少ない選び方につながります。"
        )

    note_html = ""
    if note:
        note_html = f'\n<p><small>※{note}</small></p>'

    return (
        f"{CTA_START}\n"
        '<div class="article-final-cta">\n'
        f"<p>{lead_1}</p>\n"
        f"<p>{lead_2}</p>\n"
        f"{shortcode}\n"
        "</div>"
        f"{note_html}\n"
        f"{CTA_END}"
    )


def insert_or_replace_final_cta(html: str, cta_html: str) -> str:
    if CTA_START in html and CTA_END in html:
        return re.sub(
            rf"{re.escape(CTA_START)}.*?{re.escape(CTA_END)}",
            cta_html,
            html,
            flags=re.DOTALL,
        )

    small_note_pattern = re.compile(r"<p><small>.*?</small></p>\s*$", re.DOTALL)
    match = small_note_pattern.search(html)
    if match:
        return html[:match.start()].rstrip() + "\n\n" + cta_html + "\n"
    return html.rstrip() + "\n\n" + cta_html + "\n"


def main():
    parser = argparse.ArgumentParser(description="末尾CTA差し込み")
    parser.add_argument("--html", required=True, help="記事HTMLファイル")
    parser.add_argument("--keyword", required=True, help="検索キーワード")
    parser.add_argument("--genre-json", required=True, help="ジャンル設定JSON")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)
    if not os.path.exists(args.genre_json):
        print(f"Error: ジャンル設定ファイルが見つかりません: {args.genre_json}")
        sys.exit(1)

    genre = load_genre(args.genre_json)
    with open(args.html, "r", encoding="utf-8") as f:
        html = f.read()

    cta_html = build_final_cta(args.keyword, genre, html)
    updated = insert_or_replace_final_cta(html, cta_html)

    with open(args.html, "w", encoding="utf-8") as f:
        f.write(updated)

    print(f"末尾CTAを更新しました: {args.html}")


if __name__ == "__main__":
    main()
