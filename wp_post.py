#!/usr/local/bin/python3.12
"""
WordPress記事投稿・更新スクリプト

記事HTMLをWordPress REST API経由で下書き投稿・更新する。
画像はメディアライブラリに自動アップロードし、HTMLのパスを差し替える。

使い方:
  python3.12 wp_post.py \
    --html output/aga_横浜_記事.html \
    --site sites/example.json \
    --title "横浜のAGAおすすめクリニック11選｜費用・治療内容を徹底比較" \
    --category "AGA"

サイト設定ファイル (sites/example.json):
  {
    "site_url": "https://example.com",
    "username": "admin",
    "app_password": "xxxx xxxx xxxx xxxx",
    "rest_api_base": "https://example.com/wp-json/wp/v2"
  }

事前準備:
  1. WordPress管理画面 → ユーザー → プロフィール → アプリケーションパスワード で生成
  2. sites/ ディレクトリにサイトごとのJSONファイルを作成
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import xmlrpc.client
from html import unescape

from output_utils import OUTPUT_ROOT

try:
    import requests
except ImportError:
    print("Error: requests パッケージが必要です。")
    print("  pip install requests")
    sys.exit(1)


# ========================================
# WordPress API クライアント
# ========================================
class WordPressClient:
    def __init__(
        self,
        site_url: str,
        username: str,
        app_password: str,
        rest_api_base: str | None = None,
        xmlrpc_url: str | None = None,
    ):
        self.site_url = site_url.rstrip("/")
        self.auth = (username, app_password)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.api_base = self._discover_api_base(rest_api_base)
        self.xmlrpc_url = (xmlrpc_url or f"{self.site_url}/xmlrpc.php").rstrip("/")
        self.xmlrpc = xmlrpc.client.ServerProxy(self.xmlrpc_url)

    def _build_api_base_candidates(self) -> list[str]:
        return [
            f"{self.site_url}/wp-json/wp/v2",
            f"{self.site_url}/?rest_route=/wp/v2",
            f"{self.site_url}/index.php?rest_route=/wp/v2",
        ]

    def _api_url(self, endpoint: str) -> str:
        return f"{self.api_base.rstrip('/')}/{endpoint.lstrip('/')}"

    @staticmethod
    def _looks_like_rest_response(resp: requests.Response) -> bool:
        content_type = (resp.headers.get("content-type") or "").lower()
        body = (resp.text or "").lstrip()
        return "json" in content_type or body.startswith("{") or body.startswith("[")

    def _discover_api_base(self, configured_base: str | None) -> str:
        candidates = [configured_base.rstrip("/")] if configured_base else self._build_api_base_candidates()
        last_error = None
        for candidate in candidates:
            try:
                resp = self.session.get(
                    candidate,
                    headers={"Accept": "application/json"},
                    timeout=10,
                    allow_redirects=False,
                )
                if resp.status_code in (200, 401, 403) and self._looks_like_rest_response(resp):
                    return candidate.rstrip("/")
            except Exception as e:
                last_error = e

        if configured_base:
            return configured_base.rstrip("/")
        if last_error:
            print(f"  REST API endpoint auto-discovery warning: {last_error}")
        return f"{self.site_url}/wp-json/wp/v2"

    def test_connection(self) -> bool:
        """API接続テスト"""
        try:
            print(f"  REST endpoint: {self.api_base}")
            resp = self.session.get(
                self._api_url("users/me"),
                headers={"Accept": "application/json"},
                timeout=10,
                allow_redirects=False,
            )
            if resp.status_code == 200:
                user = resp.json()
                print(f"  Connected as: {user.get('name', 'unknown')}")
                return True
            if resp.status_code == 401 and self._looks_like_rest_response(resp):
                print("  Connection failed: 401 unauthorized")
                return False
            else:
                print(f"  Connection failed: {resp.status_code} {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"  Connection error: {e}")
            return False

    def upload_media(self, file_path: str, alt_text: str = "") -> dict | None:
        """画像をメディアライブラリにアップロードする"""
        if not os.path.exists(file_path):
            print(f"    File not found: {file_path}")
            return None

        original_filename = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "image/png"

        with open(file_path, "rb") as f:
            file_data = f.read()

        try:
            filename_candidates = [original_filename]
            shortened_filename = shorten_upload_filename(original_filename)
            if shortened_filename != original_filename:
                filename_candidates.append(shortened_filename)

            resp = None
            filename = original_filename
            for candidate_filename in filename_candidates:
                filename = candidate_filename
                headers = {
                    "Content-Disposition": f'attachment; filename="{urllib.parse.quote(candidate_filename)}"',
                    "Content-Type": mime_type,
                }
                resp = self.session.post(
                    self._api_url("media"),
                    data=file_data,
                    headers=headers,
                    timeout=60,
                )
                if resp.status_code == 201:
                    break

                body = resp.text[:500]
                retryable_guid_error = (
                    resp.status_code == 400
                    and "db_insert_error" in body
                    and ("guid" in body.lower() or candidate_filename != shortened_filename)
                )
                if retryable_guid_error and candidate_filename != shortened_filename:
                    print(f"    Upload retry with shortened filename: {shortened_filename}")
                    continue
                break

            if resp is not None and resp.status_code == 201:
                media = resp.json()
                media_url = media.get("source_url", "")
                media_id = media.get("id", 0)

                # alt テキストを設定
                if alt_text:
                    self.session.post(
                        self._api_url(f"media/{media_id}"),
                        json={"alt_text": alt_text},
                        timeout=10,
                    )

                return {
                    "id": media_id,
                    "url": media_url,
                    "filename": filename,
                }
            else:
                status = resp.status_code if resp is not None else "unknown"
                body = resp.text[:200] if resp is not None else ""
                print(f"    Upload failed: {status} {body}")
                return None

        except Exception as e:
            print(f"    Upload error: {e}")
            return None

    def create_post(
        self,
        title: str,
        content: str,
        status: str = "draft",
        category_ids: list[int] | None = None,
        featured_media_id: int | None = None,
    ) -> dict | None:
        """記事を投稿する"""
        post_data = {
            "title": title,
            "content": content,
            "status": status,
        }

        if category_ids:
            post_data["categories"] = category_ids

        if featured_media_id:
            post_data["featured_media"] = featured_media_id

        try:
            resp = self.session.post(
                self._api_url("posts"),
                json=post_data,
                timeout=30,
            )

            if resp.status_code == 201:
                return resp.json()
            else:
                print(f"  Post failed: {resp.status_code} {resp.text[:300]}")
                return None

        except Exception as e:
            print(f"  Post error: {e}")
            return None

    def update_post(self, post_id: int, **kwargs) -> dict | None:
        """既存の記事を更新する"""
        try:
            resp = self.session.post(
                self._api_url(f"posts/{post_id}"),
                json=kwargs,
                timeout=30,
            )

            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"  Update failed: {resp.status_code} {resp.text[:300]}")
                return None

        except Exception as e:
            print(f"  Update error: {e}")
            return None

    def get_categories(self) -> list[dict]:
        """カテゴリ一覧を取得する"""
        try:
            resp = self.session.get(
                self._api_url("categories"),
                params={"per_page": 100},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception:
            return []

    def find_or_create_category(self, name: str) -> int | None:
        """カテゴリを名前で検索し、なければ作成する"""
        categories = self.get_categories()
        for cat in categories:
            if cat.get("name", "").lower() == name.lower():
                return cat["id"]

        # 作成
        try:
            resp = self.session.post(
                self._api_url("categories"),
                json={"name": name},
                timeout=10,
            )
            if resp.status_code == 201:
                return resp.json()["id"]
            return None
        except Exception:
            return None

    def find_media_id_by_url(self, media_url: str) -> int | None:
        """メディアURLから media ID を推定する。"""
        try:
            filename = os.path.basename(urllib.parse.urlparse(media_url).path)
            stem, _ = os.path.splitext(filename)
            resp = self.session.get(
                self._api_url("media"),
                params={"search": stem, "per_page": 20},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            for item in resp.json():
                if item.get("source_url") == media_url:
                    return item.get("id")
        except Exception:
            return None
        return None

    def get_custom_fields(self, post_id: int) -> list[dict]:
        """XML-RPC経由で投稿のカスタムフィールド一覧を取得する。"""
        try:
            post = self.xmlrpc.wp.getPost(0, self.auth[0], self.auth[1], post_id)
            return post.get("custom_fields") or []
        except Exception as e:
            print(f"  Failed to fetch custom fields via XML-RPC: {e}")
            return []

    def upsert_custom_fields(self, post_id: int, field_map: dict[str, str | None]) -> bool:
        """XML-RPC経由で投稿メタを追加・更新・削除する。"""
        existing = {
            item.get("key"): item
            for item in self.get_custom_fields(post_id)
            if item.get("key")
        }
        payload = []

        for key, value in field_map.items():
            current = existing.get(key)
            if value is None:
                if current and current.get("id"):
                    payload.append({"id": current["id"]})
                continue

            if current and current.get("id"):
                payload.append({"id": current["id"], "key": key, "value": value})
            else:
                payload.append({"key": key, "value": value})

        if not payload:
            return True

        try:
            return bool(
                self.xmlrpc.wp.editPost(
                    0,
                    self.auth[0],
                    self.auth[1],
                    post_id,
                    {"custom_fields": payload},
                )
            )
        except Exception as e:
            print(f"  Failed to update custom fields via XML-RPC: {e}")
            return False


# ========================================
# 画像URLキャッシュ
# ========================================
CACHE_FILE = os.path.join(OUTPUT_ROOT, ".image_url_cache.json")
CSS_START_MARKER = "<!-- seo-article-common-css:start -->"
CSS_END_MARKER = "<!-- seo-article-common-css:end -->"
JS_START_MARKER = "<!-- seo-article-common-js:start -->"
JS_END_MARKER = "<!-- seo-article-common-js:end -->"
SWELL_CUSTOM_CSS_KEY = "swell_meta_css"
SWELL_CUSTOM_JS_KEY = "swell_meta_js"
LEGACY_SWELL_CUSTOM_CSS_KEY = "swell_custom_css"
LEGACY_SWELL_CUSTOM_JS_KEY = "swell_custom_js"
UPLOAD_FILENAME_MAX_STEM = 40


def load_image_cache() -> dict[str, str]:
    """アップロード済み画像URLのキャッシュを読み込む"""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_image_cache(cache: dict[str, str]):
    """キャッシュを保存する"""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def shorten_upload_filename(filename: str) -> str:
    """WordPressメディア登録に失敗しにくい短めのファイル名を返す。"""
    stem, ext = os.path.splitext(filename)
    stem = re.split(r"\s*[|｜]\s*", stem, maxsplit=1)[0].strip()
    stem = re.sub(r"_+", "_", stem)
    stem = re.sub(r"[^\w\-]+", "_", stem).strip("_")
    if len(stem) > UPLOAD_FILENAME_MAX_STEM:
        stem = stem[:UPLOAD_FILENAME_MAX_STEM].rstrip("_-")
    return f"{stem or 'upload'}{ext.lower()}"


def get_file_mtime(file_path: str) -> float:
    """ファイルの更新時刻を返す。取得できない場合は0。"""
    try:
        return os.path.getmtime(file_path)
    except OSError:
        return 0.0


def get_cached_image_url(cache: dict, src: str, local_path: str) -> str | None:
    """キャッシュ済みURLを返す。ローカル画像が更新されていれば無効化する。"""
    entry = cache.get(src)
    if not entry:
        return None

    local_mtime = get_file_mtime(local_path)

    if isinstance(entry, str):
        return None

    if not isinstance(entry, dict):
        return None

    cached_url = entry.get("url")
    cached_mtime = float(entry.get("mtime", 0))
    if not cached_url:
        return None

    if local_mtime > cached_mtime:
        return None

    return cached_url


def update_image_cache(cache: dict, src: str, local_path: str, url: str):
    """画像URLキャッシュを更新する。"""
    cache[src] = {
        "url": url,
        "mtime": get_file_mtime(local_path),
    }


# ========================================
# 画像処理
# ========================================
def find_local_images(html_content: str, html_dir: str) -> list[dict]:
    """HTML内のローカル画像パスを抽出する"""
    images = []
    img_pattern = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
    src_pattern = re.compile(r'src="([^"]+)"', re.IGNORECASE)
    alt_pattern = re.compile(r'alt="([^"]*)"', re.IGNORECASE)

    for match in img_pattern.finditer(html_content):
        tag = match.group(0)
        src_match = src_pattern.search(tag)
        if not src_match:
            continue
        src = src_match.group(1)
        alt_match = alt_pattern.search(tag)
        alt = alt_match.group(1) if alt_match else ""

        # すでにhttp(s)で始まっていたらスキップ
        if src.startswith("http://") or src.startswith("https://"):
            continue

        # ローカルパスを解決
        local_path = os.path.normpath(os.path.join(html_dir, src))
        if os.path.exists(local_path):
            images.append({
                "src": src,
                "alt": alt,
                "local_path": local_path,
                "original_tag": tag,
            })

    return images


def replace_image_urls(html_content: str, url_map: dict[str, str]) -> str:
    """HTML内のローカル画像パスをWordPress URLに置換する"""
    for local_src, wp_url in url_map.items():
        html_content = re.sub(
            rf'src=(["\']){re.escape(local_src)}\1',
            lambda match: f'src={match.group(1)}{wp_url}{match.group(1)}',
            html_content,
        )
    return html_content


def remove_featured_image_tag(html_content: str) -> str:
    """本文冒頭のトップ画像タグを削除してアイキャッチとの二重表示を防ぐ。"""
    pattern = re.compile(
        r'^\s*<img\s+[^>]*src="[^"]*_top\.(?:png|jpg|jpeg|webp)"[^>]*>\s*',
        re.IGNORECASE,
    )
    return pattern.sub("", html_content, count=1).lstrip()


def remove_deprecated_image_placeholders(html_content: str) -> str:
    """古いH2画像プレースホルダーを除去する。"""
    pattern = re.compile(
        r'\s*<img\s+[^>]*src="images/[^"]*_h2_\d+\.(?:jpg|jpeg|webp)"[^>]*>\s*',
        re.IGNORECASE,
    )
    cleaned = pattern.sub("\n", html_content)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def remove_missing_local_image_tags(html_content: str, html_dir: str) -> str:
    """存在しないローカル画像タグを除去する。"""
    pattern = re.compile(r'\s*<img\s+[^>]*src="(?!https?://)([^"]+)"[^>]*>\s*', re.IGNORECASE)

    def replacer(match):
        src = match.group(1)
        local_path = os.path.normpath(os.path.join(html_dir, src))
        if os.path.exists(local_path):
            return match.group(0)
        print(f"  Removing unresolved image tag: {src}")
        return "\n"

    cleaned = pattern.sub(replacer, html_content)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def remove_duplicate_screenshot_blocks(html_content: str) -> str:
    """同じスクリーンショットを複数回表示している場合は後続を除去する。"""
    seen = set()
    pattern = re.compile(
        r'\s*<div class="clinic-screenshot"[^>]*>\s*<img[^>]*src="([^"]+)"[^>]*>\s*</div>\s*',
        re.IGNORECASE,
    )

    def replacer(match):
        src = match.group(1)
        if src in seen:
            print(f"  Removing duplicate screenshot block: {src}")
            return "\n"
        seen.add(src)
        return match.group(0)

    cleaned = pattern.sub(replacer, html_content)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_existing_injected_assets(html_content: str) -> str:
    html_content = re.sub(
        rf"{re.escape(CSS_START_MARKER)}.*?{re.escape(CSS_END_MARKER)}\s*",
        "",
        html_content,
        flags=re.DOTALL,
    )
    html_content = re.sub(
        rf"{re.escape(JS_START_MARKER)}.*?{re.escape(JS_END_MARKER)}\s*",
        "",
        html_content,
        flags=re.DOTALL,
    )
    return html_content.strip()


def load_article_assets(html_dir: str) -> dict[str, str | None]:
    """記事ごとの article-common.css / .js を読み込む。"""
    css_path = os.path.join(html_dir, "article-common.css")
    js_path = os.path.join(html_dir, "article-common.js")
    if not os.path.exists(css_path):
        fallback_css = os.path.join(OUTPUT_ROOT, "article-common.css")
        if os.path.exists(fallback_css):
            css_path = fallback_css
    if not os.path.exists(js_path):
        fallback_js = os.path.join(OUTPUT_ROOT, "article-common.js")
        if os.path.exists(fallback_js):
            js_path = fallback_js

    assets = {"css": None, "js": None}
    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            assets["css"] = f.read()
    if os.path.exists(js_path):
        with open(js_path, "r", encoding="utf-8") as f:
            assets["js"] = f.read()
    return assets


def extract_post_title(html_content: str, html_path: str) -> str:
    """HTML内容からWordPress投稿タイトルを推定する。"""
    tag_structure_path = html_path.replace("_記事.html", "_タグ構成.md")
    if os.path.exists(tag_structure_path):
        try:
            with open(tag_structure_path, "r", encoding="utf-8") as f:
                tag_structure = f.read()
            match = re.search(r"\*\*titleタグ\*\*:\s*(.+)", tag_structure)
            if match:
                text = unescape(match.group(1)).strip()
                if text:
                    return text
        except OSError:
            pass

    for pattern in (r"<h1[^>]*>(.*?)</h1>", r"<title[^>]*>(.*?)</title>", r"<h2[^>]*>(.*?)</h2>"):
        match = re.search(pattern, html_content, flags=re.IGNORECASE | re.DOTALL)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1))
            text = unescape(text).strip()
            if text:
                return text

    basename = os.path.splitext(os.path.basename(html_path))[0]
    return basename.replace("_記事", "").replace("_", " ").strip()


def find_featured_image_candidate(html_path: str) -> dict | None:
    """記事HTMLに対応するトップ画像ファイルを推定する。"""
    html_abs = os.path.abspath(html_path)
    html_dir = os.path.dirname(html_abs)
    basename = os.path.splitext(os.path.basename(html_abs))[0]
    keyword_slug = basename.replace("_記事", "")

    candidate_names = [
        f"{keyword_slug}_top.png",
        f"{keyword_slug}_top.jpg",
        f"{keyword_slug}_top.jpeg",
        f"{keyword_slug}_top.webp",
    ]

    images_dir = os.path.join(html_dir, "images")
    for name in candidate_names:
        local_path = os.path.join(images_dir, name)
        if os.path.exists(local_path):
            return {
                "src": f"images/{name}",
                "alt": keyword_slug.replace("_", " ") + " のトップ画像",
                "local_path": local_path,
            }
    return None


def validate_html_for_wordpress(html_content: str, *, allow_local_images: bool = False) -> list[str]:
    issues = []

    local_srcs = re.findall(r'src="(?!https?://)([^"]+)"', html_content, flags=re.IGNORECASE)
    if local_srcs and not allow_local_images:
        issues.append("ローカル画像参照が残っています: " + ", ".join(local_srcs[:5]))

    if re.search(r'<img\s+[^>]*src="[^"]*_top\.(?:png|jpg|jpeg|webp)"', html_content, re.IGNORECASE):
        issues.append("本文先頭のトップ画像タグが残っています")

    if any(
        marker in html_content
        for marker in (CSS_START_MARKER, CSS_END_MARKER, JS_START_MARKER, JS_END_MARKER)
    ):
        issues.append("共通CSS/JSの本文埋め込みが残っています")

    return issues


# ========================================
# メイン
# ========================================
def main():
    parser = argparse.ArgumentParser(description="WordPress下書き投稿・更新スクリプト")
    parser.add_argument("--html", type=str, help="記事HTMLファイルのパス")
    parser.add_argument("--site", required=True, help="サイト設定JSONファイルのパス")
    parser.add_argument("--title", type=str, help="記事タイトル")
    parser.add_argument("--category", type=str, help="カテゴリ名（なければ自動作成）")
    parser.add_argument("--status", default="draft", choices=["draft", "publish", "pending"],
                        help="投稿ステータス（通常運用は draft）")
    parser.add_argument("--publish", action="store_true",
                        help="既存記事を公開する（互換用オプション。通常運用では管理画面から公開推奨）")
    parser.add_argument("--post-id", type=int, help="更新対象の投稿ID")
    parser.add_argument("--skip-images", action="store_true",
                        help="画像アップロードをスキップ")
    parser.add_argument("--dry-run", action="store_true",
                        help="実際には投稿せず確認のみ")
    args = parser.parse_args()

    # サイト設定読み込み
    if not os.path.exists(args.site):
        print(f"Error: サイト設定ファイルが見つかりません: {args.site}")
        sys.exit(1)

    with open(args.site, "r", encoding="utf-8") as f:
        site_config = json.load(f)

    site_url = site_config["site_url"]
    username = site_config["username"]
    app_password = site_config["app_password"]
    rest_api_base = site_config.get("rest_api_base")
    xmlrpc_url = site_config.get("xmlrpc_url")

    print(f"Site: {site_url}")

    # クライアント初期化
    client = WordPressClient(
        site_url,
        username,
        app_password,
        rest_api_base=rest_api_base,
        xmlrpc_url=xmlrpc_url,
    )

    # 接続テスト
    print("Testing connection...")
    if not client.test_connection():
        print("Error: WordPress APIに接続できません。")
        print("  - サイトURLが正しいか確認")
        print("  - ユーザー名・アプリケーションパスワードが正しいか確認")
        print("  - WordPress REST APIが有効か確認")
        sys.exit(1)

    # 公開モード
    if args.publish and args.post_id:
        print(f"\nPublishing post ID: {args.post_id}...")
        if args.dry_run:
            print("  [DRY RUN] Would publish post.")
            return

        result = client.update_post(args.post_id, status="publish")
        if result:
            print(f"  Published: {result.get('link', '')}")
        return

    # 更新モード（--post-id + --html）
    if args.post_id and args.html:
        if not os.path.exists(args.html):
            print(f"Error: HTMLファイルが見つかりません: {args.html}")
            sys.exit(1)

        with open(args.html, "r", encoding="utf-8") as f:
            html_content = f.read()

        html_dir = os.path.dirname(os.path.abspath(args.html))
        assets = load_article_assets(html_dir)

        title = args.title or extract_post_title(html_content, args.html)

        # 画像処理（キャッシュ対応）
        cache = load_image_cache()
        images = find_local_images(html_content, html_dir)
        url_map = {}
        featured_media_id = None
        featured_candidate = find_featured_image_candidate(args.html)

        if images:
            print(f"\nFound {len(images)} local images:")
            for img in images:
                src = img["src"]
                cached_url = get_cached_image_url(cache, src, img["local_path"])
                if cached_url:
                    url_map[src] = cached_url
                    print(f"  - {src} → cached: {cached_url}")
                    if "_top." in src and not featured_media_id:
                        featured_media_id = client.find_media_id_by_url(cached_url)
                        if featured_media_id:
                            print(f"    → Reuse featured image (ID: {featured_media_id})")
                elif not args.skip_images and not args.dry_run:
                    print(f"  Uploading: {src}")
                    result = client.upload_media(img["local_path"], img["alt"])
                    if result:
                        url_map[src] = result["url"]
                        update_image_cache(cache, src, img["local_path"], result["url"])
                        print(f"    → {result['url']}")

                        if "_top." in src:
                            featured_media_id = result["id"]
                            print(f"    → Set as featured image (ID: {featured_media_id})")
                else:
                    print(f"  - {src} (skipped, not in cache)")

            if url_map:
                html_content = replace_image_urls(html_content, url_map)
                print(f"\n  Replaced {len(url_map)} image URLs in HTML.")
                save_image_cache(cache)

        if featured_candidate and not args.skip_images and not args.dry_run:
            cached_url = get_cached_image_url(cache, featured_candidate["src"], featured_candidate["local_path"])
            if cached_url:
                featured_media_id = client.find_media_id_by_url(cached_url)
                if featured_media_id:
                    print(f"\nFeatured image: cached reuse ({featured_media_id})")
            if not featured_media_id:
                print(f"\nUploading featured image: {featured_candidate['src']}")
                result = client.upload_media(featured_candidate["local_path"], featured_candidate["alt"])
                if result:
                    featured_media_id = result["id"]
                    update_image_cache(
                        cache,
                        featured_candidate["src"],
                        featured_candidate["local_path"],
                        result["url"],
                    )
                    save_image_cache(cache)
                    print(f"  → Set as featured image (ID: {featured_media_id})")

        html_content = remove_featured_image_tag(html_content)
        html_content = remove_deprecated_image_placeholders(html_content)
        html_content = remove_missing_local_image_tags(html_content, html_dir)
        html_content = remove_duplicate_screenshot_blocks(html_content)
        html_content = strip_existing_injected_assets(html_content)

        print("\nPreparing post-level CSS/JS...")
        print(f"  CSS: {len(assets['css']) if assets['css'] else 0} chars")
        print(f"  JS: {len(assets['js']) if assets['js'] else 0} chars")

        issues = validate_html_for_wordpress(html_content, allow_local_images=args.dry_run)
        if issues:
            print("\nError: 更新前HTMLの検証に失敗しました。")
            for issue in issues:
                print(f"  - {issue}")
            sys.exit(1)

        update_data = {"content": html_content, "title": title}
        if featured_media_id:
            update_data["featured_media"] = featured_media_id

        print(f"\nUpdating post ID: {args.post_id}...")
        if args.dry_run:
            print(f"  [DRY RUN] Would update post content ({len(html_content)} chars).")
            print("  [DRY RUN] Would sync SWELL post CSS/JS fields.")
            return

        result = client.update_post(args.post_id, **update_data)
        if result:
            asset_ok = client.upsert_custom_fields(
                args.post_id,
                {
                    SWELL_CUSTOM_CSS_KEY: assets["css"],
                    SWELL_CUSTOM_JS_KEY: assets["js"],
                    LEGACY_SWELL_CUSTOM_CSS_KEY: None,
                    LEGACY_SWELL_CUSTOM_JS_KEY: None,
                },
            )
            if not asset_ok:
                print("Error: 記事ごとのカスタムCSS/JS更新に失敗しました。")
                sys.exit(1)
            print(f"  Updated: {result.get('link', '')}")
            edit_link = f"{site_url}/wp-admin/post.php?post={args.post_id}&action=edit"
            print(f"  Edit: {edit_link}")
            print("  Synced SWELL custom CSS/JS fields.")
        else:
            print("Error: 更新に失敗しました。")
            sys.exit(1)
        return

    # 新規投稿モード
    if not args.html:
        print("Error: --html オプションが必要です。")
        sys.exit(1)

    if not os.path.exists(args.html):
        print(f"Error: HTMLファイルが見つかりません: {args.html}")
        sys.exit(1)

    # HTML読み込み
    with open(args.html, "r", encoding="utf-8") as f:
        html_content = f.read()

    html_dir = os.path.dirname(os.path.abspath(args.html))
    assets = load_article_assets(html_dir)

    # タイトル
    title = args.title
    if not title:
        title = extract_post_title(html_content, args.html)
        print(f"  Title (auto): {title}")

    print(f"\nTitle: {title}")
    print(f"Status: {args.status}")

    # 画像処理（キャッシュ対応）
    cache = load_image_cache()
    url_map = {}
    featured_media_id = None
    images = find_local_images(html_content, html_dir)
    featured_candidate = find_featured_image_candidate(args.html)

    if images:
        print(f"\nFound {len(images)} local images:")
        for img in images:
            src = img["src"]
            cached_url = get_cached_image_url(cache, src, img["local_path"])
            if cached_url:
                url_map[src] = cached_url
                print(f"  - {src} → cached: {cached_url}")
                if "_top." in src and not featured_media_id:
                    featured_media_id = client.find_media_id_by_url(cached_url)
                    if featured_media_id:
                        print(f"    → Reuse featured image (ID: {featured_media_id})")
            elif not args.skip_images and not args.dry_run:
                print(f"  Uploading: {src}")
                result = client.upload_media(img["local_path"], img["alt"])
                if result:
                    url_map[src] = result["url"]
                    update_image_cache(cache, src, img["local_path"], result["url"])
                    print(f"    → {result['url']}")

                    if "_top." in src:
                        featured_media_id = result["id"]
                        print(f"    → Set as featured image (ID: {featured_media_id})")
            else:
                print(f"  - {src} ({img['alt']})")

        if url_map:
            html_content = replace_image_urls(html_content, url_map)
            print(f"\n  Replaced {len(url_map)} image URLs in HTML.")
            save_image_cache(cache)

    if featured_candidate and not args.skip_images and not args.dry_run:
        cached_url = get_cached_image_url(cache, featured_candidate["src"], featured_candidate["local_path"])
        if cached_url:
            featured_media_id = client.find_media_id_by_url(cached_url)
            if featured_media_id:
                print(f"\nFeatured image: cached reuse ({featured_media_id})")
        if not featured_media_id:
            print(f"\nUploading featured image: {featured_candidate['src']}")
            result = client.upload_media(featured_candidate["local_path"], featured_candidate["alt"])
            if result:
                featured_media_id = result["id"]
                update_image_cache(
                    cache,
                    featured_candidate["src"],
                    featured_candidate["local_path"],
                    result["url"],
                )
                save_image_cache(cache)
                print(f"  → Set as featured image (ID: {featured_media_id})")

    html_content = remove_featured_image_tag(html_content)
    html_content = remove_deprecated_image_placeholders(html_content)
    html_content = remove_missing_local_image_tags(html_content, html_dir)
    html_content = remove_duplicate_screenshot_blocks(html_content)
    html_content = strip_existing_injected_assets(html_content)

    print("\nPreparing post-level CSS/JS...")
    print(f"  CSS: {len(assets['css']) if assets['css'] else 0} chars")
    print(f"  JS: {len(assets['js']) if assets['js'] else 0} chars")

    issues = validate_html_for_wordpress(html_content, allow_local_images=args.dry_run)
    if issues:
        print("\nError: 投稿前HTMLの検証に失敗しました。")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)

    # カテゴリ
    category_ids = None
    if args.category:
        if not args.dry_run:
            cat_id = client.find_or_create_category(args.category)
            if cat_id:
                category_ids = [cat_id]
                print(f"\nCategory: {args.category} (ID: {cat_id})")
            else:
                print(f"\nWarning: Category '{args.category}' could not be found or created.")
        else:
            print(f"\nCategory: {args.category} [DRY RUN]")

    # 投稿
    if args.dry_run:
        print("\n[DRY RUN] Would create post:")
        print(f"  Title: {title}")
        print(f"  Status: {args.status}")
        print(f"  Content length: {len(html_content)} chars")
        print(f"  Images uploaded: {len(url_map)}")
        print(f"  Featured media: {featured_media_id}")
        print(f"  SWELL CSS: {len(assets['css']) if assets['css'] else 0} chars")
        print(f"  SWELL JS: {len(assets['js']) if assets['js'] else 0} chars")
        return

    print("\nCreating post...")
    result = client.create_post(
        title=title,
        content=html_content,
        status=args.status,
        category_ids=category_ids,
        featured_media_id=featured_media_id,
    )

    if result:
        post_id = result.get("id", "")
        post_link = result.get("link", "")
        edit_link = f"{site_url}/wp-admin/post.php?post={post_id}&action=edit"

        asset_ok = client.upsert_custom_fields(
            post_id,
            {
                SWELL_CUSTOM_CSS_KEY: assets["css"],
                SWELL_CUSTOM_JS_KEY: assets["js"],
                LEGACY_SWELL_CUSTOM_CSS_KEY: None,
                LEGACY_SWELL_CUSTOM_JS_KEY: None,
            },
        )
        if not asset_ok:
            print("\nError: 記事ごとのカスタムCSS/JS更新に失敗しました。")
            sys.exit(1)

        print(f"\n{'=' * 60}")
        print(f"Post created successfully!")
        print(f"  ID: {post_id}")
        print(f"  Status: {args.status}")
        print(f"  URL: {post_link}")
        print(f"  Edit: {edit_link}")
        print("  SWELL custom CSS/JS: synced")
        print(f"{'=' * 60}")

        if args.status == "draft":
            print("\nDraft created. Review and publish from the WordPress admin screen if needed.")
    else:
        print("\nError: 投稿に失敗しました。")
        sys.exit(1)


if __name__ == "__main__":
    main()
