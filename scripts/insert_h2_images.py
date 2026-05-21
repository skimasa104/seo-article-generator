"""H2配下に画像を自動挿入するスクリプト

仕様の詳細は docs/ARTICLE_RULES.md §「H2画像振り分け」を参照。

フロー:
  1. ローカル画像(--images)を Claude Vision で読み取り「画像内コピー」を抽出
  2. WPメディアにアップロード（SHA-256キャッシュで重複アップ防止）
  3. 記事HTML(--article)を読み込み、H2見出しを抽出
  4. 各画像コピー × 各H2 タイトルを Claude に渡して最適マッチを決定
  5. 記事HTMLの該当H2直下に <figure><img wp-image-XXXX> を挿入
  6. build_for_wp.py を呼び出して _for-wp.html を再生成
  7. 記事タイトルでWP下書きを検索 → 本文を update_post で更新

承認ステップは挟まずに全自動で進める（ARTICLE_RULES.md §H2画像振り分けに準拠）。

使い方:
  python3 scripts/insert_h2_images.py \\
    --article "output/ED治療_名古屋__nandemo_v1/ED治療_名古屋__nandemo_v1_記事.html" \\
    --site sites/nandemo.json \\
    --images ~/Downloads/img1.png ~/Downloads/img2.png ...

  python3 scripts/insert_h2_images.py --help
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from wp_post import WordPressClient  # noqa: E402

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_VISION_MODEL = "claude-opus-4-7"
CACHE_FILE = ROOT_DIR / ".image_upload_cache.json"


# ----------------------------------------------------------------------
# 共通ユーティリティ
# ----------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def load_api_key() -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        env_path = ROOT_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip("'\"")
                    break
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY を .env か環境変数に設定してください")
    return api_key


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------------
# Claude Vision
# ----------------------------------------------------------------------
def claude_call(api_key: str, body: dict, *, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Claude API error: {e.code} {e.read().decode()[:500]}")


def extract_image_copy(api_key: str, image_path: Path) -> str:
    """画像内のテキスト要素を Claude Vision で抽出"""
    data = image_path.read_bytes()
    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime:
        mime = "image/png"
    b64 = base64.standard_b64encode(data).decode("ascii")

    resp = claude_call(
        api_key,
        {
            "model": CLAUDE_VISION_MODEL,
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": b64},
                        },
                        {
                            "type": "text",
                            "text": (
                                "この画像に含まれる文字（タイトル・コピー・小見出し・"
                                "ラベル）をすべて読み取り、改行区切りで返してください。"
                                "装飾や説明は不要、文字だけを抽出してください。"
                            ),
                        },
                    ],
                }
            ],
        },
    )
    return resp["content"][0]["text"].strip()


def match_images_to_h2s(
    api_key: str, image_copies: list[dict], h2_titles: list[str]
) -> dict[int, int]:
    """画像コピーとH2タイトルをClaudeに渡して最適マッチを取得

    Returns: {h2_index: image_index} のdict
    """
    images_block = "\n".join(
        f"[画像{i}]\n{c['copy']}" for i, c in enumerate(image_copies)
    )
    h2_block = "\n".join(f"[H2-{i}] {t}" for i, t in enumerate(h2_titles))

    prompt = f"""以下の画像コピーとH2見出しを、内容が最も近いペアでマッチングしてください。

# 画像
{images_block}

# H2見出し
{h2_block}

