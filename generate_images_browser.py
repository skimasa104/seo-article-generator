#!/usr/local/bin/python3.12
"""
ブラウザ操作で記事画像を生成するスクリプト

ChatGPT のブラウザUIを Playwright で開き、既存の記事画像プロンプトを流し込んで
トップ画像 / H2画像を保存する。API は使わない。

使い方:
  python3 generate_images_browser.py --keyword "ED治療 大阪" --html output/ED治療_大阪__nandemo_v1/ED治療_大阪__nandemo_v1_記事.html

初回セットアップ:
  python -m playwright install chromium

補足:
  - ChatGPT へのログインはブラウザ上で行う
  - デフォルトでは .playwright/chatgpt-profile を使う
  - 生成後は companion の _for-wp.html にも画像差し込みを試みる
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

import generate_images as image_logic
from output_utils import ensure_keyword_images_dir, keyword_to_slug, resolve_output_key


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CHAT_URL = "https://chatgpt.com/ja-JP/"
DEFAULT_PROFILE_DIR = SCRIPT_DIR / ".playwright" / "chatgpt-profile"
GENERATION_VERSION = 1
IMAGE_WAIT_SECONDS = 240
IMAGE_STABILIZE_SECONDS = 6
VIEWPORT_WIDTH = 1600
VIEWPORT_HEIGHT = 1400
TOP_FILENAME_SUFFIX = "top"
UI_SETTLE_SECONDS = 1.2
PROMPT_READY_TIMEOUT_MS = 15000
SEND_READY_TIMEOUT_MS = 15000
POST_SEND_WAIT_MS = 3000
MAX_STYLE_VARIANTS = 5
PAGE_READY_TIMEOUT_SECONDS = 30

INTERSTITIAL_TITLE_TOKENS = [
    "しばらくお待ちください",
    "Please stand by",
    "Just a moment",
]

CLOUDFLARE_TITLE_TOKENS = [
    "attention required",
    "just a moment",
    "verify you are human",
]

CLOUDFLARE_VISIBLE_TEXT_TOKENS = [
    "私はロボットではありません",
    "Verify you are human",
    "Cloudflare",
]

CLOUDFLARE_FRAME_URL_TOKENS = [
    "challenges.cloudflare.com",
    "challenge-platform",
    "turnstile",
]

PROMPT_SELECTORS = [
    "#prompt-textarea",
    "[data-testid='composer-text-input']",
    "textarea[placeholder*='メッセージ']",
    "textarea[placeholder*='Message']",
    "textarea[data-id='root']",
    "form [contenteditable='true'][role='textbox']",
    "form div.ProseMirror[contenteditable='true']",
    "form [data-lexical-editor='true']",
    "div.ProseMirror[contenteditable='true']",
    "[data-lexical-editor='true']",
    "[contenteditable='true'][role='textbox']",
    "div[contenteditable='true']",
    "textarea",
]

SEND_BUTTON_SELECTORS = [
    "button[data-testid='send-button']",
    "button[aria-label*='Send']",
    "button[aria-label*='送信']",
]

SIDEBAR_IMAGE_MODE_SELECTORS = [
    "nav a:has-text('画像')",
    "nav button:has-text('画像')",
    "aside a:has-text('画像')",
    "aside button:has-text('画像')",
    "[role='navigation'] a:has-text('画像')",
    "[role='navigation'] button:has-text('画像')",
    "[data-testid*='sidebar'] a:has-text('画像')",
    "[data-testid*='sidebar'] button:has-text('画像')",
    "[aria-label='画像']",
    "text=画像",
    "a:has-text('画像')",
    "button:has-text('画像')",
]

IMAGE_MODE_DIRECT_SELECTORS = [
    "button[aria-label*='画像を作成']",
    "button[aria-label*='Create image']",
    "[role='button'][aria-label*='画像を作成']",
    "[role='button'][aria-label*='Create image']",
]

TOOLS_BUTTON_SELECTORS = [
    "button[aria-label*='ツール']",
    "button[aria-label*='Tools']",
    "[role='button'][aria-label*='ツール']",
    "[role='button'][aria-label*='Tools']",
]

IMAGE_MODE_MENU_SELECTORS = [
    "[role='menuitem']",
    "[role='option']",
    "button",
    "[role='button']",
]

BROWSER_VARIANT_STYLES = {
    1: {
        "name": "V1",
        "top": "写真ベースの王道サムネ。青空と都市背景、強い白フチ見出し、金色の数字強調、悩む人物か医師相談シーンを大きく入れる。",
        "h2": "V1の派生。写真ベースで力強く、見出し文字は大きく少なめ。人物かモチーフを1つ主役にして瞬時に内容が伝わる構図。",
    },
    2: {
        "name": "V2",
        "top": "上品で整理された青×白のエディトリアル調。斜めパネルや余白を使い、都市景観を控えめに入れ、文字組みは端正にする。",
        "h2": "V2の派生。白い面を広めに取り、青系でミニマルにまとめる。図版やラベルは少数に絞り、読みやすさを優先する。",
    },
    3: {
        "name": "V3",
        "top": "やわらかいフラットイラスト。比較図解や丸みのあるパネルを使い、緑・青・オレンジの明るい配色で親しみやすく見せる。",
        "h2": "V3の派生。図解寄りのやさしいイラストで、チェック・比較・オンライン診療などをわかりやすく表す。",
    },
    4: {
        "name": "V4",
        "top": "明るい写真ベースで余白多め。落ち着いた青系と白を基調に、考える人物1人と小さなアイコンで清潔感を出す。",
        "h2": "V4の派生。写真ベースで静かなトーン。人物は1人まで、情報は絞り、H2直下で邪魔にならない軽さにする。",
    },
    5: {
        "name": "V5",
        "top": "情報訴求が強い写真サムネ。駅前や街並み背景、ネイビー×ゴールドの強い文字、位置ラベルや比較帯を活かす。",
        "h2": "V5の派生。写真ベースで都市感を入れつつ、帯やラベルを使って要点を整理する。勢いはあるが詰め込みすぎない。",
    },
}


def current_image_signature(image_kind: str) -> dict[str, Any]:
    return {
        "image_kind": image_kind,
        "generator_mode": "chatgpt-browser",
        "generation_version": GENERATION_VERSION,
    }


def get_image_metadata_path(image_path: str) -> str:
    return f"{image_path}.meta.json"


def file_exists_case_sensitive(filepath: str) -> bool:
    path = Path(filepath)
    parent = path.parent
    if not parent.exists():
        return False
    return path.name in set(os.listdir(parent))


def load_image_metadata(image_path: str) -> dict[str, Any] | None:
    meta_path = get_image_metadata_path(image_path)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_image_metadata(image_path: str, payload: dict[str, Any]) -> None:
    meta_path = get_image_metadata_path(image_path)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def should_regenerate_image(image_path: str, image_kind: str) -> tuple[bool, str]:
    if not file_exists_case_sensitive(image_path):
        return True, "missing_file"

    metadata = load_image_metadata(image_path)
    if not metadata:
        return True, "missing_metadata"

    saved_signature = metadata.get("signature") or {}
    if saved_signature != current_image_signature(image_kind):
        return True, "signature_changed"

    return False, "up_to_date"


def build_chat_request(prompt: str) -> str:
    return (
        "以下の条件で画像を1枚だけ生成してください。"
        "返答文は不要です。画像生成のみ行ってください。\n\n"
        f"{prompt}"
    )


def get_browser_variant_style(variant_index: int) -> dict[str, str]:
    index = max(1, min(MAX_STYLE_VARIANTS, int(variant_index or 1)))
    return BROWSER_VARIANT_STYLES[index]


def build_browser_copy_line(*parts: str) -> str:
    values = [part.strip() for part in parts if part and part.strip()]
    return " / ".join(values)


def describe_h2_focus_short(heading: str) -> str:
    if any(token in heading for token in ["料金", "費用", "相場", "価格"]):
        return "料金比較や費用感がひと目で伝わる構図"
    if any(token in heading for token in ["オンライン", "診療"]):
        return "オンライン診療や通院比較が伝わる構図"
    if any(token in heading for token in ["選び方", "ポイント", "チェック"]):
        return "選び方やチェックポイントを図解風に見せる構図"
    if any(token in heading for token in ["植毛", "注入", "治療法", "治療"]):
        return "治療法の違いが伝わる医療ビジュアル"
    if any(token in heading for token in ["口コミ", "評判"]):
        return "評判や比較検討の雰囲気が伝わる構図"
    if any(token in heading for token in ["まとめ", "結論"]):
        return "要点整理や最終比較を想起させる構図"
    return "見出しテーマが直感的に伝わる構図"


def build_browser_top_prompt(
    keyword_info: dict[str, Any],
    h2_headings: list[str],
    *,
    article_title: str,
    variant_index: int,
) -> str:
    style = get_browser_variant_style(variant_index)
    title_copy, subtitle_copy = image_logic.get_top_image_copy(
        keyword_info,
        h2_headings,
        article_title=article_title,
    )
    area = keyword_info.get("area", "").strip()
    genre = keyword_info.get("genre", "").strip() or "AGA記事"
    copy_text = build_browser_copy_line(title_copy, subtitle_copy)

    parts = [
        f"目的: {area or '地域名入り'}の{genre}比較記事のトップ画像。",
        "用途: WordPress記事のサムネイル兼、導入直下のメイン画像。",
        f"スタイル: {style['name']}。{style['top']}",
        f"入れたいコピー: {copy_text}",
        "条件: 16:9、日本語のみ、文字は大きく少なめ、追加の長文や余計な装飾は入れない。",
    ]
    return "\n".join(parts)


def is_interstitial_title(title: str) -> bool:
    normalized = (title or "").strip()
    return any(token in normalized for token in INTERSTITIAL_TITLE_TOKENS)


def safe_page_title(page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def frame_has_visible_text(frame, token: str) -> bool:
    locator = frame.locator(f"text={token}").first
    try:
        return locator.is_visible(timeout=300)
    except Exception:
        return False


def is_cloudflare_challenge_active(page) -> bool:
    title = safe_page_title(page).strip().lower()
    if any(token in title for token in CLOUDFLARE_TITLE_TOKENS):
        return True

    try:
        for token in CLOUDFLARE_FRAME_URL_TOKENS:
            if token in (page.url or "").lower():
                return True
    except Exception:
        pass

    try:
        frames = page.frames
    except Exception:
        frames = []

    for frame in frames:
        try:
            frame_url = (frame.url or "").lower()
        except Exception:
            frame_url = ""
        if any(token in frame_url for token in CLOUDFLARE_FRAME_URL_TOKENS):
            return True
        for token in CLOUDFLARE_VISIBLE_TEXT_TOKENS:
            if frame_has_visible_text(frame, token):
                return True

    return False


def wait_for_cloudflare_challenge(page, timeout_seconds: int = 300) -> None:
    if not is_cloudflare_challenge_active(page):
        return

    print("\nCloudflare の本人確認画面を検出しました。")
    print("ブラウザ上で「私はロボットではありません」などの確認を完了してください。")
    input("通過できたら Enter を押してください...")

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_cloudflare_challenge_active(page):
            short_pause(1.0)
            return
        time.sleep(0.5)

    raise RuntimeError("Cloudflare の確認画面が解除されませんでした。")


def wait_for_chat_page_ready(page, timeout_seconds: int = PAGE_READY_TIMEOUT_SECONDS) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    wait_for_cloudflare_challenge(page, timeout_seconds=max(timeout_seconds, 120))

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_cloudflare_challenge_active(page):
            wait_for_cloudflare_challenge(page, timeout_seconds=max(timeout_seconds, 120))
        title = safe_page_title(page)
        if title and not is_interstitial_title(title):
            return
        time.sleep(0.5)


def build_browser_h2_prompt(
    heading: str,
    keyword_info: dict[str, Any],
    *,
    variant_index: int,
) -> str:
    style = get_browser_variant_style(variant_index)
    area = keyword_info.get("area", "").strip()
    genre = keyword_info.get("genre", "").strip() or "AGA記事"
    copy_text = image_logic.get_h2_display_copy(heading)
    focus = describe_h2_focus_short(heading)

    parts = [
        f"目的: {area or '地域名入り'}の{genre}記事内、H2セクション「{heading}」の画像。",
        "用途: H2直下に差し込む横長アイキャッチ。トップ画像と同じ世界観で、本文の邪魔をしないこと。",
        f"スタイル: {style['name']}。{style['h2']}",
        f"見せ方: {focus}",
        f"入れたいコピー: {copy_text}",
        "条件: 16:9、日本語のみ、文字量は少なめ、1テーマ1メッセージで整理する。",
    ]
    return "\n".join(parts)


def build_jobs(
    keyword: str,
    html_path: str,
    *,
    output_key: str | None,
    variant_index: int,
    variant_count: int,
    only: str | None,
) -> tuple[list[dict[str, Any]], str]:
    keyword_info = image_logic.parse_keyword(keyword)
    output_key_resolved = resolve_output_key(keyword, output_key)
    keyword_slug = keyword_to_slug(output_key_resolved)
    h2_headings = image_logic.extract_h2_headings(html_path)
    article_title = image_logic.extract_article_title_from_tag_structure(html_path)
    article_type = image_logic.extract_article_type_from_tag_structure(html_path)

    if article_type:
        keyword_info["article_type"] = article_type

    jobs: list[dict[str, Any]] = []

    if only in (None, "top"):
        jobs.append(
            {
                "kind": "top",
                "filename": f"{keyword_slug}_{TOP_FILENAME_SUFFIX}.png",
                "aspect_ratio": image_logic.TOP_ASPECT_RATIO,
                "prompt": build_browser_top_prompt(
                    keyword_info,
                    h2_headings,
                    article_title=article_title,
                    variant_index=variant_index,
                ),
                "heading_index": None,
                "heading_text": article_title or keyword,
            }
        )

    for index, heading in enumerate(h2_headings, start=1):
        only_name = f"h2_{index}"
        if only is not None and only != only_name:
            continue
        jobs.append(
            {
                "kind": "h2",
                "filename": f"{keyword_slug}_h2_{index}.png",
                "aspect_ratio": image_logic.H2_ASPECT_RATIO,
                "prompt": build_browser_h2_prompt(
                    heading,
                    keyword_info,
                    variant_index=variant_index,
                ),
                "heading_index": index,
                "heading_text": heading,
            }
        )

    return jobs, keyword_slug


def find_prompt_locator(page):
    for selector in PROMPT_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=500):
                return locator
        except Exception:
            continue
    return None


def wait_for_prompt_locator(page, timeout_seconds: float = 10.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        locator = find_prompt_locator(page)
        if locator:
            return locator
        time.sleep(0.4)
    return None


def ensure_prompt_ready(page) -> Any:
    locator = wait_for_prompt_locator(page)
    if locator:
        return locator

    if is_cloudflare_challenge_active(page):
        wait_for_cloudflare_challenge(page)
        locator = wait_for_prompt_locator(page)
        if locator:
            return locator

    page_title = safe_page_title(page)
    print(f"\nPrompt debug: url={page.url} title={page_title}")
    labels = collect_visible_labels(
        page,
        ["button", "a", "[role='button']", "[role='link']"],
        limit=16,
    )
    if labels:
        print(f"Prompt debug: visible labels -> {labels}")

    print("\nChatGPT に未ログインか、入力欄が見つかりません。")
    input("ブラウザ上でログインし、入力欄が見える状態にして Enter を押してください...")
    locator = wait_for_prompt_locator(page)
    if not locator:
        raise RuntimeError("ChatGPT の入力欄が見つかりませんでした。")
    return locator


def short_pause(seconds: float = UI_SETTLE_SECONDS) -> None:
    time.sleep(seconds)


def locator_has_text(locator, expected_texts: list[str]) -> bool:
    try:
        text = locator.inner_text(timeout=500).strip()
    except Exception:
        return False
    return any(token in text for token in expected_texts)


def collect_visible_labels(page, selectors: list[str], *, limit: int = 12) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), limit)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if not item.is_visible(timeout=200):
                    continue
                text = item.inner_text(timeout=300).strip()
            except Exception:
                continue
            if not text or text in seen:
                continue
            seen.add(text)
            labels.append(text.replace("\n", " / "))
            if len(labels) >= limit:
                return labels
    return labels


def click_first_visible(page, selectors: list[str], *, require_enabled: bool = False) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if not locator.is_visible(timeout=700):
                continue
            if require_enabled and not locator.is_enabled():
                continue
            locator.click()
            return True
        except Exception:
            continue
    return False


def ensure_image_mode(page) -> bool:
    if click_first_visible(page, SIDEBAR_IMAGE_MODE_SELECTORS):
        short_pause(1.8)
        wait_for_chat_page_ready(page, timeout_seconds=12)
        print("  Image mode: sidebar item clicked")
        return True

    if click_first_visible(page, IMAGE_MODE_DIRECT_SELECTORS):
        short_pause()
        wait_for_chat_page_ready(page, timeout_seconds=12)
        print("  Image mode: direct button clicked")
        return True

    if click_first_visible(page, TOOLS_BUTTON_SELECTORS):
        short_pause()
        menu_candidates = page.locator(",".join(IMAGE_MODE_MENU_SELECTORS))
        count = min(menu_candidates.count(), 30)
        for index in range(count):
            locator = menu_candidates.nth(index)
            try:
                if not locator.is_visible(timeout=300):
                    continue
                if locator_has_text(locator, ["画像を作成", "Create image", "画像", "Image"]):
                    locator.click()
                    short_pause()
                    wait_for_chat_page_ready(page, timeout_seconds=12)
                    print("  Image mode: tools menu item clicked")
                    return True
            except Exception:
                continue

    nav_labels = collect_visible_labels(
        page,
        [
            "nav a",
            "nav button",
            "aside a",
            "aside button",
            "[role='navigation'] a",
            "[role='navigation'] button",
        ],
    )
    if nav_labels:
        print(f"  Image mode debug: visible nav labels -> {nav_labels}")

    print("  Image mode: no dedicated control detected, continuing with current mode")
    return False


def clear_and_fill_prompt(page, locator, prompt: str) -> None:
    tag_name = locator.evaluate("el => el.tagName.toLowerCase()")
    if tag_name == "textarea":
        locator.click()
        short_pause(0.3)
        locator.fill("")
        short_pause(0.2)
        locator.fill(prompt)
        short_pause(0.4)
        return

    locator.click()
    short_pause(0.3)
    page.keyboard.press("Meta+a")
    short_pause(0.2)
    page.keyboard.press("Backspace")
    short_pause(0.2)
    page.keyboard.insert_text(prompt)
    short_pause(0.4)


def wait_for_prompt_text(page, locator, prompt: str) -> None:
    expected_prefix = prompt[: min(len(prompt), 24)]
    deadline = time.time() + (PROMPT_READY_TIMEOUT_MS / 1000)

    while time.time() < deadline:
        try:
            tag_name = locator.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == "textarea":
                value = locator.input_value(timeout=500)
            else:
                value = locator.inner_text(timeout=500)
            if expected_prefix and expected_prefix in value:
                return
        except Exception:
            pass
        time.sleep(0.3)

    raise RuntimeError("プロンプトが入力欄へ安定して反映されませんでした。")


def wait_for_send_ready(page) -> bool:
    deadline = time.time() + (SEND_READY_TIMEOUT_MS / 1000)

    while time.time() < deadline:
        for selector in SEND_BUTTON_SELECTORS:
            button = page.locator(selector).first
            try:
                if button.is_visible(timeout=300) and button.is_enabled():
                    return True
            except Exception:
                continue
        time.sleep(0.3)

    return False


def send_prompt(page, locator) -> None:
    if wait_for_send_ready(page):
        for selector in SEND_BUTTON_SELECTORS:
            button = page.locator(selector).first
            try:
                if button.is_visible(timeout=500) and button.is_enabled():
                    button.click()
                    page.wait_for_timeout(POST_SEND_WAIT_MS)
                    print("  Prompt send: send button clicked")
                    return
            except Exception:
                continue

    locator.press("Enter")
    page.wait_for_timeout(POST_SEND_WAIT_MS)
    print("  Prompt send: Enter pressed")


def verify_prompt_sent(page, before_signatures: set[str], locator) -> bool:
    try:
        tag_name = locator.evaluate("el => el.tagName.toLowerCase()")
        if tag_name == "textarea":
            value = locator.input_value(timeout=500)
        else:
            value = locator.inner_text(timeout=500)
        if value.strip():
            return False
    except Exception:
        pass

    try:
        current = {item["signature"] for item in snapshot_images(page)}
        if current != before_signatures:
            return True
    except Exception:
        pass

    return True


def snapshot_images(page) -> list[dict[str, Any]]:
    script = """
    () => {
      const nodes = Array.from(document.querySelectorAll("main img"));
      return nodes
        .map((img, idx) => {
          const rect = img.getBoundingClientRect();
          return {
            idx,
            src: img.currentSrc || img.src || "",
            alt: img.alt || "",
            width: Math.round(rect.width || 0),
            height: Math.round(rect.height || 0),
            naturalWidth: img.naturalWidth || 0,
            naturalHeight: img.naturalHeight || 0,
          };
        })
        .filter(item => item.width >= 180 && item.height >= 180 && item.naturalWidth >= 256);
    }
    """
    descriptors = page.evaluate(script)
    for item in descriptors:
        item["signature"] = f"{item['idx']}|{item['src']}|{item['naturalWidth']}|{item['naturalHeight']}"
    return descriptors


def wait_for_new_image(page, before_signatures: set[str], timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    detected: dict[str, Any] | None = None

    while time.time() < deadline:
        descriptors = snapshot_images(page)
        new_items = [item for item in descriptors if item["signature"] not in before_signatures]
        if new_items:
            detected = new_items[-1]
            break
        time.sleep(2)

    if detected is None:
        raise TimeoutError("新しい画像を検出できませんでした。")

    time.sleep(IMAGE_STABILIZE_SECONDS)
    descriptors = snapshot_images(page)
    for item in reversed(descriptors):
        if item["src"] == detected["src"] or item["signature"] == detected["signature"]:
            return item
    return detected


def decode_data_url(data_url: str) -> bytes:
    _, encoded = data_url.split(",", 1)
    return base64.b64decode(encoded)


def blob_url_to_bytes(locator) -> bytes:
    encoded = locator.evaluate(
        """async (img) => {
            const response = await fetch(img.currentSrc || img.src);
            const blob = await response.blob();
            const buffer = await blob.arrayBuffer();
            let binary = "";
            const bytes = new Uint8Array(buffer);
            const chunkSize = 0x8000;
            for (let i = 0; i < bytes.length; i += chunkSize) {
              const chunk = bytes.subarray(i, i + chunkSize);
              binary += String.fromCharCode(...chunk);
            }
            return btoa(binary);
        }"""
    )
    return base64.b64decode(encoded)


def bytes_to_png(raw: bytes, output_path: str) -> None:
    with Image.open(io_bytes(raw)) as image:
        converted = image.convert("RGBA")
        converted.save(output_path, format="PNG")


def io_bytes(raw: bytes):
    from io import BytesIO

    return BytesIO(raw)


def save_image_from_locator(page, descriptor: dict[str, Any], output_path: str) -> None:
    candidates = page.locator("main img")
    locator = candidates.nth(descriptor["idx"])

    src = descriptor.get("src", "")
    raw: bytes | None = None

    if src.startswith("data:"):
        raw = decode_data_url(src)
    elif src.startswith("blob:"):
        raw = blob_url_to_bytes(locator)
    elif src.startswith("http://") or src.startswith("https://"):
        raw = locator.evaluate(
            """async (img) => {
                const response = await fetch(img.currentSrc || img.src, { credentials: "include" });
                const buffer = await response.arrayBuffer();
                let binary = "";
                const bytes = new Uint8Array(buffer);
                const chunkSize = 0x8000;
                for (let i = 0; i < bytes.length; i += chunkSize) {
                  const chunk = bytes.subarray(i, i + chunkSize);
                  binary += String.fromCharCode(...chunk);
                }
                return btoa(binary);
            }"""
        )
        raw = base64.b64decode(raw)

    if raw:
        try:
            bytes_to_png(raw, output_path)
            return
        except Exception:
            raw = None

    locator.screenshot(path=output_path)


def resolve_companion_html_paths(html_path: str) -> list[str]:
    paths = [html_path]
    if html_path.endswith("_記事.html"):
        companion = html_path.replace("_記事.html", "_記事_for-wp.html")
        if os.path.exists(companion):
            paths.append(companion)
    return paths


def launch_browser(
    playwright,
    user_data_dir: str,
    browser_channel: str | None,
    cdp_url: str | None = None,
) -> dict[str, Any]:
    if cdp_url:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError(
                "CDP接続先のブラウザに既存コンテキストがありません。"
                "リモートデバッグ付きで通常のChromeプロファイルを起動してください。"
            )
        context = browser.contexts[0]
        page = context.new_page()
        return {
            "mode": "cdp",
            "browser": browser,
            "context": context,
            "page": page,
            "close_target": page,
        }

    launch_kwargs = {
        "headless": False,
        "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    }
    if browser_channel and browser_channel != "chromium":
        launch_kwargs["channel"] = browser_channel

    context = playwright.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        **launch_kwargs,
    )
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', {
          get: () => undefined,
        });
        Object.defineProperty(navigator, 'languages', {
          get: () => ['ja-JP', 'ja', 'en-US', 'en'],
        });
        Object.defineProperty(navigator, 'plugins', {
          get: () => [1, 2, 3, 4, 5],
        });
        """
    )
    page = context.new_page()
    return {
        "mode": "persistent",
        "browser": None,
        "context": context,
        "page": page,
        "close_target": context,
    }


