#!/usr/local/bin/python3.12
"""
SEO記事画像生成スクリプト (Step 5)
Google Gemini Imagen APIを使用してトップ画像・H2画像を生成する。

使い方:
  python generate_images.py --keyword "AGA 横浜" --html output/aga_横浜_記事.html

環境変数:
  GEMINI_API_KEY: Google AI Studio の API キー
"""

import argparse
import os
import re
import sys
import time
import base64
from pathlib import Path

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("Error: google-genai パッケージが必要です。")
    print("  pip install google-genai")
    sys.exit(1)


# ========================================
# 設定
# ========================================
MODEL_ID = "imagen-4.0-generate-001"
OUTPUT_DIR = "output/images"
TOP_IMAGE_SIZE = "1200x630"
H2_IMAGE_SIZE = "1200x400"


# ========================================
# HTML解析
# ========================================
def extract_h2_headings(html_path: str) -> list[str]:
    """HTMLファイルからH2見出しテキストを抽出する"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # H2タグのテキストを抽出（HTMLタグを除去）
    h2_pattern = re.compile(r"<h2>(.*?)</h2>", re.DOTALL)
    headings = []
    for match in h2_pattern.finditer(content):
        text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        headings.append(text)

    return headings


# ========================================
# プロンプト生成
# ========================================
def parse_keyword(keyword: str) -> dict:
    """キーワードを分解して画像要素を決定する"""
    parts = keyword.split()
    result = {
        "keyword": keyword,
        "genre": "",
        "area": "",
        "type": "",
    }

    # ジャンル判定
    genre_map = {
        "AGA": ("AGA治療・薄毛治療", "医療・クリニック・頭髪ケア"),
        "ED": ("ED治療", "医療・男性の健康"),
        "脱毛": ("医療脱毛", "美容・脱毛機器・スキンケア"),
        "医療脱毛": ("医療脱毛", "美容・脱毛機器・スキンケア"),
        "ピル": ("ピル処方", "医療・女性の健康"),
        "アフターピル": ("アフターピル", "医療・女性の健康"),
        "美容": ("美容医療", "美容クリニック"),
    }

    for part in parts:
        for key, (genre_name, genre_visual) in genre_map.items():
            if key.lower() in part.lower():
                result["genre"] = genre_name
                result["genre_visual"] = genre_visual
                break

    # エリア判定（ジャンル以外の部分をエリアとみなす）
    for part in parts:
        is_genre = False
        for key in genre_map:
            if key.lower() in part.lower():
                is_genre = True
                break
        if not is_genre and part not in ["おすすめ", "比較", "ランキング", "口コミ"]:
            result["area"] = part

    # 記事タイプ
    for part in parts:
        if part in ["おすすめ", "比較", "ランキング", "口コミ"]:
            result["type"] = part

    return result


def build_top_image_prompt(keyword_info: dict) -> str:
    """トップ画像のプロンプトを生成する"""
    area = keyword_info.get("area", "")
    genre = keyword_info.get("genre", "")
    genre_visual = keyword_info.get("genre_visual", "医療")

    prompt_parts = [
        "A clean, professional, and modern flat illustration",
        "suitable for a medical article header image.",
        "The image should convey trust, cleanliness, and professionalism.",
    ]

    if area:
        prompt_parts.append(
            f"Include subtle visual elements that suggest the {area} area of Japan "
            f"(e.g., iconic landmarks or cityscape silhouettes of {area})."
        )

    if genre_visual:
        prompt_parts.append(
            f"The theme should relate to {genre_visual}."
        )

    prompt_parts.extend([
        "Use a soft, muted color palette with light blues, whites, and gentle greens.",
        "No text, no letters, no words, no characters in the image.",
        "No faces shown directly - use silhouettes or abstract representations of people if needed.",
        "Aspect ratio: landscape, wide format.",
    ])

    return " ".join(prompt_parts)


def build_h2_image_prompt(heading: str, keyword_info: dict) -> str:
    """H2見出し画像のプロンプトを生成する"""
    heading_lower = heading.lower()

    # H2の内容に応じたビジュアル方向性
    if any(w in heading_lower for w in ["一覧", "比較", "おすすめ", "選"]):
        visual_direction = (
            "multiple options being compared side by side, "
            "a selection or comparison concept with cards or panels"
        )
    elif any(w in heading_lower for w in ["費用", "料金", "相場", "価格"]):
        visual_direction = (
            "medical costs, pricing, coins or bills with medical symbols, "
            "a calculator or price comparison concept"
        )
    elif any(w in heading_lower for w in ["選び方", "ポイント", "チェック"]):
        visual_direction = (
            "a checklist, selection criteria, a magnifying glass examining options, "
            "checkmarks and decision-making"
        )
    elif any(w in heading_lower for w in ["質問", "FAQ", "Q&A", "よくある"]):
        visual_direction = (
            "question marks, Q&A bubbles, a person thinking or wondering, "
            "frequently asked questions concept"
        )
    elif any(w in heading_lower for w in ["まとめ", "結論", "最後"]):
        visual_direction = (
            "a conclusion, final decision, a confident person taking action, "
            "a positive outcome or resolution"
        )
    elif any(w in heading_lower for w in ["治療", "効果", "方法"]):
        visual_direction = (
            "medical treatment process, healthcare, "
            "a doctor or medical professional, treatment methods"
        )
    else:
        visual_direction = (
            "a clean medical or healthcare-related concept "
            "that relates to the topic"
        )

    genre_visual = keyword_info.get("genre_visual", "医療")

    prompt = (
        f"A clean, professional flat illustration for an article section header. "
        f"Theme: {genre_visual}. "
        f"Visual concept: {visual_direction}. "
        f"Use a soft, muted color palette with light blues, whites, and gentle greens. "
        f"No text, no letters, no words, no characters in the image. "
        f"No faces shown directly. "
        f"Minimalist and modern style. "
        f"Aspect ratio: landscape, very wide format (3:1 ratio)."
    )

    return prompt


# ========================================
# 画像生成
# ========================================
def generate_image(client, prompt: str, filename: str, output_dir: str) -> bool:
    """Imagen 4.0 APIで画像を生成して保存する"""
    filepath = os.path.join(output_dir, filename)

    print(f"\n  Generating: {filename}")
    print(f"  Prompt: {prompt[:100]}...")

    try:
        response = client.models.generate_images(
            model=MODEL_ID,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
            ),
        )

        if response.generated_images:
            img = response.generated_images[0]
            with open(filepath, "wb") as f:
                f.write(img.image.image_bytes)
            print(f"  Saved: {filepath}")
            return True

        print(f"  Warning: No image in response for {filename}")
        return False

    except Exception as e:
        print(f"  Error generating {filename}: {e}")
        return False


# ========================================
# HTML挿入
# ========================================
def insert_images_into_html(html_path: str, keyword_slug: str, h2_count: int):
    """生成した画像のimgタグをHTMLに挿入する"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    # トップ画像: 最初の<p>の前に挿入
    top_img_tag = (
        f'<img src="images/{keyword_slug}_top.png" '
        f'alt="{keyword_slug.replace("_", " ")}のおすすめ記事" '
        f'width="1200" height="630" loading="eager">\n\n'
    )

    # 既にトップ画像が挿入されていなければ追加
    if f'src="images/{keyword_slug}_top.png"' not in content:
        content = top_img_tag + content

    # H2画像: 各H2の直後に挿入
    h2_pattern = re.compile(r"(<h2>)(.*?)(</h2>)", re.DOTALL)
    h2_index = [0]  # mutableにするためリスト

    def replace_h2(match):
        h2_index[0] += 1
        n = h2_index[0]
        h2_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        img_tag = (
            f'\n<img src="images/{keyword_slug}_h2_{n}.png" '
            f'alt="{h2_text}" '
            f'width="1200" height="400" loading="lazy">\n'
        )
        # 既に画像が挿入されていなければ追加
        if f'src="images/{keyword_slug}_h2_{n}.png"' in content:
            return match.group(0)
        return match.group(0) + img_tag

    content = h2_pattern.sub(replace_h2, content)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n  Images inserted into: {html_path}")


