#!/usr/local/bin/python3.12
"""
記事末尾のCTAショートコードを自然な導線付きで差し込む。
"""

import argparse
import json
import os
import re
import sys

from variant_utils import normalize_variant_shortcodes_in_html, resolve_variant_embed_html


CTA_START = "<!-- final-cta:start -->"
CTA_END = "<!-- final-cta:end -->"
MAX_VARIANTS = 5
PLAIN_FINAL_CTA_VARIANTS = {4, 5}


def load_genre(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_variant_index(value: int | str | None) -> int:
    try:
        index = int(value or 1)
    except (TypeError, ValueError):
        index = 1
    return max(1, min(MAX_VARIANTS, index))


def infer_variant_index_from_html_path(path: str) -> int:
    match = re.search(r"__nandemo_v(\d+)", path or "")
    if not match:
        return 1
    return normalize_variant_index(match.group(1))


def build_cta_variant_profile(keyword: str, genre_name: str, has_online: bool, variant_index: int) -> dict[str, object]:
    online_clause = (
        f"{genre_name}の候補を比較するときは、通院前提の院だけでなく"
        "<strong>オンライン診療の有無</strong>まで見ておくと選びやすくなります。"
        if has_online
        else f"{genre_name}の候補を比較するときは、価格だけでなく"
        "<strong>通いやすさや診療体制</strong>まで見ておくのが大切です。"
    )
    legacy_online_clause = (
        "通院のしやすさだけでなく、<strong>オンライン診療の有無や続けやすさ</strong>"
        "まで含めて確認すると、無理なく続けられる候補を選びやすくなります。"
        if has_online
        else "価格だけでなく、<strong>診療体制やアクセス、治療の続けやすさ</strong>"
        "まであわせて確認すると、後悔の少ない選び方につながります。"
    )

    profiles = [
        {
            "class_name": "cta-variant-1",
            "eyebrow": "COMPARE FIRST",
            "heading": "まずは早見表で候補を2〜3件に絞りたい人向け",
            "lead_1": (
                f"ここまで{keyword}で比較してきた内容を踏まえると、まずは"
                f"<strong>料金・通いやすさ・治療内容</strong>を一覧で見比べながら、"
                f"自分に合う{genre_name}の候補を2〜3件に絞るのがおすすめです。"
            ),
            "lead_2": legacy_online_clause,
        },
        {
            "class_name": "cta-variant-2",
            "eyebrow": "CHECK YOUR FIT",
            "heading": "自分に合う条件から逆算して候補を見つけたい人向け",
            "lead_1": (
                f"{keyword}で比較するときは、最初から1院に決めるよりも、"
                f"<strong>予算・診療時間・アクセス条件</strong>に合うところを並べて見る方が"
                "判断しやすくなります。"
            ),
            "lead_2": (
                f"特に、忙しい人ほど<strong>続けやすさと受診しやすさ</strong>の差が大きいので、"
                "早見表で条件が合う院から順に確認していくのが効率的です。"
            ),
        },
        {
            "class_name": "cta-variant-3",
            "eyebrow": "COST & CONTINUITY",
            "heading": "料金と続けやすさをまとめて比べたい人向け",
            "lead_1": (
                f"{genre_name}は、始めやすさだけでなく"
                f"<strong>数ヶ月単位で無理なく続けられるか</strong>まで見て選ぶことが重要です。"
            ),
            "lead_2": (
                f"{keyword}の候補を比較するなら、月額感・診察料・通いやすさを"
                "<strong>ひと目で整理できる早見表</strong>から確認すると、判断がぶれにくくなります。"
            ),
        },
        {
            "class_name": "cta-variant-4",
            "eyebrow": "",
            "heading": "",
            "use_plain_layout": True,
            "lead_1": (
                f"ここまで{keyword}で比較してきた内容を踏まえると、まずは"
                f"<strong>料金・通いやすさ・治療内容</strong>を一覧で見比べながら、"
                f"自分に合う{genre_name}の候補を2〜3件に絞るのがおすすめです。"
            ),
            "lead_2": legacy_online_clause,
        },
        {
            "class_name": "cta-variant-5",
            "eyebrow": "OFFICIAL CHECK",
            "heading": "公式情報を見比べながら候補を選びたい人向け",
            "use_plain_layout": True,
            "lead_1": (
                f"{genre_name}は、最終的には<strong>公式サイトで最新料金や診療条件を確認すること</strong>が大切です。"
            ),
            "lead_2": (
                f"その前段として、{keyword}の候補を早見表で横並びにしておくと、"
                "<strong>どの院を優先して確認すべきか</strong>がはっきりします。"
            ),
        },
    ]
    return profiles[normalize_variant_index(variant_index) - 1]


def build_final_cta(keyword: str, genre: dict, html: str, html_path: str, variant_index: int = 1) -> str:
    shortcode = genre.get("shortcodes", {}).get("早見表", "").strip()
    shortcode = resolve_variant_embed_html(
        shortcode,
        genre_id=genre.get("genre_id", ""),
        output_key=html_path,
        variant_index=variant_index,
    )
    if not shortcode:
        raise ValueError("ジャンル設定に早見表ショートコードがありません")

    genre_name = genre.get("genre_name", "クリニック")
    note = genre.get("notes", "").strip()
    has_online = "オンライン診療" in html
    profile = build_cta_variant_profile(keyword, genre_name, has_online, variant_index)

    note_html = ""
    if note:
        note_html = f'\n<p><small>※{note}</small></p>'

    # V4/V5 は本文との馴染みを優先し、外側ラッパーを持たないプレーン配置を正式ルールにする。
    if normalize_variant_index(variant_index) in PLAIN_FINAL_CTA_VARIANTS or profile.get("use_plain_layout"):
        return (
            f"{CTA_START}\n"
            f"<p>{profile['lead_1']}</p>\n"
            f"<p>{profile['lead_2']}</p>\n"
            f"{shortcode}"
            f"{note_html}\n"
            f"{CTA_END}"
        )

    return (
        f"{CTA_START}\n"
        f'<div class="article-final-cta__title">{profile["heading"]}</div>\n'
        f"<p>{profile['lead_1']}</p>\n"
        f"<p>{profile['lead_2']}</p>\n"
        f"{shortcode}"
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

    small_note_pattern = re.compile(r"<p><small>[^<]*</small></p>", re.DOTALL)
    matches = list(small_note_pattern.finditer(html))
    if matches:
        last_match = matches[-1]
        tail = html[last_match.end():].strip()
        if tail == "":
            return html[:last_match.start()].rstrip() + "\n\n" + cta_html + "\n"
    return html.rstrip() + "\n\n" + cta_html + "\n"


def main():
    parser = argparse.ArgumentParser(description="末尾CTA差し込み")
    parser.add_argument("--html", required=True, help="記事HTMLファイル")
    parser.add_argument("--keyword", required=True, help="検索キーワード")
    parser.add_argument("--genre-json", required=True, help="ジャンル設定JSON")
    parser.add_argument("--variant-index", type=int, default=1, help="CTAバリエーション番号")
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

    variant_index = normalize_variant_index(args.variant_index)
    if variant_index == 1:
        variant_index = infer_variant_index_from_html_path(args.html)

    html = normalize_variant_shortcodes_in_html(
        html,
        genre_id=genre.get("genre_id", ""),
        output_key=args.html,
        variant_index=variant_index,
    )
    cta_html = build_final_cta(args.keyword, genre, html, args.html, variant_index=variant_index)
    updated = insert_or_replace_final_cta(html, cta_html)

    with open(args.html, "w", encoding="utf-8") as f:
        f.write(updated)

    print(f"末尾CTAを更新しました: {args.html}")


if __name__ == "__main__":
    main()
