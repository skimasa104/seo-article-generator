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
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from env_utils import load_project_env
from output_utils import ensure_keyword_images_dir, keyword_to_slug, resolve_output_key
from variant_utils import build_variant_profile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Error: Pillow パッケージが必要です。")
    print("  python -m pip install Pillow")
    sys.exit(1)

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


# ========================================
# 設定
# ========================================
MODEL_ID = "gemini-3-pro-image-preview"
TOP_ASPECT_RATIO = "16:9"
H2_ASPECT_RATIO = "16:9"
MAX_IMAGE_RETRIES = 3
IMAGE_GENERATION_VERSION = 3
MAX_TOP_COPY_LENGTH = 40
MAX_H2_COPY_LENGTH = 20
GEMINI_IMAGE_SIZE = "2K"
SITE_STYLE_MODEL_ID = "gemini-2.5-flash"
SITE_STYLE_CACHE_DIR = Path(__file__).parent / "site_style_cache"
SITE_REFERENCE_SUFFIX = ".reference.png"
DEFAULT_SITE_STYLE = {
    "name": "generic-medical-editorial",
    "site_url": "",
    "tone": (
        "白ベースで清潔感があり、信頼感のある日本向け医療メディアの世界観。 "
        "余白を多めに取り、上品で落ち着いたエディトリアルデザイン。"
    ),
    "colors": "白、オフホワイト、淡いグレー、やさしいアクセントカラー",
    "mood": "誠実、清潔、やわらかい、読みやすい、過度に広告っぽくない",
    "avoid": "ネオンカラー、どぎつい配色、派手な比較広告、情報過多、安っぽい装飾",
}
load_project_env()


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