# ========================================
# メイン
# ========================================
def main():
    parser = argparse.ArgumentParser(description="SEO記事画像生成スクリプト")
    parser.add_argument("--keyword", required=True, help="検索キーワード（例: 'AGA 横浜'）")
    parser.add_argument("--html", required=True, help="記事HTMLファイルのパス")
    parser.add_argument("--skip-insert", action="store_true", help="HTMLへの画像挿入をスキップ")
    parser.add_argument("--only", type=str, help="特定の画像のみ生成（top, h2_1, h2_2, ...）")
    args = parser.parse_args()

    # API キー確認
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY 環境変数を設定してください。")
        print("  export GEMINI_API_KEY='your-api-key'")
        sys.exit(1)

    # HTML確認
    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    # 出力ディレクトリ作成
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # キーワード解析
    keyword_info = parse_keyword(args.keyword)
    keyword_slug = args.keyword.replace(" ", "_")

    print(f"Keyword: {args.keyword}")
    print(f"  Genre: {keyword_info.get('genre', '不明')}")
    print(f"  Area: {keyword_info.get('area', 'なし')}")

    # H2見出し抽出
    h2_headings = extract_h2_headings(args.html)
    print(f"  H2 headings found: {len(h2_headings)}")
    for i, h in enumerate(h2_headings, 1):
        print(f"    {i}. {h}")

    # Gemini クライアント初期化
    client = genai.Client(api_key=api_key)

    # 画像生成
    results = []

    # トップ画像
    if args.only is None or args.only == "top":
        prompt = build_top_image_prompt(keyword_info)
        filename = f"{keyword_slug}_top.png"
        success = generate_image(client, prompt, filename, OUTPUT_DIR)
        results.append(("top", filename, success))
        time.sleep(2)  # レートリミット対策

    # H2画像
    for i, heading in enumerate(h2_headings, 1):
        h2_key = f"h2_{i}"
        if args.only is not None and args.only != h2_key:
            continue

        prompt = build_h2_image_prompt(heading, keyword_info)
        filename = f"{keyword_slug}_h2_{i}.png"
        success = generate_image(client, prompt, filename, OUTPUT_DIR)
        results.append((h2_key, filename, success))
        time.sleep(2)  # レートリミット対策

    # 結果サマリー
    print("\n" + "=" * 50)
    print("Results:")
    for key, filename, success in results:
        status = "OK" if success else "FAILED"
        print(f"  [{status}] {key}: {filename}")

    # HTML挿入
    if not args.skip_insert:
        successful_count = sum(1 for _, _, s in results if s)
        if successful_count > 0:
            insert_images_into_html(args.html, keyword_slug, len(h2_headings))
        else:
            print("\nNo images generated successfully. Skipping HTML insertion.")

    print("\nDone!")


if __name__ == "__main__":
    main()