def main():
    parser = argparse.ArgumentParser(description="ChatGPTブラウザUIで記事画像を生成する")
    parser.add_argument("--keyword", required=True, help="検索キーワード")
    parser.add_argument("--html", required=True, help="記事HTMLファイル")
    parser.add_argument("--chat-url", default=DEFAULT_CHAT_URL, help="ChatGPTのURL")
    parser.add_argument("--user-data-dir", default=str(DEFAULT_PROFILE_DIR), help="Playwright用ユーザーデータディレクトリ")
    parser.add_argument("--browser-channel", choices=["chrome", "msedge", "chromium"], default="chrome", help="起動するブラウザ")
    parser.add_argument("--cdp-url", help="既存ChromeへCDP接続するURL。例: http://127.0.0.1:9222")
    parser.add_argument("--skip-insert", action="store_true", help="HTMLへの画像差し込みをスキップ")
    parser.add_argument("--only", help="top または h2_1 のように指定して一部だけ生成")
    parser.add_argument("--force", action="store_true", help="既存画像があっても再生成する")
    parser.add_argument("--output-key", help="出力先の識別キー")
    parser.add_argument("--variant-index", type=int, default=1, help="編集バリエーション番号")
    parser.add_argument("--variant-count", type=int, default=1, help="生成する総バリエーション数")
    parser.add_argument("--timeout", type=int, default=IMAGE_WAIT_SECONDS, help="1枚あたりの待機秒数")
    parser.add_argument("--allow-top-fallback", action="store_true", help="トップ画像失敗時にフォールバック画像を生成する")
    args = parser.parse_args()

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    jobs, keyword_slug = build_jobs(
        args.keyword,
        args.html,
        output_key=args.output_key,
        variant_index=args.variant_index,
        variant_count=args.variant_count,
        only=args.only,
    )
    if not jobs:
        print("生成対象の画像がありません。")
        return

    output_dir = ensure_keyword_images_dir(args.keyword, output_key=args.output_key)
    print(f"Output dir: {output_dir}")
    print(f"Jobs: {len(jobs)}")
    for job in jobs:
        print(f"  - {job['filename']}")

    generated_h2_numbers: list[int] = []

    with sync_playwright() as playwright:
        session = launch_browser(
            playwright,
            args.user_data_dir,
            args.browser_channel,
            cdp_url=args.cdp_url,
        )
        context = session["context"]
        page = session["page"]
        page.goto(args.chat_url, wait_until="domcontentloaded", timeout=60000)
        wait_for_chat_page_ready(page)
        if session["mode"] == "cdp":
            print("\n既存Chromeへ接続しました。現在のGoogleアカウント状態のまま新しいタブで操作します。")
        print("\nChatGPT 側は画像生成できるモデルに切り替えてから使ってください。")

        for job in jobs:
            output_path = os.path.join(output_dir, job["filename"])
            if not args.force:
                should_generate, reason = should_regenerate_image(output_path, job["kind"])
                if not should_generate:
                    print(f"Skip {job['filename']}: {reason}")
                    if job["kind"] == "h2" and job["heading_index"] is not None:
                        generated_h2_numbers.append(job["heading_index"])
                    continue

            prompt = build_chat_request(job["prompt"])
            print(f"\nGenerating {job['filename']} ...")
            before_signatures = {item["signature"] for item in snapshot_images(page)}
            ensure_image_mode(page)
            prompt_locator = ensure_prompt_ready(page)
            clear_and_fill_prompt(page, prompt_locator, prompt)
            wait_for_prompt_text(page, prompt_locator, prompt)
            send_prompt(page, prompt_locator)
            if not verify_prompt_sent(page, before_signatures, prompt_locator):
                print("  Prompt send check failed: input seems to remain unsent")
                continue

            try:
                descriptor = wait_for_new_image(page, before_signatures, args.timeout)
                save_image_from_locator(page, descriptor, output_path)
            except TimeoutError as exc:
                print(f"  Failed: {exc}")
                if job["kind"] == "top" and args.allow_top_fallback:
                    print("  Top画像はフォールバック画像を生成します。")
                    keyword_info = image_logic.parse_keyword(args.keyword)
                    article_title = image_logic.extract_article_title_from_tag_structure(args.html)
                    image_logic.create_fallback_top_image(output_path, keyword_info, article_title=article_title)
                else:
                    continue
            except PlaywrightTimeoutError as exc:
                print(f"  Browser timeout: {exc}")
                continue
            except Exception as exc:
                print(f"  Error while saving image: {exc}")
                continue

            audit = image_logic.review_generated_image(output_path, job["kind"])
            save_image_metadata(
                output_path,
                {
                    "generated_at": datetime.now().isoformat(),
                    "prompt": job["prompt"],
                    "chat_request": prompt,
                    "signature": current_image_signature(job["kind"]),
                    "audit": audit,
                    "source": {
                        "chat_url": args.chat_url,
                        "browser_channel": args.browser_channel,
                        "browser_mode": session["mode"],
                        "cdp_url": args.cdp_url,
                    },
                },
            )
            print(f"  Saved: {output_path}")
            if not audit.get("ok"):
                print(f"  Review warning: {', '.join(audit.get('issues', []))}")

            if job["kind"] == "h2" and job["heading_index"] is not None:
                generated_h2_numbers.append(job["heading_index"])

        if not args.skip_insert and generated_h2_numbers:
            for target_html in resolve_companion_html_paths(args.html):
                image_logic.insert_images_into_html(target_html, keyword_slug, sorted(set(generated_h2_numbers)))

        if session["mode"] == "cdp":
            print("\n完了しました。既存ブラウザ本体は閉じず、この作業タブだけ後で閉じます。")
            input("確認できたら Enter を押して、この作業タブを閉じます...")
        else:
            print("\n完了しました。ブラウザはログイン状態保持のため開いたままです。")
            input("確認できたら Enter を押してブラウザを閉じます...")
        session["close_target"].close()


if __name__ == "__main__":
    main()
