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
import io
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import xmlrpc.client
from datetime import datetime
from html import unescape

from output_utils import OUTPUT_ROOT, save_variant_status

try:
    import requests
except ImportError:
    print("Error: requests パッケージが必要です。")
    print("  pip install requests")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    Image = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def strip_iframes_for_post(html: str) -> str:
    """403回避用に iframe を投稿前HTMLから除去する。"""
    stripped = re.sub(r"<iframe\b[^>]*>.*?</iframe>", "", html, flags=re.IGNORECASE | re.DOTALL)
    stripped = re.sub(r'<div class="clinic-map">\s*</div>', "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped


def infer_output_key_from_html_path(html_path: str) -> str:
    basename = os.path.basename(html_path)
    if basename.endswith("_記事.html"):
        stem = basename[:-len("_記事.html")]
    else:
        stem = os.path.splitext(basename)[0]
    sentinel = "<<DOUBLE_UNDERSCORE>>"
    stem = stem.replace("__", sentinel)
    stem = stem.replace("_", " ")
    return stem.replace(sentinel, "__")


def is_manual_wp_html_path(html_path: str) -> bool:
    """build_for_wp.py が生成した手動投稿用 HTML かどうか。"""
    return (html_path or "").endswith("_記事_for-wp.html")


def detect_variant_index_from_path(html_path: str) -> int | None:
    """パスから __nandemo_v(\\d+) を抽出。v1〜v5でなければ None。"""
    match = re.search(r"__nandemo_v(\d+)", html_path or "")
    if not match:
        return None
    n = int(match.group(1))
    if 1 <= n <= 5:
        return n
    return None


def get_variant_theme_css_path(variant_index: int | None) -> str | None:
    """v2〜v5には対応するテーマCSSパスを返す（v1はベースのみ）。"""
    if variant_index is None or variant_index == 1:
        return None
    path = os.path.join(OUTPUT_ROOT, f"article-theme-v{variant_index}.css")
    return path if os.path.exists(path) else None


# ========================================
# XML-RPC タイムアウト付きトランスポート
# ========================================
class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout=120, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout=120, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


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
        xmlrpc_transport = (
            _TimeoutSafeTransport(timeout=120)
            if self.xmlrpc_url.startswith("https://")
            else _TimeoutTransport(timeout=120)
        )
        self.xmlrpc = xmlrpc.client.ServerProxy(
            self.xmlrpc_url,
            transport=xmlrpc_transport,
            allow_none=True,
        )
        self.rest_available = True
        self.xmlrpc_available = False
        self.xmlrpc_blog_id = "0"
        self.connection_mode = "rest"

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
                self.rest_available = True
                self.connection_mode = "rest"
                print(f"  Connected as: {user.get('name', 'unknown')}")
                return True
            if resp.status_code in (401, 403) and self._looks_like_rest_response(resp):
                print(f"  REST auth unavailable: {resp.status_code}")
            else:
                print(f"  REST connection failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"  REST connection error: {e}")

        return self._test_xmlrpc_connection()

    def _test_xmlrpc_connection(self) -> bool:
        try:
            blogs = self.xmlrpc.wp.getUsersBlogs(self.auth[0], self.auth[1])
            if not blogs:
                print("  XML-RPC auth succeeded but no blogs were returned.")
                return False

            primary_blog = blogs[0]
            self.xmlrpc_blog_id = str(primary_blog.get("blogid", "0"))
            self.rest_available = False
            self.xmlrpc_available = True
            self.connection_mode = "xmlrpc"
            print(
                "  Connected via XML-RPC as: "
                f"{primary_blog.get('blogName', primary_blog.get('url', 'unknown'))}"
            )
            return True
        except Exception as e:
            print(f"  XML-RPC connection error: {e}")
            return False

    def _build_xmlrpc_post_data(
        self,
        *,
        title: str | None = None,
        content: str | None = None,
        status: str | None = None,
        category_ids: list[int] | None = None,
        featured_media_id: int | None = None,
        extra_fields: dict | None = None,
    ) -> dict:
        post_data = {"post_type": "post"}

        if title is not None:
            post_data["post_title"] = title
        if content is not None:
            post_data["post_content"] = content
        if status is not None:
            post_data["post_status"] = status
        if category_ids:
            post_data["terms"] = {"category": category_ids}
        if featured_media_id:
            post_data["post_thumbnail"] = int(featured_media_id)
        if extra_fields:
            post_data.update(extra_fields)

        return post_data

    def _get_post_link_via_xmlrpc(self, post_id: int) -> str:
        try:
            post = self.xmlrpc.wp.getPost(self.xmlrpc_blog_id, self.auth[0], self.auth[1], post_id)
        except Exception:
            return ""
        return post.get("link") or post.get("permaLink") or post.get("post_link") or ""

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

        upload_filename, mime_type, file_data = optimize_upload_image(
            file_path=file_path,
            filename=original_filename,
            mime_type=mime_type,
            file_data=file_data,
        )

        if not self.rest_available and self.xmlrpc_available:
            return self._upload_media_via_xmlrpc(
                original_filename=upload_filename,
                mime_type=mime_type,
                file_data=file_data,
                alt_text=alt_text,
            )

        try:
            filename_candidates = [upload_filename]
            shortened_filename = shorten_upload_filename(upload_filename)
            if shortened_filename != upload_filename:
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
                    try:
                        self.session.post(
                            self._api_url(f"media/{media_id}"),
                            json={"alt_text": alt_text},
                            timeout=30,
                        )
                    except Exception as e:
                        print(f"    Alt text update warning: {e}")

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

    def _upload_media_via_xmlrpc(
        self,
        *,
        original_filename: str,
        mime_type: str,
        file_data: bytes,
        alt_text: str = "",
    ) -> dict | None:
        filename_candidates = [original_filename]
        shortened_filename = shorten_upload_filename(original_filename)
        if shortened_filename != original_filename:
            filename_candidates.append(shortened_filename)

        for filename in filename_candidates:
            try:
                upload = self.xmlrpc.wp.uploadFile(
                    self.xmlrpc_blog_id,
                    self.auth[0],
                    self.auth[1],
                    {
                        "name": filename,
                        "type": mime_type,
                        "bits": xmlrpc.client.Binary(file_data),
                        "overwrite": True,
                    },
                )
                media_id = int(upload.get("id", 0))
                media_url = upload.get("url", "")

                if alt_text and media_id:
                    try:
                        self.xmlrpc.wp.editPost(
                            self.xmlrpc_blog_id,
                            self.auth[0],
                            self.auth[1],
                            media_id,
                            {
                                "custom_fields": [
                                    {"key": "_wp_attachment_image_alt", "value": alt_text},
                                ]
                            },
                        )
                    except Exception:
                        pass

                return {
                    "id": media_id,
                    "url": media_url,
                    "filename": filename,
                }
            except xmlrpc.client.Fault as fault:
                retryable = "guid" in fault.faultString.lower() or filename != shortened_filename
                if retryable and filename != shortened_filename:
                    print(f"    Upload retry with shortened filename: {shortened_filename}")
                    continue
                print(f"    Upload failed via XML-RPC: {fault.faultString[:200]}")
                return None
            except Exception as e:
                print(f"    Upload error via XML-RPC: {e}")
                return None

        return None

    def create_post(
        self,
        title: str,
        content: str,
        status: str = "draft",
        category_ids: list[int] | None = None,
        featured_media_id: int | None = None,
        extra_fields: dict | None = None,
    ) -> dict | None:
        """記事を投稿する"""
        if not self.rest_available and self.xmlrpc_available:
            if extra_fields:
                print("  XML-RPC create does not support extra ACF fields for _for-wp.html posting.")
                return None
            return self._create_post_via_xmlrpc(
                title=title,
                content=content,
                status=status,
                category_ids=category_ids,
                featured_media_id=featured_media_id,
            )

        post_data = {
            "title": title,
            "content": content,
            "status": status,
        }

        if category_ids:
            post_data["categories"] = category_ids

        if featured_media_id:
            post_data["featured_media"] = featured_media_id
        if extra_fields:
            post_data.update(extra_fields)

        try:
            attempts = [("original", content)]
            iframe_stripped = strip_iframes_for_post(content)
            if iframe_stripped != content:
                attempts.append(("iframe_stripped", iframe_stripped))

            for label, attempt_content in attempts:
                post_data["content"] = attempt_content
                resp = self.session.post(
                    self._api_url("posts"),
                    json=post_data,
                    timeout=30,
                )

                if resp.status_code == 201:
                    if label != "original":
                        print("  Post create retry succeeded after removing iframe embeds.")
                    return resp.json()

                print(f"  Post failed ({label}): {resp.status_code} {resp.text[:300]}")
                if resp.status_code != 403 or label != "original":
                    continue
                if len(attempts) > 1:
                    print("  Retrying post creation without iframe embeds...")

            if extra_fields:
                return None
            if self._test_xmlrpc_connection():
                print("  REST投稿に失敗したため、XML-RPCで再試行します。")
                return self._create_post_via_xmlrpc(
                    title=title,
                    content=content,
                    status=status,
                    category_ids=category_ids,
                    featured_media_id=featured_media_id,
                )

            return None

        except Exception as e:
            print(f"  Post error: {e}")
            if extra_fields:
                return None
            if self._test_xmlrpc_connection():
                print("  REST例外のため、XML-RPCで再試行します。")
                return self._create_post_via_xmlrpc(
                    title=title,
                    content=content,
                    status=status,
                    category_ids=category_ids,
                    featured_media_id=featured_media_id,
                )
            return None

    def _create_post_via_xmlrpc(
        self,
        *,
        title: str,
        content: str,
        status: str = "draft",
        category_ids: list[int] | None = None,
        featured_media_id: int | None = None,
    ) -> dict | None:
        post_data = self._build_xmlrpc_post_data(
            title=title,
            content=content,
            status=status,
            category_ids=category_ids,
            featured_media_id=featured_media_id,
        )

        try:
            post_id = int(
                self.xmlrpc.wp.newPost(
                    self.xmlrpc_blog_id,
                    self.auth[0],
                    self.auth[1],
                    post_data,
                )
            )
            return {
                "id": post_id,
                "link": self._get_post_link_via_xmlrpc(post_id),
                "featured_media": featured_media_id or 0,
            }
        except Exception as e:
            print(f"  Post error via XML-RPC: {e}")
            return None

    def update_post(self, post_id: int, **kwargs) -> dict | None:
        """既存の記事を更新する"""
        if not self.rest_available and self.xmlrpc_available:
            return self._update_post_via_xmlrpc(post_id, **kwargs)

        try:
            attempts = [("original", kwargs.get("content"))]
            content = kwargs.get("content")
            iframe_stripped = strip_iframes_for_post(content) if isinstance(content, str) else content
            if isinstance(content, str) and iframe_stripped != content:
                attempts.append(("iframe_stripped", iframe_stripped))

            for label, attempt_content in attempts:
                payload = dict(kwargs)
                if attempt_content is not None:
                    payload["content"] = attempt_content
                resp = self.session.post(
                    self._api_url(f"posts/{post_id}"),
                    json=payload,
                    timeout=30,
                )

                if resp.status_code == 200:
                    if label != "original":
                        print("  Post update retry succeeded after removing iframe embeds.")
                    return resp.json()

                print(f"  Update failed ({label}): {resp.status_code} {resp.text[:300]}")
                if resp.status_code != 403 or label != "original":
                    continue
                if len(attempts) > 1:
                    print("  Retrying post update without iframe embeds...")

            if self._test_xmlrpc_connection():
                print("  REST更新に失敗したため、XML-RPCで再試行します。")
                return self._update_post_via_xmlrpc(post_id, **kwargs)

            return None

        except Exception as e:
            print(f"  Update error: {e}")
            if self._test_xmlrpc_connection():
                print("  REST例外のため、XML-RPCで再試行します。")
                return self._update_post_via_xmlrpc(post_id, **kwargs)
            return None

    def _update_post_via_xmlrpc(self, post_id: int, **kwargs) -> dict | None:
        acf_fields = kwargs.get("acf") or {}
        manual_html = acf_fields.get("custom_html_content")
        content = kwargs.get("content")
        if manual_html and not content:
            # REST 更新が落ちても for-wp 用HTMLの本文は同期しておく。
            content = manual_html

        post_data = self._build_xmlrpc_post_data(
            title=kwargs.get("title"),
            content=content,
            status=kwargs.get("status"),
            featured_media_id=kwargs.get("featured_media"),
            extra_fields={
                key: value
                for key, value in kwargs.items()
                if key not in {"title", "content", "status", "featured_media"}
            },
        )

        post_data.pop("post_type", None)
        post_data.pop("post_thumbnail", None)

        try:
            success = self.xmlrpc.wp.editPost(
                self.xmlrpc_blog_id,
                self.auth[0],
                self.auth[1],
                post_id,
                post_data,
            )
            if not success:
                print("  Update failed via XML-RPC: unknown error")
                return None
            if content:
                try:
                    self.xmlrpc.wp.editPost(
                        self.xmlrpc_blog_id,
                        self.auth[0],
                        self.auth[1],
                        post_id,
                        {"post_content": content},
                    )
                except Exception as retry_e:
                    print(f"  Content-only retry failed: {retry_e}")
            if manual_html:
                custom_field_ok = self.upsert_custom_fields(
                    post_id,
                    {"custom_html_content": manual_html},
                )
                if not custom_field_ok:
                    print("  Warning: custom_html_content sync via XML-RPC custom fields failed")
            return {
                "id": post_id,
                "link": self._get_post_link_via_xmlrpc(post_id),
                "featured_media": kwargs.get("featured_media", 0),
            }
        except Exception as e:
            print(f"  Update error via XML-RPC: {e}")
            return None

    def get_categories(self) -> list[dict]:
        """カテゴリ一覧を取得する"""
        if not self.rest_available and self.xmlrpc_available:
            try:
                categories = self.xmlrpc.wp.getTerms(
                    self.xmlrpc_blog_id,
                    self.auth[0],
                    self.auth[1],
                    "category",
                    {"number": 100},
                )
                return [
                    {
                        "id": int(item.get("term_id", 0)),
                        "name": item.get("name", ""),
                    }
                    for item in categories
                ]
            except Exception:
                return []

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
        if not self.rest_available and self.xmlrpc_available:
            try:
                term_id = self.xmlrpc.wp.newTerm(
                    self.xmlrpc_blog_id,
                    self.auth[0],
                    self.auth[1],
                    {"taxonomy": "category", "name": name},
                )
                return int(term_id)
            except Exception:
                return None

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
        if not self.rest_available and self.xmlrpc_available:
            try:
                items = self.xmlrpc.wp.getMediaLibrary(
                    self.xmlrpc_blog_id,
                    self.auth[0],
                    self.auth[1],
                    {"number": 100},
                )
                for item in items:
                    if item.get("link") == media_url:
                        return int(item.get("attachment_id", 0))
            except Exception:
                return None
            return None

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
            post = self.xmlrpc.wp.getPost(self.xmlrpc_blog_id, self.auth[0], self.auth[1], post_id)
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
                    self.xmlrpc_blog_id,
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


# ========================================
# 共通アセット配信（テーマ別）
# ========================================
# SWELL は post meta (`swell_meta_css` / `swell_meta_js`) を読むのでカスタムフィールドに流し込めば完了。
# SANGO はじめ他テーマはそのフィールドを無視するので、記事本文先頭に `<style>` / `<script>` ブロックを
# 注入する必要がある。その際 WordPress の wpautop フィルタが空行を `</p><p>` に変換して CSS/JS を壊すため、
# 注入前に空行を潰す。加えて kses が REST 経由の `<style>` を弾く場合があるので XML-RPC editPost で書き込む。
def _compact_asset_for_inline(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\n\s*\n+", "\n", text)


def build_inline_assets_block(css: str | None, js: str | None) -> str:
    # マーカーは <style> / <script> の内側に CSS/JS コメント形式で埋め込む。
    # 旧仕様の HTML コメント形式 (<!-- ... -->) は WordPress wpautop が
    # 単独 <p> で囲んでしまい本文冒頭に空白が生じるため、新仕様に統一する。
    block = ""
    if css:
        block += (
            f"<style>/* seo-article-common-css:start */"
            f"{_compact_asset_for_inline(css)}"
            f"/* seo-article-common-css:end */</style>\n"
        )
    if js:
        block += (
            f"<script>/* seo-article-common-js:start */"
            f"{_compact_asset_for_inline(js)}"
            f"/* seo-article-common-js:end */</script>\n"
        )
    return block


def strip_inline_assets_block(content: str) -> str:
    if not content:
        return content or ""
    # 新仕様: <style>...marker:start ... marker:end ...</style>
    content = re.sub(
        r'<style>\s*/\*\s*seo-article-common-css:start\s*\*/.*?/\*\s*seo-article-common-css:end\s*\*/\s*</style>\s*',
        "",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r'<script>\s*/\*\s*seo-article-common-js:start\s*\*/.*?/\*\s*seo-article-common-js:end\s*\*/\s*</script>\s*',
        "",
        content,
        flags=re.DOTALL,
    )
    # 旧仕様: HTML コメントが <style>/<script> の外側にあったブロック（後方互換）
    content = re.sub(
        re.escape(CSS_START_MARKER) + r".*?" + re.escape(CSS_END_MARKER) + r"\s*",
        "",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        re.escape(JS_START_MARKER) + r".*?" + re.escape(JS_END_MARKER) + r"\s*",
        "",
        content,
        flags=re.DOTALL,
    )
    return content


def detect_active_theme(client) -> str:
    """REST API の themes?status=active を見て 'swell' / 'sango' / 'other' を返す。失敗時は 'other'。
    swell_child などの子テーマも SWELL 系として認識する。SWELL でないものは `swell_meta_*` を読まないため
    全てインライン注入ルートに倒す。"""
    try:
        resp = client.session.get(
            f"{client.api_base}/themes",
            params={"status": "active"},
            timeout=15,
        )
        if resp.status_code == 200:
            for theme in resp.json():
                stylesheet = (theme.get("stylesheet") or "").lower()
                template = (theme.get("template") or "").lower()
                if "swell" in stylesheet or "swell" in template:
                    return "swell"
                if "sango" in stylesheet or "sango" in template:
                    return "sango"
    except Exception as e:
        print(f"  (theme detection failed: {e})")
    return "other"


def _xmlrpc_server_for(client):
    username, password = client.auth
    return xmlrpc.client.ServerProxy(client.xmlrpc_url, allow_none=True), username, password


def inject_inline_assets_via_xmlrpc(client, post_id: int, css: str | None, js: str | None) -> bool:
    """SANGO など <style> 本文注入が必要なテーマ向け。既存マーカー区間を剥がしてから先頭に差し替える。
    REST 経由だと kses に `<style>` を剥がされて 500 が返るので、XML-RPC editPost を使う。"""
    server, username, password = _xmlrpc_server_for(client)
    try:
        post = server.wp.getPost("0", username, password, post_id)
    except Exception as e:
        print(f"  XMLRPC getPost failed: {e}")
        return False
    raw = post.get("post_content", "") or ""
    cleaned = strip_inline_assets_block(raw)
    new_content = build_inline_assets_block(css, js) + cleaned
    try:
        ok = server.wp.editPost("0", username, password, post_id, {"post_content": new_content})
        return bool(ok)
    except Exception as e:
        print(f"  XMLRPC editPost failed: {e}")
        return False


def push_article_assets(client, post_id: int, assets: dict, theme: str | None = None) -> tuple[bool, str]:
    """テーマに応じて CSS/JS を配信する。戻り値は (成否, 使用されたテーマ名)。
    SWELL のみ `swell_meta_*` カスタムフィールド方式。それ以外（SANGO/SERUM/other）は本文に <style>/<script>
    ブロックをインライン注入する。"""
    resolved = theme or detect_active_theme(client)
    if resolved == "swell":
        ok = client.upsert_custom_fields(
            post_id,
            {
                SWELL_CUSTOM_CSS_KEY: assets.get("css"),
                SWELL_CUSTOM_JS_KEY: assets.get("js"),
                LEGACY_SWELL_CUSTOM_CSS_KEY: None,
                LEGACY_SWELL_CUSTOM_JS_KEY: None,
            },
        )
        return ok, resolved
    # SANGO/SERUM/その他: インライン注入（SWELL 以外は custom field を読まないので）
    ok = inject_inline_assets_via_xmlrpc(client, post_id, assets.get("css"), assets.get("js"))
    return ok, resolved
UPLOAD_FILENAME_MAX_STEM = 40
UPLOAD_IMAGE_OPTIMIZE_THRESHOLD = 1_000_000
UPLOAD_IMAGE_MAX_WIDTH = 1600
UPLOAD_IMAGE_JPEG_QUALITY = 82


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


def optimize_upload_image(
    file_path: str,
    filename: str,
    mime_type: str,
    file_data: bytes,
) -> tuple[str, str, bytes]:
    """大きい画像はアップロードしやすいサイズ/形式へ軽量化する。"""
    if Image is None:
        return filename, mime_type, file_data

    if not mime_type.startswith("image/"):
        return filename, mime_type, file_data

    if len(file_data) < UPLOAD_IMAGE_OPTIMIZE_THRESHOLD:
        return filename, mime_type, file_data

    try:
        with Image.open(file_path) as img:
            img.load()
            working = img.copy()
    except Exception:
        return filename, mime_type, file_data

    try:
        if working.width > UPLOAD_IMAGE_MAX_WIDTH:
            working.thumbnail((UPLOAD_IMAGE_MAX_WIDTH, UPLOAD_IMAGE_MAX_WIDTH * 4))

        if working.mode in ("RGBA", "LA") or (
            working.mode == "P" and "transparency" in working.info
        ):
            background = Image.new("RGB", working.size, "white")
            alpha = working.convert("RGBA")
            background.paste(alpha, mask=alpha.getchannel("A"))
            working = background
        elif working.mode != "RGB":
            working = working.convert("RGB")

        buffer = io.BytesIO()
        working.save(
            buffer,
            format="JPEG",
            quality=UPLOAD_IMAGE_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        optimized = buffer.getvalue()
        if len(optimized) >= len(file_data):
            return filename, mime_type, file_data

        optimized_name = f"{os.path.splitext(filename)[0]}.jpg"
        print(
            "    Optimized image for upload: "
            f"{filename} ({len(file_data) // 1024}KB -> {len(optimized) // 1024}KB)"
        )
        return optimized_name, "image/jpeg", optimized
    except Exception:
        return filename, mime_type, file_data


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


ARTICLE_SCOPE_CLASS = "ndm-article"
ARTICLE_SCOPE_OPEN_RE = re.compile(
    r'^\s*<article[^>]*class="[^"]*\b' + re.escape(ARTICLE_SCOPE_CLASS) + r'\b[^"]*"',
    re.IGNORECASE,
)


def wrap_article_scope(html_content: str) -> str:
    """記事本文を <article class="ndm-article"> でラップして、テーマ CSS の干渉を遮断する。
    既にラップ済みの場合は何もしない。"""
    if not html_content or not html_content.strip():
        return html_content
    if ARTICLE_SCOPE_OPEN_RE.search(html_content):
        return html_content
    body = html_content.strip()
    return f'<article class="{ARTICLE_SCOPE_CLASS}">\n{body}\n</article>'


def load_article_assets(html_dir: str) -> dict[str, str | None]:
    """共通アセットをベースに、必要なら記事ローカル上書きを連結する。

    古い article-common.css / .js が記事フォルダに残っていても、
    投稿時のデザインルールは output/article-common.css / .js を正本にする。
    記事単位の明示上書きが必要な場合のみ article-local.css / .js を使う。
    """
    local_css_path = os.path.join(html_dir, "article-local.css")
    local_js_path = os.path.join(html_dir, "article-local.js")
    legacy_css_path = os.path.join(html_dir, "article-common.css")
    legacy_js_path = os.path.join(html_dir, "article-common.js")
    fallback_css = os.path.join(OUTPUT_ROOT, "article-common.css")
    fallback_js = os.path.join(OUTPUT_ROOT, "article-common.js")

    assets = {"css": None, "js": None}
    if os.path.exists(fallback_css):
        with open(fallback_css, "r", encoding="utf-8") as f:
            assets["css"] = f.read()
    elif os.path.exists(legacy_css_path):
        with open(legacy_css_path, "r", encoding="utf-8") as f:
            assets["css"] = f.read()

    if os.path.exists(fallback_js):
        with open(fallback_js, "r", encoding="utf-8") as f:
            assets["js"] = f.read()
    elif os.path.exists(legacy_js_path):
        with open(legacy_js_path, "r", encoding="utf-8") as f:
            assets["js"] = f.read()

    if os.path.exists(local_css_path):
        with open(local_css_path, "r", encoding="utf-8") as f:
            local_css = f.read()
        if local_css.strip():
            base_css = assets["css"] or ""
            assets["css"] = base_css.rstrip() + "\n\n/* === Article Local CSS === */\n" + local_css

    if os.path.exists(local_js_path):
        with open(local_js_path, "r", encoding="utf-8") as f:
            local_js = f.read()
        if local_js.strip():
            base_js = assets["js"] or ""
            assets["js"] = base_js.rstrip() + "\n\n/* === Article Local JS === */\n" + local_js
    return assets


def load_post_assets(html_path: str) -> dict[str, str | None]:
    """WordPress投稿用アセットを読み込む。variant別テーマCSSも後段で連結する。"""
    html_dir = os.path.dirname(os.path.abspath(html_path))
    assets = load_article_assets(html_dir)
    variant_index = detect_variant_index_from_path(html_path)
    theme_path = get_variant_theme_css_path(variant_index)
    if theme_path:
        with open(theme_path, "r", encoding="utf-8") as f:
            theme_css = f.read()
        if assets.get("css") and theme_css:
            assets["css"] = assets["css"].rstrip() + (
                f"\n\n/* === Variant Theme v{variant_index} === */\n" + theme_css
            )
        elif theme_css:
            assets["css"] = theme_css
    return assets


def extract_post_title(html_content: str, html_path: str) -> str:
    """HTML内容からWordPress投稿タイトルを推定する。"""
    tag_structure_path = html_path
    if tag_structure_path.endswith("_記事_for-wp.html"):
        tag_structure_path = tag_structure_path.replace("_記事_for-wp.html", "_タグ構成.md")
    else:
        tag_structure_path = tag_structure_path.replace("_記事.html", "_タグ構成.md")
    if os.path.exists(tag_structure_path):
        try:
            with open(tag_structure_path, "r", encoding="utf-8") as f:
                tag_structure = f.read()
            title_patterns = (
                r"\|\s*\*\*titleタグ\*\*\s*\|\s*(.+?)\s*\|",
                r"\*\*titleタグ\*\*:\s*(.+)",
            )
            for pattern in title_patterns:
                match = re.search(pattern, tag_structure)
                if not match:
                    continue
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

    if re.search(r"\*\*[^*\n][^*\n]*\*\*", html_content):
        issues.append("Markdown の強調記法(**...**)が本文HTMLに残っています")

    return issues


def validate_html_for_custom_html_field(html_content: str, *, allow_local_images: bool = False) -> list[str]:
    """custom_html_content 用の検証。for-wp.html は <style>/<script> を含む前提。"""
    issues = []

    local_srcs = re.findall(r'src="(?!https?://)([^"]+)"', html_content, flags=re.IGNORECASE)
    if local_srcs and not allow_local_images:
        issues.append("ローカル画像参照が残っています: " + ", ".join(local_srcs[:5]))

    if re.search(r"\*\*[^*\n][^*\n]*\*\*", html_content):
        issues.append("Markdown の強調記法(**...**)が本文HTMLに残っています")

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
        manual_wp_html = is_manual_wp_html_path(args.html)
        assets = {"css": None, "js": None} if manual_wp_html else load_post_assets(args.html)
        output_key = infer_output_key_from_html_path(args.html)

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
        if not manual_wp_html:
            html_content = strip_existing_injected_assets(html_content)
            html_content = wrap_article_scope(html_content)

        if manual_wp_html:
            print("\nPreparing custom_html_content from for-wp HTML...")
            issues = validate_html_for_custom_html_field(html_content, allow_local_images=args.dry_run)
        else:
            print("\nPreparing post-level CSS/JS...")
            print(f"  CSS: {len(assets['css']) if assets['css'] else 0} chars")
            print(f"  JS: {len(assets['js']) if assets['js'] else 0} chars")
            issues = validate_html_for_wordpress(html_content, allow_local_images=args.dry_run)
        if issues:
            print("\nError: 更新前HTMLの検証に失敗しました。")
            for issue in issues:
                print(f"  - {issue}")
            sys.exit(1)

        if manual_wp_html:
            update_data = {
                "acf": {"custom_html_content": html_content},
                "content": html_content,
                "title": title,
            }
        else:
            update_data = {"content": html_content, "title": title}

        print(f"\nUpdating post ID: {args.post_id}...")
        if args.dry_run:
            if manual_wp_html:
                print(f"  [DRY RUN] Would update ACF custom_html_content ({len(html_content)} chars).")
                print(f"  [DRY RUN] Would also sync post content ({len(html_content)} chars).")
            else:
                print(f"  [DRY RUN] Would update post content ({len(html_content)} chars).")
                print("  [DRY RUN] Would sync SWELL post CSS/JS fields.")
            return

        result = client.update_post(args.post_id, **update_data)
        # featured_media は content と同時送信すると無視されるため別リクエストで更新
        if result and featured_media_id:
            fm_result = client.update_post(args.post_id, featured_media=featured_media_id)
            if fm_result and fm_result.get("featured_media") == featured_media_id:
                print(f"  Featured image updated (ID: {featured_media_id})")
            else:
                print(f"  Warning: featured_media update may have failed")
        if result:
            if manual_wp_html:
                print("  Updated _for-wp.html into both post content and ACF custom_html_content.")
            else:
                asset_ok, theme_used = push_article_assets(client, args.post_id, assets)
                if not asset_ok:
                    print(f"  Warning: common CSS/JS delivery failed (theme={theme_used}).")
                elif theme_used == "swell":
                    print("  Synced common CSS/JS to SWELL custom fields.")
                else:
                    print(f"  Synced common CSS/JS via inline injection (theme={theme_used}).")
            save_variant_status(
                output_key,
                {
                    "completed": True,
                    "post_id": args.post_id,
                    "link": result.get("link", ""),
                    "status": "updated",
                    "saved_at": datetime.now().isoformat(),
                    "source": "wp_post_update",
                },
            )
            print(f"  Updated: {result.get('link', '')}")
            edit_link = f"{site_url}/wp-admin/post.php?post={args.post_id}&action=edit"
            print(f"  Edit: {edit_link}")
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
    manual_wp_html = is_manual_wp_html_path(args.html)
    assets = {"css": None, "js": None} if manual_wp_html else load_post_assets(args.html)
    output_key = infer_output_key_from_html_path(args.html)

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
    if not manual_wp_html:
        html_content = strip_existing_injected_assets(html_content)

    if manual_wp_html:
        print("\nPreparing custom_html_content from for-wp HTML...")
        issues = validate_html_for_custom_html_field(html_content, allow_local_images=args.dry_run)
    else:
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
        if manual_wp_html:
            print(f"  custom_html_content length: {len(html_content)} chars")
        else:
            print(f"  Content length: {len(html_content)} chars")
        print(f"  Images uploaded: {len(url_map)}")
        print(f"  Featured media: {featured_media_id}")
        if not manual_wp_html:
            print(f"  SWELL CSS: {len(assets['css']) if assets['css'] else 0} chars")
            print(f"  SWELL JS: {len(assets['js']) if assets['js'] else 0} chars")
        return

    print("\nCreating post...")
    result = client.create_post(
        title=title,
        content="" if manual_wp_html else html_content,
        status=args.status,
        category_ids=category_ids,
        featured_media_id=featured_media_id,
        extra_fields={"acf": {"custom_html_content": html_content}} if manual_wp_html else None,
    )

    if result:
        post_id = result.get("id", "")
        post_link = result.get("link", "")
        edit_link = f"{site_url}/wp-admin/post.php?post={post_id}&action=edit"

        asset_ok, theme_used = (True, "custom_html_content") if manual_wp_html else push_article_assets(client, post_id, assets)
        print(f"\n{'=' * 60}")
        print(f"Post created successfully!")
        print(f"  ID: {post_id}")
        print(f"  Status: {args.status}")
        print(f"  URL: {post_link}")
        print(f"  Edit: {edit_link}")
        if manual_wp_html:
            print("  custom_html_content: posted directly from _for-wp.html")
        elif not asset_ok:
            print(f"  Common CSS/JS: WARNING - delivery failed (theme={theme_used})")
        elif theme_used == "swell":
            print("  Common CSS/JS: synced to SWELL custom fields")
        else:
            print(f"  Common CSS/JS: synced via inline injection (theme={theme_used})")
        save_variant_status(
            output_key,
            {
                "completed": True,
                "post_id": post_id,
                "link": post_link,
                "status": args.status,
                "saved_at": datetime.now().isoformat(),
                "source": "wp_post_create",
            },
        )
        print(f"{'=' * 60}")

        if args.status == "draft":
            print("\nDraft created. Review and publish from the WordPress admin screen if needed.")
    else:
        print("\nError: 投稿に失敗しました。")
        sys.exit(1)


if __name__ == "__main__":
    main()