def extract_article_visual_context(html_path: str) -> dict:
    """記事全体と各H2の要点を、画像プロンプト用に軽く抽出する。"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    soup = BeautifulSoup(content, "lxml")
    context = {
        "intro_paragraphs": [],
        "sections": {},
    }

    root = soup.body or soup
    for node in root.children:
        tag_name = getattr(node, "name", None)
        if tag_name == "h2":
            break
        if tag_name == "p":
            text = normalize_editorial_copy(node.get_text(" ", strip=True))
            if text:
                context["intro_paragraphs"].append(text)
        if len(context["intro_paragraphs"]) >= 2:
            break

    for h2 in soup.find_all("h2"):
        heading = normalize_editorial_copy(h2.get_text(" ", strip=True))
        if not heading:
            continue

        section = {"h3s": [], "paragraphs": []}
        node = h2.find_next_sibling()
        while node and getattr(node, "name", None) != "h2":
            tag_name = getattr(node, "name", None)
            if tag_name == "h3":
                text = normalize_editorial_copy(node.get_text(" ", strip=True))
                if text:
                    section["h3s"].append(text)
            elif tag_name == "p":
                text = normalize_editorial_copy(node.get_text(" ", strip=True))
                if text and not text.startswith("※"):
                    section["paragraphs"].append(text)
            node = node.find_next_sibling()

        context["sections"][heading] = {
            "h3s": section["h3s"][:4],
            "paragraphs": section["paragraphs"][:2],
        }

    return context


def infer_tag_structure_path_from_html(html_path: str) -> str | None:
    """記事HTMLから対応するタグ構成ファイルを推定する。"""
    path = Path(html_path)
    name = path.name
    if not name.endswith("_記事.html"):
        return None
    tag_name = name.replace("_記事.html", "_タグ構成.md")
    candidate = path.with_name(tag_name)
    if candidate.exists():
        return str(candidate)
    return None


def extract_article_title_from_tag_structure(html_path: str) -> str:
    """対応するタグ構成ファイルからtitleタグ文言を抽出する。"""
    tag_structure_path = infer_tag_structure_path_from_html(html_path)
    if not tag_structure_path:
        return ""

    try:
        with open(tag_structure_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return ""

    match = re.search(r"\*\*titleタグ\*\*:\s*(.+)", content)
    if not match:
        return ""

    return normalize_editorial_copy(match.group(1).strip())


def extract_article_type_from_tag_structure(html_path: str) -> str:
    """対応するタグ構成ファイルから記事タイプを抽出する。"""
    tag_structure_path = infer_tag_structure_path_from_html(html_path)
    if not tag_structure_path:
        return ""

    try:
        with open(tag_structure_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return ""

    match = re.search(r"\*\*記事タイプ\*\*:\s*(.+)", content)
    return normalize_editorial_copy(match.group(1).strip()) if match else ""


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


def load_genre_context(keyword_info: dict) -> dict:
    """ジャンル設定ファイルから記事タイプなどの補助情報を取得する"""
    genre = keyword_info.get("genre", "")
    genre_id_map = {
        "AGA治療・薄毛治療": "aga",
        "ED治療": "ed",
        "医療脱毛": "hair_removal",
        "包茎治療": "phimosis",
        "医療ダイエット": "diet",
        "ダイエット": "diet",
    }
    genre_id = genre_id_map.get(genre)
    if not genre_id:
        return {}

    genre_path = Path(__file__).parent / "genres" / f"{genre_id}.json"
    if not genre_path.exists():
        return {}

    try:
        return json.loads(genre_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_article_type(keyword_info: dict) -> str:
    """実際のタグ構成を優先して記事タイプを決める。"""
    explicit_article_type = normalize_editorial_copy(keyword_info.get("article_type", ""))
    if explicit_article_type:
        return explicit_article_type

    genre_context = load_genre_context(keyword_info)
    default_article_type = normalize_editorial_copy(genre_context.get("default_article_type", ""))
    if default_article_type:
        return default_article_type

    genre_article_type = normalize_editorial_copy(genre_context.get("article_type", ""))
    if genre_article_type:
        return genre_article_type

    keyword_type = keyword_info.get("type", "")
    if keyword_type in {"おすすめ", "比較", "ランキング"}:
        return "比較記事"
    if keyword_type:
        return "解説記事"
    return "比較記事"


def load_site_style(site_config_path: str | None = None) -> dict:
    """サイト設定から画像の世界観を決める。"""
    profile = dict(DEFAULT_SITE_STYLE)
    if not site_config_path:
        return profile

    config_path = Path(site_config_path)
    if not config_path.is_absolute():
        config_path = Path(__file__).parent / site_config_path

    try:
        site_config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return profile

    site_url = site_config.get("site_url", "") or ""
    profile["site_url"] = site_url

    if "aurora-clinic.jp" in site_url:
        profile.update(
            {
                "name": "aurora-clinic",
                "tone": (
                    "Aurora Clinic のサイト世界観に合わせる。"
                    "白とオフホワイトをベースに、淡いラベンダーやモーブをアクセントにした、"
                    "やさしい高級感のある美容医療メディアの雰囲気。"
                    "余白を広く取り、上品でクリーン、落ち着いたエディトリアルデザインにする。"
                ),
                "colors": (
                    "白、オフホワイト、淡いラベンダー、薄いモーブ、やわらかいグレー。"
                    "彩度は低めで、上品にまとめる。"
                ),
                "mood": (
                    "やさしい高級感、清潔感、信頼感、過度に営業的でない落ち着き、"
                    "美容クリニックらしい繊細さ。"
                ),
                "avoid": (
                    "強すぎる紫、黒ベース、ネオン、赤の煽り表現、チラシ風、"
                    "情報を詰め込みすぎたバナー、男性向け商材広告っぽい過剰な煽り。"
                ),
            }
        )

    return profile


def get_site_style_cache_path(site_url: str) -> Path | None:
    if not site_url:
        return None
    parsed = urllib.parse.urlparse(site_url)
    domain = parsed.netloc.lower()
    if not domain:
        return None
    safe_domain = re.sub(r"[^a-z0-9.-]", "_", domain)
    return SITE_STYLE_CACHE_DIR / f"{safe_domain}.json"


def get_site_reference_cache_path(site_url: str) -> Path | None:
    if not site_url:
        return None
    parsed = urllib.parse.urlparse(site_url)
    domain = parsed.netloc.lower()
    if not domain:
        return None
    safe_domain = re.sub(r"[^a-z0-9.-]", "_", domain)
    return SITE_STYLE_CACHE_DIR / f"{safe_domain}{SITE_REFERENCE_SUFFIX}"


def resolve_homepage_url(site_url: str) -> str:
    if not site_url:
        return ""
    parsed = urllib.parse.urlparse(site_url)
    if not parsed.scheme or not parsed.netloc:
        return site_url
    return f"{parsed.scheme}://{parsed.netloc}/"


def fetch_site_signals(homepage_url: str) -> dict:
    if not homepage_url:
        return {}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
    }
    response = requests.get(homepage_url, headers=headers, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    description_tag = soup.find("meta", attrs={"name": "description"})
    theme_tag = soup.find("meta", attrs={"name": "theme-color"})
    og_site_name = soup.find("meta", attrs={"property": "og:site_name"})
    return {
        "title": title,
        "description": (description_tag.get("content", "") or "").strip() if description_tag else "",
        "theme_color": (theme_tag.get("content", "") or "").strip() if theme_tag else "",
        "og_site_name": (og_site_name.get("content", "") or "").strip() if og_site_name else "",
    }


def capture_homepage_reference(homepage_url: str, output_path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            try:
                try:
                    page.goto(homepage_url, wait_until="load", timeout=30000)
                except Exception:
                    page.goto(homepage_url, wait_until="domcontentloaded", timeout=30000)
                page.screenshot(path=str(output_path), full_page=False)
            finally:
                browser.close()
        return True
    except Exception:
        return False


def parse_json_response(text: str) -> dict | None:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def analyze_site_style_with_gemini(client, homepage_url: str, signals: dict, screenshot_path: Path | None) -> dict | None:
    prompt = (
        "あなたはWebデザインアートディレクターです。"
        "入力されたクリニックサイトのトップページを見て、"
        "そのサイトに合うSEO記事サムネイル/H2画像の世界観をJSONで要約してください。"
        "配色、余白、光、清潔感、上品さ、広告感の弱さなど、スタイル面だけを抽出してください。"
        "人物の性別、顔立ち、肌のアップなどの具体的な被写体提案はしないでください。"
        "主役のモチーフは記事テーマ側で決まるため、ここでは世界観だけを抽出してください。"
        "出力はJSONのみ。説明文は禁止。"
        '必須キー: name, tone, colors, mood, avoid, thumbnail_guidance, h2_guidance'
    )
    site_text = (
        f"サイトURL: {homepage_url}\n"
        f"タイトル: {signals.get('title', '')}\n"
        f"説明: {signals.get('description', '')}\n"
        f"theme-color: {signals.get('theme_color', '')}\n"
        f"og:site_name: {signals.get('og_site_name', '')}\n"
    )
    contents = [prompt, site_text]
    if screenshot_path and screenshot_path.exists():
        contents.append(
            types.Part.from_bytes(
                data=screenshot_path.read_bytes(),
                mime_type="image/png",
            )
        )
    response = client.models.generate_content(
        model=SITE_STYLE_MODEL_ID,
        contents=contents,
    )
    if not getattr(response, "text", None):
        return None
    parsed = parse_json_response(response.text)
    if not parsed:
        return None
    required = ["name", "tone", "colors", "mood", "avoid", "thumbnail_guidance", "h2_guidance"]
    if not all(parsed.get(key) for key in required):
        return None
    return parsed


def sanitize_site_style_profile(profile: dict) -> dict:
    cleaned = dict(profile)
    cleaned["thumbnail_guidance"] = (
        "主役のモチーフや人物属性は記事テーマに合わせる。"
        "サイトの配色、余白、光の柔らかさ、上品さをSEOサムネイル向けに取り入れ、"
        "少要素で整理された見せ方にする。"
    )
    cleaned["h2_guidance"] = (
        "主役のモチーフや人物属性は記事テーマに合わせる。"
        "サイトの色調と空気感を保ちつつ、本文の区切り画像として主張しすぎない、"
        "静かで整ったバナー構図にする。"
    )
    return cleaned


def build_fallback_site_style(site_url: str, signals: dict) -> dict:
    title = signals.get("title", "")
    description = signals.get("description", "")
    theme_color = signals.get("theme_color", "")
    profile = dict(DEFAULT_SITE_STYLE)
    profile["site_url"] = site_url
    if title or description:
        profile["tone"] = (
            f"{title} {description}".strip()[:180]
            or profile["tone"]
        )
    if theme_color:
        profile["colors"] = f"{profile['colors']}。 theme-color は {theme_color}"
    profile["thumbnail_guidance"] = "SEO記事のトップサムネとして、サイトに自然になじむ上品で整理された構図にする。"
    profile["h2_guidance"] = "本文のH2画像として、主張しすぎず、余白を保った穏やかなバナーにする。"
    return profile


def get_or_create_site_style(client, site_config_path: str | None) -> dict:
    base_profile = load_site_style(site_config_path)
    site_url = base_profile.get("site_url", "")
    cache_path = get_site_style_cache_path(site_url)
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            merged = dict(base_profile)
            merged.update(cached)
            return merged
        except Exception:
            pass

    homepage_url = resolve_homepage_url(site_url)
    if not homepage_url:
        return base_profile

    screenshot_path = None
    signals = {}
    try:
        signals = fetch_site_signals(homepage_url)
    except Exception:
        signals = {}

    if cache_path:
        screenshot_path = cache_path.with_suffix(".png")
        capture_homepage_reference(homepage_url, screenshot_path)

    analyzed = None
    try:
        analyzed = analyze_site_style_with_gemini(client, homepage_url, signals, screenshot_path)
    except Exception:
        analyzed = None

    final_profile = dict(base_profile)
    if analyzed:
        final_profile.update(sanitize_site_style_profile(analyzed))
    else:
        final_profile.update(build_fallback_site_style(homepage_url, signals))

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(final_profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return final_profile


def infer_keyword_intent(keyword: str) -> list[str]:
    """キーワードから想定検索意図を抽出する"""
    intents = []
    mapping = [
        ("おすすめ", "おすすめの選択肢を比較したい"),
        ("比較", "選択肢を並べて比較したい"),
        ("ランキング", "上位の選択肢を知りたい"),
        ("口コミ", "実際の評判や口コミを知りたい"),
        ("費用", "料金やコスト感を知りたい"),
        ("料金", "料金やコスト感を知りたい"),
        ("安い", "費用を抑えたい"),
        ("オンライン", "オンライン診療の利便性を重視している"),
    ]
    for token, description in mapping:
        if token in keyword:
            intents.append(description)

    if not intents:
        intents.append("信頼できる比較情報を見て判断したい")
    return intents


def build_style_guardrails() -> str:
    """画像全体で共通に使う品質ガードレール"""
    return (
        "高品質な日本向けWebメディアのサムネイル用ビジュアル。 "
        "清潔感のあるシンプルな医療イラスト。 "
        "写実的な写真、リアル人物写真、映画風、3Dレンダー、情報過多の複雑構図は避ける。 "
        "少人数、少要素、横長で見やすい、整理された構図。 "
        "ただし単調にはせず、視線誘導があり、主役と補助要素の関係が明快な、意図的に設計された構図にする。 "
        "ロゴ、ウォーターマーク、UI画面、看板の余計な文字は不可。 "
        "画像内に入れてよい文字は、こちらが指定する短い日本語見出しのみ。 "
        "それ以外の文字、ランダムな記号、崩れた文字、英単語、偽テキストは入れない。"
    )


def build_site_visual_direction(site_style: dict, image_role: str) -> str:
    """サイトの世界観に合わせた絵作りの方向性。"""
    role_hint = (
        "記事全体の第一印象を決めるトップサムネイルとして、内容がひと目で伝わり、"
        "サイトの世界観にも自然になじむようにする。"
        if image_role == "top"
        else "本文中のH2画像として、主張しすぎず、上品で整理された区切り画像にする。"
    )
    role_specific = site_style.get("thumbnail_guidance", "") if image_role == "top" else site_style.get("h2_guidance", "")
    return (
        f"サイトの世界観: {site_style['tone']} "
        f"配色方針: {site_style['colors']} "
        f"印象: {site_style['mood']} "
        f"避ける表現: {site_style['avoid']} "
        f"{role_hint} "
        f"{role_specific}"
    )


def build_reference_image_direction(reference_image_path: str | None) -> str:
    if not reference_image_path:
        return ""
    return (
        "参考画像も一緒に渡す。"
        "参考画像からは配色、トーン、線のやわらかさ、余白の使い方などスタイル面だけを参考にする。"
        "主役モチーフや章内容は今回の見出しに合わせ、構図や文言を流用しない。"
    )


def build_genre_visual_direction(keyword_info: dict) -> str:
    """ジャンルに応じた絵作りの方向性"""
    genre = keyword_info.get("genre", "")

    if "AGA" in genre:
        return (
            "薄毛治療、頭皮ケア、毛髪の悩み、医師相談、安心感のある医療サポートを表現する。 "
            "写実写真ではなく、親しみやすく整った日本向け医療イラストにする。"
        )
    if "ED" in genre:
        return (
            "男性の健康相談、プライバシー配慮、信頼感、落ち着いた医療相談の雰囲気を表現する。 "
            "シンプルで上品な日本向け医療イラストにする。"
        )
    if "脱毛" in genre:
        return (
            "清潔感のある美容医療、肌ケア、やさしい施術イメージ、現代的なクリニック空間を表現する。 "
            "過度に写実的ではないシンプルイラストにする。"
        )
    return (
        "信頼感のある日本向け医療クリニックの雰囲気、安心できる相談シーン、 "
        "整理された院内空間をシンプルイラストで表現する。"
    )


def build_top_thumbnail_composition(keyword_info: dict, article_type: str) -> str:
    """トップサムネイルを魅力的かつわかりやすくするための構図指示。"""
    keyword = keyword_info.get("keyword", "")
    genre = keyword_info.get("genre", "")

    layout_parts = [
        "主役1つ、補助要素1〜2つの明快な階層構造にする。",
        "視線が最初に主役へ集まり、次にテーマ補助要素へ流れる構図にする。",
        "左右分割、斜め分割、カード重ね、余白を使った非対称レイアウトなど、編集デザインとして洗練された構図を使ってよい。",
        "ただし要素数は絞り、ゴチャつかせない。",
        "SEO記事サムネイルとして、一覧で見たときにも内容が伝わる強い第一印象を作る。",
    ]

    if "比較" in article_type or any(token in keyword for token in ["おすすめ", "比較", "ランキング"]):
        layout_parts.extend(
            [
                "比較記事として、複数選択肢を連想できる整った比較構図にする。",
                "カード、比較パネル、チェック、順位感、選択肢の並びを抽象化して取り入れてよい。",
                "安っぽいランキングバナーではなく、上質なWebメディアの特集サムネイルの見せ方にする。",
            ]
        )
    else:
        layout_parts.extend(
            [
                "解説記事として、1つのテーマを掘り下げる落ち着いた特集ビジュアルにする。",
                "主役モチーフと補助図形で、記事の論点をひと目で理解できるようにする。",
            ]
        )

    if "AGA" in genre:
        layout_parts.extend(
            [
                "AGA記事として、頭髪の悩み、相談、治療選択、比較検討の空気感を抽象的に表現する。",
                "過度に深刻すぎる表情や恐怖訴求ではなく、悩みから前向きな判断へ向かうトーンにする。",
            ]
        )

    return " ".join(layout_parts)


def build_h2_banner_composition(heading: str) -> str:
    """H2画像を単調にせず、理解しやすい編集バナーにする構図指示。"""
    heading_lower = heading.lower()
    parts = [
        "本文中のH2バナーとして、横長で整理された編集バナー構図にする。",
        "主役1つと補助要素少数で、見出しの意味が1秒で伝わる構成にする。",
        "余白を活かし、静かだが弱すぎない視覚的なフックを作る。",
    ]
    if any(w in heading_lower for w in ["一覧", "比較", "おすすめ", "選"]):
        parts.extend(
            [
                "比較・一覧感が伝わるよう、整列したカード、比較軸、チェックモチーフを上品に抽象化する。",
                "単なる装飾ではなく、選択肢が整理されて見える構図にする。",
            ]
        )
    elif any(w in heading_lower for w in ["費用", "料金", "相場", "価格"]):
        parts.extend(
            [
                "料金や費用の見通しがよくなるイメージとして、比較パネル、価格帯、判断軸を象徴的に見せる。",
                "数字だらけにせず、視覚的に『費用感を整理する章』とわかる構図にする。",
            ]
        )
    elif any(w in heading_lower for w in ["選び方", "ポイント", "チェック"]):
        parts.extend(
            [
                "判断ポイントを整理する章として、チェック、比較視点、相談メモのようなモチーフを洗練して入れる。",
            ]
        )
    elif any(w in heading_lower for w in ["副作用", "注意点", "リスク"]):
        parts.extend(
            [
                "不安を煽りすぎず、注意点を冷静に整理する章として、注意・確認・安心を両立した構図にする。",
            ]
        )
    elif any(w in heading_lower for w in ["よくある", "質問"]):
        parts.extend(
            [
                "疑問解消が自然に伝わる、軽やかでわかりやすい情報バナーにする。",
                "英語テキストは一切使わない。日本語の吹き出しや疑問符アイコンで表現する。",
            ]
        )
    elif any(w in heading_lower for w in ["まとめ", "結論"]):
        parts.extend(
            [
                "内容を整理して前向きに締めくくる章として、整然とした収束感と安心感を出す。",
            ]
        )
    return " ".join(parts)


def normalize_editorial_copy(text: str) -> str:
    """プロンプトに入れやすい形へ整える。"""
    return re.sub(r"\s+", " ", text).strip().strip("「」\"")


def trim_prompt_text(text: str, max_len: int = 140) -> str:
    text = normalize_editorial_copy(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def summarize_intro_context(article_context: dict | None) -> str:
    if not article_context:
        return ""
    paragraphs = [trim_prompt_text(p, 100) for p in article_context.get("intro_paragraphs", []) if p]
    return " / ".join(paragraphs[:2])


def summarize_section_context(article_context: dict | None, heading: str) -> str:
    if not article_context:
        return ""
    section = (article_context.get("sections") or {}).get(heading) or {}
    h3s = [trim_prompt_text(h3, 36) for h3 in section.get("h3s", []) if h3]
    paragraphs = [trim_prompt_text(p, 90) for p in section.get("paragraphs", []) if p]
    parts = []
    if h3s:
        parts.append("小見出し: " + " / ".join(h3s[:4]))
    if paragraphs:
        parts.append("要点: " + " / ".join(paragraphs[:2]))
    return " ".join(parts)


def split_display_text_for_model(text: str, max_lines: int) -> list[str]:
    text = normalize_editorial_copy(text)
    if not text:
        return []
    tokens = tokenize_text_for_layout(text)
    if len(tokens) <= 1:
        return force_balanced_lines(text, max_lines)

    target_len = max(6, round(len(text) / max_lines))
    lines: list[str] = []
    current = ""

    for token in tokens:
        token = token.strip()
        if not token:
            continue
        candidate = current + token if current else token
        if current and len(candidate) > target_len + 2 and len(lines) < max_lines - 1:
            lines.append(current.strip())
            current = token
        else:
            current = candidate

    if current:
        lines.append(current.strip())

    if not lines:
        return force_balanced_lines(text, max_lines)
    if len(lines) > max_lines:
        return force_balanced_lines(text, max_lines)
    return lines


def build_model_text_instruction(lines: list[str]) -> str:
    if not lines:
        return ""
    parts = [
        "画像内に描画する文字は次の日本語だけに限定する。他の文字、英字、記号列、飾り文字は一切入れない。"
    ]
    for idx, line in enumerate(lines, start=1):
        parts.append(f'{idx}行目: "{line}"')
    parts.append("行の順番は守り、日本語として自然に読める改行で大きく明瞭に描画する。")
    return " ".join(parts)


def compact_japanese_copy(text: str, max_len: int) -> str:
    """画像用に短く整える。"""
    text = normalize_editorial_copy(text)
    replacements = [
        ("AGA治療", "AGA"),
        ("クリニック", ""),
        ("を徹底比較", ""),
        ("を比較", ""),
        ("の選び方", "選び方"),
        ("失敗しない", ""),
        ("おすすめ", "おすすめ"),
        ("費用相場と料金の見方", "費用相場"),
        ("費用相場", "費用相場"),
        ("副作用と注意点", "副作用と注意点"),
        ("よくある質問", "よくある質問"),
        ("まとめ｜", ""),
        ("まとめ", "まとめ"),
        ("どの治療法を選ぶべきか", "治療法"),
        ("おすすめクリニック", "おすすめ"),
    ]
    for before, after in replacements:
        text = text.replace(before, after)

    text = re.sub(r"[｜|].*$", "", text).strip()
    text = re.sub(r"\s+", "", text)
    if len(text) <= max_len:
        return text

    splitters = ["と", "・", "、", "の", "を"]
    for splitter in splitters:
        if splitter in text:
            candidate = text.split(splitter)[0].strip()
            if 0 < len(candidate) <= max_len:
                return candidate

    return text[:max_len]


def soft_compact_japanese_copy(text: str, max_len: int) -> str:
    """意味を落としにくい控えめな短縮。主に記事タイトル由来の文言向け。"""
    text = normalize_editorial_copy(re.sub(r"【[^】]+】", "", text))
    text = re.sub(r"[｜|].*$", "", text).strip("!！。 ")
    if len(text) <= max_len:
        return text

    for sep in ["！", "!", "。", "：", ":", "｜", "|"]:
        if sep in text:
            candidate = text.split(sep, 1)[0].strip()
            if candidate and len(candidate) <= max_len:
                return candidate

    return text[:max_len].rstrip()


def get_h2_display_copy(heading: str) -> str:
    """H2画像内に描かせる短い見出し。長すぎる原文は意味を保って要約する。"""
    normalized = normalize_editorial_copy(heading)
    compact = normalized.replace("？", "").replace("!", "").replace("！", "")

    rule_map = [
        (["とは", "違い"], "デイリータダラフィルとは"),
        (["料金", "相場"], "料金相場と価格比較"),
        (["選び方"], "オンラインクリニックの選び方"),
        (["おすすめ", "オンラインクリニック"], "安いおすすめオンラインクリニック12院"),
        (["処方", "流れ"], "オンライン診療の流れ"),
        (["他のED治療薬", "比較"], "ED治療薬との比較"),
        (["通販", "個人輸入"], "クリニック処方を選ぶ理由"),
        (["注意点"], "服用時の注意点"),
        (["よくある", "質問"], "よくある質問"),
        (["まとめ", "コスト"], "まとめ"),
    ]
    for keywords, label in rule_map:
        if all(token in compact for token in keywords):
            return label

    if "まとめ" in compact:
        return "まとめ"
    return soft_compact_japanese_copy(compact, 22)


def build_top_image_prompt(
    keyword_info: dict,
    h2_headings: list[str],
    article_title: str = "",
    article_context: dict | None = None,
    site_style: dict | None = None,
    reference_image_path: str | None = None,
    variant_instruction: str = "",
) -> str:
    """トップ画像のプロンプトを生成する"""
    area = keyword_info.get("area", "")
    genre = keyword_info.get("genre", "")
    style_guardrails = build_style_guardrails()
    genre_direction = build_genre_visual_direction(keyword_info)
    article_type = resolve_article_type(keyword_info)
    search_intents = " / ".join(infer_keyword_intent(keyword_info.get("keyword", "")))
    heading_context = " / ".join(h2_headings[:3]) if h2_headings else ""
    intro_context = summarize_intro_context(article_context)
    title_copy, subtitle_copy = get_top_image_copy(keyword_info, h2_headings, article_title)
    text_lines = split_display_text_for_model(title_copy, 2)
    if subtitle_copy:
        text_lines.extend(split_display_text_for_model(subtitle_copy, 1))
    site_style = site_style or DEFAULT_SITE_STYLE
    site_direction = build_site_visual_direction(site_style, "top")
    reference_direction = build_reference_image_direction(reference_image_path)
    composition_direction = build_top_thumbnail_composition(keyword_info, article_type)

    prompt_parts = [
        style_guardrails,
        "日本語のSEO記事のトップ画像を作成する。",
        f"テーマ: {genre or '医療記事'}。",
        f"記事タイプ: {article_type}。",
        f"記事意図: {search_intents}。",
        genre_direction,
        site_direction,
        reference_direction,
        composition_direction,
        f"記事タイトル: {article_title or keyword_info.get('keyword', '')}。",
        "構図はシンプルで、横長サムネとしてひと目で内容が伝わることを最優先にする。",
        "要素は多くても3つまで。人物は0〜2人まで。背景は簡潔にし、情報を詰め込みすぎない。",
        "リアル写真風ではなく、シンプルで清潔感のある日本向け医療イラストにする。",
        "人物を使う場合も、少人数で、誇張の少ない自然なイラストにする。",
        "記事内容が伝わることを重視し、装飾過多や複雑な背景、余計な小物は避ける。",
        "配色は白やオフホワイトを基調に、淡いラベンダーややさしいニュアンスカラーを上品に使う。",
        "比率は16:9、サムネイルとして読みやすい明快な構図。",
        "魅力的で洗練されて見えることを重視するが、情報量ではなく構図の巧さで見せる。",
        "記事全体のテーマと無関係な汎用医療イラスト、意味のないタブレット持ちポーズ、抽象的すぎる素材集っぽい絵は避ける。",
    ]

    if variant_instruction:
        prompt_parts.append(f"今回の別編集版向け調整: {variant_instruction}")

    if area:
        prompt_parts.append(
            f"{area}らしさは控えめなランドマークや街の雰囲気として軽く入れる。観光ポスターにはしない。"
        )

    if heading_context:
        prompt_parts.append(
            f"記事の主な内容: {heading_context}。"
        )
    if intro_context:
        prompt_parts.append(
            f"導入で伝えている要点: {intro_context}。"
        )

    prompt_parts.append(build_model_text_instruction(text_lines))
    prompt_parts.append("文字は中央揃えでも左揃えでもよいが、読み順が一目で分かる配置にする。")

    return " ".join(prompt_parts)


def infer_heading_focus(heading: str) -> str:
    """見出しに対して、必ず押さえたい構図要素を返す"""
    heading_lower = heading.lower()

    if any(w in heading_lower for w in ["一覧", "比較", "おすすめ", "ランキング", "選"]):
        return (
            "複数の選択肢を比較するイメージ。カード、チェック、比較、一覧感が伝わるシンプルな構図。"
        )
    if any(w in heading_lower for w in ["費用", "料金", "相場", "価格", "コスト"]):
        return (
            "費用や料金比較を連想できるイメージ。金額表現は象徴的にし、細かい数字は描かない。"
        )
    if any(w in heading_lower for w in ["選び方", "ポイント", "チェック", "比較ポイント"]):
        return (
            "選び方や判断基準を連想できるイメージ。チェック、比較、相談の雰囲気。"
        )
    if any(w in heading_lower for w in ["質問", "よくある"]):
        return (
            "疑問を解消する安心感のある日本語バナー。吹き出しや疑問符を主役にしたシンプルなアイコン構成。"
        )
    if any(w in heading_lower for w in ["まとめ", "結論", "最後"]):
        return (
            "まとめや最終判断を連想できる、前向きで整理されたイメージ。"
        )
    if any(w in heading_lower for w in ["治療", "効果", "方法", "流れ"]):
        return (
            "治療法や流れを連想できる、医療説明イラスト。"
        )
    return (
        "見出し内容を素直に伝える、清潔感のあるシンプルな医療イラスト。"
    )


def get_top_image_copy(keyword_info: dict, h2_headings: list[str], article_title: str = "") -> tuple[str, str]:
    """トップ画像用のタイトル・サブタイトルを返す。"""
    area = keyword_info.get("area", "").strip()
    genre = keyword_info.get("genre", "")
    article_type = resolve_article_type(keyword_info)
    keyword = keyword_info.get("keyword", "")

    genre_label_map = {
        "AGA治療・薄毛治療": "AGA",
        "ED治療": "ED",
        "医療脱毛": "医療脱毛",
    }
    genre_label = genre_label_map.get(genre, genre or "医療")

    if article_title:
        title_seed = normalize_editorial_copy(re.sub(r"【[^】]+】", "", article_title))
        parts = [
            normalize_editorial_copy(part)
            for part in re.split(r"[｜|!！。]", title_seed)
            if part.strip()
        ]
        if len(parts) >= 2:
            title = parts[0]
            subtitle = parts[1]
            return (
                soft_compact_japanese_copy(title, MAX_TOP_COPY_LENGTH),
                soft_compact_japanese_copy(subtitle, MAX_H2_COPY_LENGTH * 2),
            )
        title = title_seed
    elif area and genre_label:
        title = f"{area}×{genre_label}"
    else:
        title = keyword.replace(" ", "　")

    heading_text = h2_headings[0] if h2_headings else ""
    if any(token in heading_text for token in ["比較", "おすすめ", "ランキング", "選"]):
        subtitle = f"{article_type}・おすすめ情報"
    elif any(token in heading_text for token in ["料金", "費用", "相場", "価格"]):
        subtitle = "料金・相場をわかりやすく解説"
    elif any(token in heading_text for token in ["口コミ", "評判"]):
        subtitle = "口コミ・評判をチェック"
    else:
        subtitle = f"{article_type}をわかりやすく解説"

    title = soft_compact_japanese_copy(title, MAX_TOP_COPY_LENGTH)
    subtitle = compact_japanese_copy(subtitle, MAX_H2_COPY_LENGTH * 2)
    if len(title) >= 20 and not subtitle:
        title_lines = split_display_text_for_model(title, 2)
        title = "".join(title_lines)
    return title, subtitle


def tokenize_text_for_layout(text: str) -> list[str]:
    """改行用のゆるいトークン分割。日本語でも極端な1行化を避ける。"""
    normalized = text.strip()
    if not normalized:
        return []

    break_before_words = [
        "オンライン",
        "クリニック",
        "料金",
        "費用",
        "相場",
        "比較",
        "選び方",
        "おすすめ",
        "質問",
        "注意点",
    ]
    for word in break_before_words:
        normalized = normalized.replace(word, "\n" + word)

    separators = ["｜", "|", "・", "、", "。", "：", ":", "×", " ", "　", "では", "とは", "から", "まで", "で", "を", "の", "と", "が", "は", "に", "へ", "も"]
    for sep in separators:
        normalized = normalized.replace(sep, sep + "\n")

    tokens = [token for token in normalized.split("\n") if token]
    if not tokens:
        return [normalized]
    return tokens


def force_balanced_lines(text: str, max_lines: int) -> list[str]:
    """長文をざっくり均等な長さで分割する。"""
    text = text.strip()
    if not text:
        return []
    if max_lines <= 1 or len(text) <= 12:
        return [text]

    target = max(6, len(text) // max_lines)
    lines = []
    remaining = text
    for remaining_lines in range(max_lines, 1, -1):
        split_at = min(len(remaining) - (remaining_lines - 1) * 4, target)
        best = split_at
        for offset in range(-4, 5):
            idx = split_at + offset
            if idx <= 0 or idx >= len(remaining):
                continue
            if remaining[idx - 1] in "・、。:：|｜ ":
                best = idx
                break
        lines.append(remaining[:best].strip("・、。:：|｜ "))
        remaining = remaining[best:].strip("・、。:：|｜ ")
    if remaining:
        lines.append(remaining)
    return [line for line in lines if line]
def build_h2_image_prompt(
    heading: str,
    keyword_info: dict,
    article_context: dict | None = None,
    site_style: dict | None = None,
    reference_image_path: str | None = None,
    heading_index: int = 0,
    variant_instruction: str = "",
) -> str:
    """H2見出し画像のプロンプトを生成する"""
    heading_lower = heading.lower()

    # H2の内容に応じたビジュアル方向性（英語キーワードをモデルに渡さない）
    if any(w in heading_lower for w in ["一覧", "比較", "おすすめ", "選"]):
        visual_direction = (
            "複数の選択肢を並べて比較するイメージ。"
            "カードや比較パネルのような整理された見せ方"
        )
        use_character = True
    elif any(w in heading_lower for w in ["費用", "料金", "相場", "価格"]):
        visual_direction = (
            "費用や料金比較を連想できるイメージ。"
            "コイン、通帳、価格帯の比較パネルなどのアイコンを主役にする。"
            "人物（医師・患者）は使わない。"
        )
        use_character = False
    elif any(w in heading_lower for w in ["選び方", "ポイント", "チェック"]):
        visual_direction = (
            "選び方や比較ポイントを連想できるイメージ。"
            "チェックマーク、判断軸、比較リストのアイコンが主役。"
        )
        use_character = True
    elif any(w in heading_lower for w in ["質問", "よくある"]):
        visual_direction = (
            "疑問を解消する安心感のあるイメージ。"
            "吹き出し、疑問符、回答アイコンを主役にしたシンプルなバナー。"
            "人物（医師・患者）は使わない。"
            "画像内に英語テキストは一切入れない。"
        )
        use_character = False
    elif any(w in heading_lower for w in ["まとめ", "結論", "最後"]):
        visual_direction = (
            "まとめや最終判断を連想できるイメージ。"
            "チェックリスト、矢印、前向きな収束感を表すアイコン構成。"
            "人物は使わない。"
        )
        use_character = False
    elif any(w in heading_lower for w in ["治療", "効果", "方法"]):
        visual_direction = (
            "治療法や医療説明を連想できるイメージ。"
            "頭皮・毛髪の回復プロセスを表す図解的なイラスト。"
            "医師を登場させる場合は横向きや斜め向きのポーズにし、タブレットは持たせない。"
        )
        use_character = True
    else:
        visual_direction = (
            "見出し内容に合う、清潔感のある医療イラストイメージ"
        )
        use_character = heading_index % 2 == 0

    # インデックスに応じた構図バリエーション
    layout_variants = [
        "主役モチーフを左寄りに配置し、右側にテキスト領域を設ける左寄せ構図。",
        "主役モチーフを中央に置き、左右対称で整理したセンター構図。",
        "主役モチーフを右寄りに配置し、左側にテキスト領域を設ける右寄せ構図。",
    ]
    layout_direction = layout_variants[heading_index % len(layout_variants)]

    genre_visual = keyword_info.get("genre_visual", "医療")
    genre_direction = build_genre_visual_direction(keyword_info)
    style_guardrails = build_style_guardrails()
    article_type = resolve_article_type(keyword_info)
    heading_focus = infer_heading_focus(heading)
    section_context = summarize_section_context(article_context, heading)
    heading_copy = get_h2_display_copy(heading)
    text_lines = split_display_text_for_model(heading_copy, 2)
    site_style = site_style or DEFAULT_SITE_STYLE
    site_direction = build_site_visual_direction(site_style, "h2")
    reference_direction = build_reference_image_direction(reference_image_path)
    banner_composition = build_h2_banner_composition(heading)

    character_direction = (
        "人物（医師・患者）は使わない。アイコンや図表的なモチーフだけを主役にする。 "
        if not use_character
        else "人物を使う場合は正面向き立ち姿・タブレット手持ちのポーズは避け、構図に合わせた自然な配置にする。 "
    )

    prompt = (
        f"{style_guardrails} "
        f"日本語SEO記事のH2見出し用バナー画像を作成する。 "
        f"記事タイプ: {article_type}。 "
        f"テーマ: {genre_visual}。 "
        f"章のテーマ説明: {heading}。 "
        f"ビジュアル方向性: {visual_direction}。 "
        f"必須の構図意図: {heading_focus} "
        f"{character_direction}"
        f"{layout_direction} "
        f"{genre_direction} "
        f"{site_direction} "
        f"{reference_direction} "
        f"{banner_composition} "
        f"{('この章の内容: ' + section_context) if section_context else ''} "
        f"シンプルで清潔感のある日本向け医療イラストにする。 "
        f"要素は少なく、横長バナーとしてひと目で意味が伝わるようにする。 "
        f"背景は簡潔にし、複雑すぎる演出や写実写真風は避ける。 "
        f"白やオフホワイトを基調に、淡いラベンダーや柔らかいニュアンスカラーで上品にまとめる。 "
        f"横長バナー構図。余白を保ち、整理された見た目にする。 "
        f"装飾量ではなく、構図と視線誘導で『少し凝って見える』品質を目指す。 "
        f"この章の内容と関係ない汎用医療イラストや、別章でも使い回せる曖昧なモチーフは避ける。 "
        f"画像内に見出し文字を置く場所は1か所だけにする。 "
        f"カード、比較表、吹き出し、薬パッケージ、画面、ラベル、背景には文字や数字を一切入れない。 "
        f"長い元見出しは描かず、指定する短い見出しだけを描画する。 "
    )

    prompt += " " + build_model_text_instruction(text_lines)
    prompt += " 文字は2行以内で、短い見出しバナーとして明瞭に描画する。"
    if variant_instruction:
        prompt += f" 別編集版向けの差分指定: {variant_instruction}"

    return prompt


def review_generated_image(image_path: str, image_kind: str) -> dict:
    """画像の最低限の品質レビュー。"""
    image = Image.open(image_path)
    width, height = image.size
    issues = []
    if width <= height:
        issues.append("画像が横長ではありません")
    if width < 1000:
        issues.append("画像幅が小さすぎます")
    return {"ok": not issues, "issues": issues}


# ========================================
# 画像生成
# ========================================
def is_gemini_native_image_model(model_id: str) -> bool:
    """Geminiネイティブ画像生成モデルかどうか。"""
    return model_id.startswith("gemini-") and "image" in model_id


def extract_image_parts_from_response(response) -> list:
    """GenerateContentレスポンスから画像パートを集める。"""
    parts = []
    if getattr(response, "parts", None):
        parts.extend(response.parts)

    if not parts and getattr(response, "candidates", None):
        for candidate in response.candidates:
            content = getattr(candidate, "content", None)
            if content and getattr(content, "parts", None):
                parts.extend(content.parts)

    return parts


def detect_mime_type(path: str) -> str:
    mime_type, _ = mimetypes.guess_type(path)
    return mime_type or "image/png"


def get_image_metadata_path(image_path: str) -> str:
    return f"{image_path}.meta.json"


def current_image_signature(image_kind: str) -> dict:
    return {
        "image_kind": image_kind,
        "model_id": MODEL_ID,
        "text_render_mode": "model",
        "generator_mode": "gemini-native" if is_gemini_native_image_model(MODEL_ID) else "generate-images",
        "generation_version": IMAGE_GENERATION_VERSION,
    }


def file_exists_case_sensitive(filepath: str) -> bool:
    """大文字小文字を区別してファイルの存在を確認する。
    macOS の case-insensitive FS でも正確に判定できる。"""
    path = Path(filepath)
    parent = path.parent
    if not parent.exists():
        return False
    return path.name in set(os.listdir(str(parent)))


def load_image_metadata(image_path: str) -> dict | None:
    meta_path = get_image_metadata_path(image_path)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_image_metadata(image_path: str, payload: dict) -> None:
    meta_path = get_image_metadata_path(image_path)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def should_regenerate_image(image_path: str, image_kind: str) -> tuple[bool, str]:
    if not file_exists_case_sensitive(image_path):
        return True, "missing_file"

    metadata = load_image_metadata(image_path)
    if not metadata:
        return True, "missing_metadata"

    current_signature = current_image_signature(image_kind)
    saved_signature = metadata.get("signature") or {}
    if saved_signature != current_signature:
        return True, "signature_changed"

    return False, "up_to_date"


def generate_image(
    client,
    prompt: str,
    filename: str,
    output_dir: str,
    aspect_ratio: str,
    reference_image_path: str | None = None,
) -> bool:
    """モデル種別に応じて画像を生成して保存する。"""
    filepath = os.path.join(output_dir, filename)

    print(f"\n  Generating: {filename}")
    print(f"  Prompt: {prompt[:100]}...")

    try:
        if is_gemini_native_image_model(MODEL_ID):
            contents = [prompt]
            if reference_image_path and os.path.exists(reference_image_path):
                contents.append(
                    types.Part.from_bytes(
                        data=Path(reference_image_path).read_bytes(),
                        mime_type=detect_mime_type(reference_image_path),
                    )
                )
            response = client.models.generate_content(
                model=MODEL_ID,
                contents=contents,
                config=types.GenerateContentConfig(
                    image_config=types.ImageConfig(
                        aspect_ratio=aspect_ratio,
                        image_size=GEMINI_IMAGE_SIZE,
                    )
                ),
            )

            for part in extract_image_parts_from_response(response):
                if getattr(part, "inline_data", None) is not None:
                    try:
                        image = part.as_image()
                        image.save(filepath)
                    except Exception:
                        with open(filepath, "wb") as f:
                            f.write(part.inline_data.data)
                    print(f"  Saved: {filepath}")
                    return True

            print(f"  Warning: No image part in response for {filename}")
            return False

        response = client.models.generate_images(
            model=MODEL_ID,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=aspect_ratio,
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


def refine_prompt_for_retry(prompt: str, image_kind: str, attempt: int) -> str:
    """再試行時に、文字や構図が整理された画像へ寄せる。"""
    retry_hints = [
        "構図をさらに単純化し、要素数を減らす。",
        "人物数を減らし、背景をよりシンプルにする。",
        "文字まわりをより読みやすくし、不要な装飾や細部を減らす。",
    ]
    if image_kind == "h2":
        retry_hints.append("短い見出しが読みやすく、主役と補助要素の役割がはっきりした上品な見出しバナーにする。")
    else:
        retry_hints.append("トップサムネとして、短い日本語見出しが自然に読めて、視線誘導のある洗練された構図にする。")

    extra = " ".join(retry_hints[: min(attempt, len(retry_hints)) + 1])
    return prompt + " " + extra


def generate_with_quality_gate(
    client,
    base_prompt: str,
    filename: str,
    output_dir: str,
    aspect_ratio: str,
    image_kind: str,
    reviewer,
    reference_image_path: str | None = None,
):
    """生成→監査→必要なら再生成を行う。"""
    final_audit = {"ok": False, "issues": ["not_generated"]}
    for attempt in range(1, MAX_IMAGE_RETRIES + 1):
        prompt = base_prompt if attempt == 1 else refine_prompt_for_retry(base_prompt, image_kind, attempt)
        print(f"  Review attempt: {attempt}/{MAX_IMAGE_RETRIES}")
        success = generate_image(client, prompt, filename, output_dir, aspect_ratio, reference_image_path=reference_image_path)
        if not success:
            continue

        filepath = os.path.join(output_dir, filename)
        final_audit = reviewer(filepath)
        if final_audit.get("ok"):
            if final_audit.get("lines"):
                print(f"  Review passed with lines: {final_audit['lines']}")
            if final_audit.get("title_lines") or final_audit.get("subtitle_lines"):
                print(f"  Review passed with title lines: {final_audit.get('title_lines', [])}")
            return True, final_audit

        print(f"  Review failed: {', '.join(final_audit.get('issues', []))}")

    return False, final_audit


# ========================================
# フォールバック画像
# ========================================
def wrap_text_for_fallback(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]

    lines = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        bbox = draw.textbbox((0, 0), trial, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = trial
            continue
        lines.append(current)
        current = word
    lines.append(current)
    return lines


def create_fallback_top_image(filepath: str, keyword_info: dict, article_title: str = "") -> None:
    width, height = 1200, 675
    image = Image.new("RGB", (width, height), "#f7f9fc")
    draw = ImageDraw.Draw(image)

    for y in range(height):
        ratio = y / max(height - 1, 1)
        r1, g1, b1 = (247, 249, 252)
        r2, g2, b2 = (232, 240, 251)
        color = (
            int(r1 + (r2 - r1) * ratio),
            int(g1 + (g2 - g1) * ratio),
            int(b1 + (b2 - b1) * ratio),
        )
        draw.line([(0, y), (width, y)], fill=color)

    accent = "#4f7db8"
    secondary = "#9bb7de"
    draw.rounded_rectangle((72, 72, width - 72, height - 72), radius=36, outline=secondary, width=4, fill="#ffffff")
    draw.rounded_rectangle((108, 118, 380, 154), radius=18, fill=accent)

    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", 52)
        body_font = ImageFont.truetype("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 26)
        badge_font = ImageFont.truetype("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc", 22)
    except OSError:
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()
        badge_font = ImageFont.load_default()

    draw.text((132, 124), "MEDICAL ARTICLE IMAGE", fill="#ffffff", font=badge_font)

    area = keyword_info.get("area", "").strip()
    genre = keyword_info.get("genre", "").strip() or "医療記事"
    title_text = article_title.strip() or f"{area}の{genre}" if area else genre
    title_text = title_text.replace("！", " ").replace("｜", " ").strip()
    lines = wrap_text_for_fallback(draw, title_text, title_font, width - 260)

    current_y = 220
    for line in lines[:3]:
        draw.text((132, current_y), line, fill="#213047", font=title_font)
        bbox = draw.textbbox((132, current_y), line, font=title_font)
        current_y = bbox[3] + 12

    _, subtitle = get_top_image_copy(keyword_info, [], article_title)
    subtitle = subtitle or "費用・口コミ・アクセスをわかりやすく比較"
    draw.text((136, current_y + 16), subtitle, fill="#5f6f85", font=body_font)

    draw.rounded_rectangle((132, height - 190, width - 132, height - 126), radius=20, fill="#eef4fb")
    detail = f"{area} / {genre}" if area else genre
    draw.text((164, height - 172), detail, fill="#4f647f", font=body_font)

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    image.save(filepath, format="PNG")


# ========================================
# HTML挿入
# ========================================
def insert_images_into_html(html_path: str, keyword_slug: str, available_h2_numbers: list[int]):
    """存在するH2画像だけをHTMLに挿入する。トップ画像は本文に入れない。"""
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    available_h2 = set(available_h2_numbers)
    if not available_h2:
        print("\n  No H2 images available for insertion. Skipping HTML insertion.")
        return

    # H2画像: 各H2の直後に挿入
    h2_pattern = re.compile(r"(<h2>)(.*?)(</h2>)", re.DOTALL)
    h2_index = [0]  # mutableにするためリスト

    def replace_h2(match):
        h2_index[0] += 1
        n = h2_index[0]
        if n not in available_h2:
            return match.group(0)
        h2_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        img_tag = (
            f'\n<img src="images/{keyword_slug}_h2_{n}.png" '
            f'alt="{h2_text}" '
            f'width="1200" height="675" loading="lazy">\n'
        )
        # 既に画像が挿入されていなければ追加
        if f'src="images/{keyword_slug}_h2_{n}.png"' in content:
            return match.group(0)
        return match.group(0) + img_tag

    content = h2_pattern.sub(replace_h2, content)

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n  Images inserted into: {html_path}")


def register_site_reference_image(site_style: dict, explicit_reference_path: str | None) -> str | None:
    if not explicit_reference_path:
        return None
    if not os.path.exists(explicit_reference_path):
        return None
    cache_path = get_site_reference_cache_path(site_style.get("site_url", ""))
    if cache_path is None:
        return explicit_reference_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(explicit_reference_path, cache_path)
    return str(cache_path)


def resolve_reference_image_path(site_style: dict, explicit_reference_path: str | None = None) -> str | None:
    registered = register_site_reference_image(site_style, explicit_reference_path)
    if registered:
        return registered
    cache_path = get_site_reference_cache_path(site_style.get("site_url", ""))
    if cache_path and cache_path.exists():
        return str(cache_path)
    return None


# ========================================
# メイン
# ========================================
def main():
    parser = argparse.ArgumentParser(description="SEO記事画像生成スクリプト")
    parser.add_argument("--keyword", required=True, help="検索キーワード（例: 'AGA 横浜'）")
    parser.add_argument("--html", required=True, help="記事HTMLファイルのパス")
    parser.add_argument("--site-config", help="サイト設定JSONのパス（世界観の参照に使用）")
    parser.add_argument("--reference-image", help="タッチ統一用の参照画像パス")
    parser.add_argument("--skip-insert", action="store_true", help="HTMLへの画像挿入をスキップ")
    parser.add_argument("--only", type=str, help="特定の画像のみ生成（top, h2_1, h2_2, ...）")
    parser.add_argument("--force", action="store_true", help="既存画像があっても再生成する")
    parser.add_argument("--output-key", help="出力先の識別キー（省略時はキーワード）")
    parser.add_argument("--variant-index", type=int, default=1, help="編集バリエーション番号")
    parser.add_argument("--variant-count", type=int, default=1, help="生成する総バリエーション数")
    args = parser.parse_args()

    # API キー確認
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY 環境変数を設定してください。")
        print("  export GEMINI_API_KEY='your-api-key'")
        sys.exit(1)
    if genai is None or types is None:
        print("Error: google-genai パッケージが必要です。")
        print("  pip install google-genai")
        sys.exit(1)

    # HTML確認
    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    # キーワード解析
    keyword_info = parse_keyword(args.keyword)
    output_key = resolve_output_key(args.keyword, args.output_key)
    keyword_slug = keyword_to_slug(output_key)
    output_dir = ensure_keyword_images_dir(args.keyword, output_key=output_key)
    variant_profile = build_variant_profile(args.variant_index, args.variant_count)
    variant_instruction = variant_profile["image_style"]

    print(f"Keyword: {args.keyword}")
    print(f"  Genre: {keyword_info.get('genre', '不明')}")
    print(f"  Area: {keyword_info.get('area', 'なし')}")
    if args.output_key:
        print(f"  Output key: {args.output_key}")
    if args.variant_count > 1:
        print(f"  Variant: {args.variant_index}/{args.variant_count}")
    # H2見出し抽出
    h2_headings = extract_h2_headings(args.html)
    article_title = extract_article_title_from_tag_structure(args.html)
    article_type = extract_article_type_from_tag_structure(args.html)
    article_context = extract_article_visual_context(args.html)
    if article_type:
        keyword_info["article_type"] = article_type
    print(f"  H2 headings found: {len(h2_headings)}")
    for i, h in enumerate(h2_headings, 1):
        print(f"    {i}. {h}")
    if article_title:
        print(f"  Article title: {article_title}")
    if article_type:
        print(f"  Article type: {article_type}")

    # Gemini クライアント初期化
    client = genai.Client(api_key=api_key)
    site_style = get_or_create_site_style(client, args.site_config)
    reference_image_path = resolve_reference_image_path(site_style, args.reference_image)
    print(f"  Site style: {site_style.get('name', 'generic-medical-editorial')}")
    if reference_image_path:
        print(f"  Reference image: {reference_image_path}")

    # 画像生成
    results = []

    def image_path(name: str) -> str:
        return os.path.join(output_dir, name)

    def should_generate(name: str, image_kind: str) -> bool:
        if args.force:
            return True
        path = image_path(name)
        needs_regen, reason = should_regenerate_image(path, image_kind)
        if needs_regen and file_exists_case_sensitive(path):
            print(f"  Regenerate {image_kind} image: {name} ({reason})")
        return needs_regen

    # トップ画像
    if args.only is None or args.only == "top":
        filename = f"{keyword_slug}_top.png"
        filepath = image_path(filename)
        if should_generate(filename, "top"):
            prompt = build_top_image_prompt(
                keyword_info,
                h2_headings,
                article_title,
                article_context,
                site_style,
                reference_image_path=reference_image_path,
                variant_instruction=variant_instruction,
            )
            reviewer = lambda path: review_generated_image(path, "top")
            success, audit = generate_with_quality_gate(
                client,
                prompt,
                filename,
                output_dir,
                TOP_ASPECT_RATIO,
                "top",
                reviewer,
                reference_image_path=reference_image_path,
                )
            if not success:
                print(f"  Top image review issues: {audit.get('issues', [])}")
                create_fallback_top_image(filepath, keyword_info, article_title)
                save_image_metadata(
                    filepath,
                    {
                        "signature": current_image_signature("top"),
                        "saved_at": datetime.now().isoformat(),
                        "audit": {"status": "fallback", "issues": audit.get("issues", [])},
                    },
                )
                print(f"  Fallback top image created: {filename}")
                success = True
            if success:
                save_image_metadata(
                    filepath,
                    {
                        "signature": current_image_signature("top"),
                        "saved_at": datetime.now().isoformat(),
                        "audit": audit,
                    },
                )
            results.append(("top", filename, success, "generated"))
            time.sleep(2)  # レートリミット対策
        else:
            print(f"  Skip existing top image: {filename}")
            results.append(("top", filename, True, "reused"))

    # H2画像
    for i, heading in enumerate(h2_headings, 1):
        h2_key = f"h2_{i}"
        if args.only is not None and args.only != h2_key:
            continue

        filename = f"{keyword_slug}_h2_{i}.png"
        if should_generate(filename, "h2"):
            prompt = build_h2_image_prompt(
                heading,
                keyword_info,
                article_context,
                site_style,
                reference_image_path=reference_image_path,
                heading_index=i - 1,
                variant_instruction=variant_instruction,
            )
            reviewer = lambda path: review_generated_image(path, "h2")
            success, audit = generate_with_quality_gate(
                client,
                prompt,
                filename,
                output_dir,
                H2_ASPECT_RATIO,
                "h2",
                reviewer,
                reference_image_path=reference_image_path,
            )
            if not success:
                print(f"  H2 image review issues: {audit.get('issues', [])}")
            if success:
                save_image_metadata(
                    image_path(filename),
                    {
                        "signature": current_image_signature("h2"),
                        "saved_at": datetime.now().isoformat(),
                        "audit": audit,
                    },
                )
            results.append((h2_key, filename, success, "generated"))
            time.sleep(2)  # レートリミット対策
        else:
            print(f"  Skip existing H2 image: {filename}")
            results.append((h2_key, filename, True, "reused"))

    # 結果サマリー
    print("\n" + "=" * 50)
    print("Results:")
    for key, filename, success, mode in results:
        status = "OK" if success else "FAILED"
        print(f"  [{status}] {key}: {filename} ({mode})")

    # HTML挿入
    if not args.skip_insert:
        available_h2_numbers = []
        for key, filename, success, _ in results:
            if not success or not key.startswith("h2_"):
                continue
            match = re.match(r"h2_(\d+)", key)
            if match and file_exists_case_sensitive(image_path(filename)):
                available_h2_numbers.append(int(match.group(1)))
        if available_h2_numbers:
            insert_images_into_html(args.html, keyword_slug, available_h2_numbers)
        else:
            print("\nNo H2 images generated successfully. Skipping HTML insertion.")

    print("\nDone!")


if __name__ == "__main__":
    main()