# 出力フォーマット
JSONのみを出力。フォーマットは {{"matches": [{{"h2_index": 0, "image_index": 3}}, ...]}} 。
- 各画像は最大1つのH2にしか割り当てない
- 各H2は最大1つの画像しか持たない
- マッチしない画像・H2はリストから除外する
- 内容が明らかに似ているペアのみを含める。曖昧なペアは含めない"""

    resp = claude_call(
        api_key,
        {
            "model": CLAUDE_VISION_MODEL,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    text = resp["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        sys.exit(f"Claude が JSON を返しませんでした:\n{text}")
    data = json.loads(m.group(0))
    return {pair["h2_index"]: pair["image_index"] for pair in data["matches"]}


# ----------------------------------------------------------------------
# WP メディアアップロード（重複スキップ付き）
# ----------------------------------------------------------------------
def upload_with_cache(
    client: WordPressClient, site_url: str, image_path: Path, cache: dict
) -> dict:
    """SHA-256キャッシュでスキップ、なければアップロード"""
    sha = sha256_of(image_path)
    key = f"{site_url}::{sha}"
    if key in cache:
        log(f"  cache hit: {image_path.name} -> media_id={cache[key]['id']}")
        return cache[key]

    log(f"  uploading: {image_path.name}")
    media = client.upload_media(str(image_path))
    if not media:
        sys.exit(f"Upload failed: {image_path}")
    entry = {"id": media["id"], "url": media["url"], "filename": image_path.name}
    cache[key] = entry
    save_cache(cache)
    return entry


# ----------------------------------------------------------------------
# 記事HTMLパース＆挿入
# ----------------------------------------------------------------------
H2_RE = re.compile(
    r'<h2(?P<attrs>[^>]*)>(?P<text>.*?)</h2>', re.DOTALL
)


def extract_h2_titles(html: str) -> list[tuple[int, str]]:
    """H2タイトル一覧を (出現順index, plain_text) で返す。
    タグを除去したテキスト比較用と、HTML置換用に位置を保持する。
    """
    results = []
    for i, m in enumerate(H2_RE.finditer(html)):
        text = re.sub(r"<[^>]+>", "", m.group("text")).strip()
        results.append((i, text))
    return results


def insert_image_after_h2(html: str, h2_index: int, img_html: str) -> str:
    """h2_index番目のH2の直後にimg_htmlを挿入"""
    matches = list(H2_RE.finditer(html))
    if h2_index >= len(matches):
        return html
    m = matches[h2_index]
    insert_at = m.end()
    return html[:insert_at] + "\n" + img_html + html[insert_at:]


def build_figure_html(media_url: str, media_id: int, alt: str) -> str:
    return (
        f'<figure class="wp-block-image size-large">'
        f'<img src="{media_url}" alt="{alt}" class="wp-image-{media_id}" />'
        f"</figure>"
    )


# ----------------------------------------------------------------------
# build_for_wp.py 呼び出し
# ----------------------------------------------------------------------
def rebuild_for_wp(article_path: Path) -> Path:
    log(f"  rebuilding _for-wp.html ...")
    subprocess.run(
        ["python3", "build_for_wp.py", "--html", str(article_path)],
        cwd=ROOT_DIR,
        check=True,
    )
    for_wp = article_path.with_name(article_path.stem + "_for-wp.html")
    if not for_wp.exists():
        sys.exit(f"_for-wp.html が見つかりません: {for_wp}")
    return for_wp


# ----------------------------------------------------------------------
# WP下書き検索＆更新
# ----------------------------------------------------------------------
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
TAG_STRUCTURE_TITLE_RE_TABLE = re.compile(r"\|\s*\*\*titleタグ\*\*\s*\|\s*(.+?)\s*\|")
TAG_STRUCTURE_TITLE_RE_PLAIN = re.compile(r"\*\*titleタグ\*\*[:：]\s*(.+)")


def find_tag_structure_path(article_path: Path) -> Path | None:
    """記事HTMLパスから対応する タグ構成.md を探す。
    例: 包茎_福岡__nandemo_v1_記事.html → 包茎_福岡__nandemo_v1_タグ構成.md
    """
    stem = article_path.stem
    # 末尾の _記事 を取り除く（無くてもOK）
    base = re.sub(r"_記事$", "", stem)
    candidate = article_path.with_name(f"{base}_タグ構成.md")
    return candidate if candidate.exists() else None


def extract_title_from_tag_structure(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        m = TAG_STRUCTURE_TITLE_RE_TABLE.match(stripped) or TAG_STRUCTURE_TITLE_RE_PLAIN.match(stripped)
        if m:
            return m.group(1).strip()
    return None


def extract_article_title(article_path: Path, article_html: str) -> str:
    """記事タイトルを優先順位付きで抽出:
    1. 同フォルダの タグ構成.md の **titleタグ**（最も正確）
    2. _for-wp.html の <title> タグ（build_for_wp.py が埋め込む）
    3. 記事HTML内の <title> タグ
    4. 最初のH1
    （最初のH2は使わない — 本文先頭H2は記事タイトルとは別物のことが多い）
    """
    tag_path = find_tag_structure_path(article_path)
    if tag_path:
        title = extract_title_from_tag_structure(tag_path)
        if title:
            log(f"  title (from {tag_path.name}): {title}")
            return title

    for_wp = article_path.with_name(article_path.stem + "_for-wp.html")
    if for_wp.exists():
        m = TITLE_RE.search(for_wp.read_text(encoding="utf-8"))
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
            log(f"  title (from {for_wp.name} <title>): {title}")
            return title

    m = TITLE_RE.search(article_html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        log(f"  title (from article <title>): {title}")
        return title

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", article_html, re.DOTALL)
    if h1:
        title = re.sub(r"<[^>]+>", "", h1.group(1)).strip()
        log(f"  title (from <h1>): {title}")
        return title

    sys.exit(
        "記事タイトルが特定できませんでした。タグ構成.md が無い・<title>も無い場合は "
        "--wp-post-id を明示してください。"
    )


def find_wp_draft(client: WordPressClient, title: str) -> int:
    """記事タイトルで下書きを検索"""
    keyword = title.split("｜")[0].split("|")[0].strip()
    keyword = keyword[:30]
    log(f"  searching WP draft with keyword: {keyword!r}")
    candidates = []
    for status in ("draft", "pending", "private", "publish", "future"):
        url = f"{client.api_base}/posts?status={status}&per_page=50&search={urllib.parse.quote(keyword)}"
        resp = client.session.get(url, timeout=30)
        if resp.status_code != 200:
            continue
        for p in resp.json():
            t = re.sub(r"<[^>]+>", "", p.get("title", {}).get("rendered", ""))
            if keyword in t:
                candidates.append((p["id"], t, status))
    if not candidates:
        sys.exit(f"WP投稿が見つかりませんでした: {keyword!r}")
    if len(candidates) > 1:
        log("  複数候補:")
        for pid, t, st in candidates:
            log(f"    [{st}] {pid}: {t}")
        # 完全一致を優先
        for pid, t, st in candidates:
            if t.strip() == title.strip():
                log(f"  → 完全一致を採用: {pid}")
                return pid
        sys.exit("候補が複数あり一意に決まらないので --wp-post-id で明示してください")
    return candidates[0][0]


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--article", required=True, help="記事HTMLパス（_for-wp.html 前の元ファイル）")
    parser.add_argument("--site", required=True, help="sites/xxx.json")
    parser.add_argument("--images", required=True, nargs="+", help="挿入候補の画像ファイル群")
    parser.add_argument("--wp-post-id", type=int, default=None, help="WP投稿IDを明示指定（省略時はタイトルで検索）")
    parser.add_argument("--dry-run", action="store_true", help="WP更新を行わずプレビューだけ")
    parser.add_argument("--skip-rebuild", action="store_true", help="build_for_wp.py を呼ばない")
    args = parser.parse_args()

    article_path = Path(args.article).resolve()
    if not article_path.exists():
        sys.exit(f"記事HTMLが見つかりません: {article_path}")

    image_paths = [Path(p).expanduser().resolve() for p in args.images]
    for p in image_paths:
        if not p.exists():
            sys.exit(f"画像が見つかりません: {p}")

    site = json.loads(Path(args.site).read_text())
    api_key = load_api_key()
    cache = load_cache()

    # 1. 画像コピー抽出
    log("=== 1. 画像コピーを抽出 ===")
    image_info: list[dict] = []
    for p in image_paths:
        copy = extract_image_copy(api_key, p)
        first_line = copy.splitlines()[0] if copy else "(空)"
        log(f"  {p.name}: {first_line}")
        image_info.append({"path": p, "copy": copy})

    # 2. WPアップロード
    log("\n=== 2. WPメディアにアップロード（重複はスキップ） ===")
    client = WordPressClient(site["site_url"], site["username"], site["app_password"])
    for info in image_info:
        media = upload_with_cache(client, site["site_url"], info["path"], cache)
        info["media_id"] = media["id"]
        info["media_url"] = media["url"]

    # 3. H2抽出
    article_html = article_path.read_text(encoding="utf-8")
    h2_list = extract_h2_titles(article_html)
    log(f"\n=== 3. H2 {len(h2_list)}個を検出 ===")
    for i, (_, t) in enumerate(h2_list):
        log(f"  [H2-{i}] {t}")

    # 4. マッチング
    log("\n=== 4. 画像とH2をマッチング ===")
    matches = match_images_to_h2s(
        api_key,
        [{"copy": info["copy"]} for info in image_info],
        [t for _, t in h2_list],
    )
    if not matches:
        sys.exit("マッチが0件でした。画像と記事のテーマが合っていない可能性があります。")
    for h2_idx, img_idx in sorted(matches.items()):
        log(f"  H2-{h2_idx} '{h2_list[h2_idx][1]}' ← {image_info[img_idx]['path'].name} (media_id={image_info[img_idx]['media_id']})")

    # 未マッチを明示的に警告
    matched_img_idxs = set(matches.values())
    matched_h2_idxs = set(matches.keys())
    unmatched_images = [
        (i, info) for i, info in enumerate(image_info) if i not in matched_img_idxs
    ]
    unmatched_h2s = [
        (i, t) for i, t in enumerate(t for _, t in h2_list) if i not in matched_h2_idxs
    ]
    if unmatched_images:
        log("\n  ⚠ 未挿入の画像（マッチするH2が無かった・アップ済みだが本文には入らない）:")
        for i, info in unmatched_images:
            first_line = info["copy"].splitlines()[0] if info["copy"] else "(空)"
            log(f"    - {info['path'].name} / コピー先頭: {first_line} / media_id={info['media_id']}")
    if unmatched_h2s:
        log("\n  ⚠ 画像が割り当たらなかったH2:")
        for i, t in unmatched_h2s:
            log(f"    - H2-{i} '{t}'")

    # 5. HTML挿入（後ろから挿入してインデックスずれを防止）
    log("\n=== 5. 記事HTMLにimg挿入 ===")
    new_html = article_html
    for h2_idx in sorted(matches.keys(), reverse=True):
        info = image_info[matches[h2_idx]]
        alt = h2_list[h2_idx][1]
        fig = build_figure_html(info["media_url"], info["media_id"], alt)
        new_html = insert_image_after_h2(new_html, h2_idx, fig)
    article_path.write_text(new_html, encoding="utf-8")
    log(f"  updated: {article_path}")

    if args.dry_run:
        log("\n--dry-run 指定なのでここで終了")
        return

    # 6. build_for_wp.py
    if not args.skip_rebuild:
        log("\n=== 6. _for-wp.html を再生成 ===")
        for_wp_path = rebuild_for_wp(article_path)
    else:
        for_wp_path = article_path.with_name(article_path.stem + "_for-wp.html")

    # 7. WP更新
    log("\n=== 7. WP下書きを更新 ===")
    if args.wp_post_id:
        post_id = args.wp_post_id
    else:
        title = extract_article_title(article_path, article_html)
        post_id = find_wp_draft(client, title)
    log(f"  WP post id: {post_id}")
    content = for_wp_path.read_text(encoding="utf-8")
    result = client.update_post(post_id, content=content)
    if result:
        log(f"  ✓ updated: {result.get('link')}")
    else:
        sys.exit("WP更新に失敗しました")


if __name__ == "__main__":
    main()
