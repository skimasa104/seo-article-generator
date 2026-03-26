#!/usr/local/bin/python3.12
"""
WordPress記事投稿スクリプト

記事HTMLをWordPress REST API経由で下書き投稿する。
画像はメディアライブラリに自動アップロードし、HTMLのパスを差し替える。

使い方:
  python3.12 wp_post.py \
    --html output/aga_横浜_記事.html \
    --site sites/example.json \
    --title "横浜のAGAおすすめクリニック11選｜費用・治療内容を徹底比較" \
    --category "AGA"

  # 下書き確認後に公開する場合
  python3.12 wp_post.py --publish --post-id 123 --site sites/example.json

サイト設定ファイル (sites/example.json):
  {
    "site_url": "https://example.com",
    "username": "admin",
    "app_password": "xxxx xxxx xxxx xxxx"
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
    def __init__(self, site_url: str, username: str, app_password: str):
        self.site_url = site_url.rstrip("/")
        self.api_base = f"{self.site_url}/wp-json/wp/v2"
        self.auth = (username, app_password)
        self.session = requests.Session()
        self.session.auth = self.auth

    def test_connection(self) -> bool:
        """API接続テスト"""
        try:
            resp = self.session.get(f"{self.api_base}/users/me", timeout=10)
            if resp.status_code == 200:
                user = resp.json()
                print(f"  Connected as: {user.get('name', 'unknown')}")
                return True
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

        filename = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "image/png"

        with open(file_path, "rb") as f:
            file_data = f.read()

        headers = {
            "Content-Disposition": f'attachment; filename="{urllib.parse.quote(filename)}"',
            "Content-Type": mime_type,
        }

        try:
            resp = self.session.post(
                f"{self.api_base}/media",
                data=file_data,
                headers=headers,
                timeout=60,
            )

            if resp.status_code == 201:
                media = resp.json()
                media_url = media.get("source_url", "")
                media_id = media.get("id", 0)

                # alt テキストを設定
                if alt_text:
                    self.session.post(
                        f"{self.api_base}/media/{media_id}",
                        json={"alt_text": alt_text},
                        timeout=10,
                    )

                return {
                    "id": media_id,
                    "url": media_url,
                    "filename": filename,
                }
            else:
                print(f"    Upload failed: {resp.status_code} {resp.text[:200]}")
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
                f"{self.api_base}/posts",
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
                f"{self.api_base}/posts/{post_id}",
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
                f"{self.api_base}/categories",
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
                f"{self.api_base}/categories",
                json={"name": name},
                timeout=10,
            )
            if resp.status_code == 201:
                return resp.json()["id"]
            return None
        except Exception:
            return None


# ========================================
# 画像URLキャッシュ
# ========================================
CACHE_FILE = "output/.image_url_cache.json"


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


# ========================================
# 画像処理
# ========================================
def find_local_images(html_content: str, html_dir: str) -> list[dict]:
    """HTML内のローカル画像パスを抽出する"""
    images = []
    # src="images/xxx.png" or src="xxx.png" のパターン
    img_pattern = re.compile(
        r'<img\s[^>]*src="([^"]+)"[^>]*alt="([^"]*)"[^>]*/?>',
        re.IGNORECASE,
    )

    for match in img_pattern.finditer(html_content):
        src = match.group(1)
        alt = match.group(2)

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
                "original_tag": match.group(0),
            })

    return images


def replace_image_urls(html_content: str, url_map: dict[str, str]) -> str:
    """HTML内のローカル画像パスをWordPress URLに置換する"""
    for local_src, wp_url in url_map.items():
        html_content = html_content.replace(f'src="{local_src}"', f'src="{wp_url}"')
    return html_content


# ========================================
# CSS/JS 埋め込み
# ========================================
def inject_css_js(html_content: str, html_dir: str) -> str:
    """article-common.css / .js があれば HTML末尾に埋め込む"""
    # output/ ディレクトリ内の共通ファイルを探す
    css_path = os.path.join(html_dir, "article-common.css")
    js_path = os.path.join(html_dir, "article-common.js")

    injection = ""

    if os.path.exists(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            css_content = f.read()
        injection += f"\n<style>\n{css_content}\n</style>\n"
        print(f"  Injected CSS ({len(css_content)} chars)")

    if os.path.exists(js_path):
        with open(js_path, "r", encoding="utf-8") as f:
            js_content = f.read()
        injection += f"\n<script>\n{js_content}\n</script>\n"
        print(f"  Injected JS ({len(js_content)} chars)")

    if injection:
        html_content += injection

    return html_content


# ========================================
# メイン
# ========================================
def main():
    parser = argparse.ArgumentParser(description="WordPress記事投稿スクリプト")
    parser.add_argument("--html", type=str, help="記事HTMLファイルのパス")
    parser.add_argument("--site", required=True, help="サイト設定JSONファイルのパス")
    parser.add_argument("--title", type=str, help="記事タイトル")
    parser.add_argument("--category", type=str, help="カテゴリ名（なければ自動作成）")
    parser.add_argument("--status", default="draft", choices=["draft", "publish", "pending"],
                        help="投稿ステータス（デフォルト: draft）")
    parser.add_argument("--publish", action="store_true",
                        help="既存記事を公開する（--post-id と併用）")
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

    print(f"Site: {site_url}")

    # クライアント初期化
    client = WordPressClient(site_url, username, app_password)

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

        # 画像処理（キャッシュ対応）
        cache = load_image_cache()
        images = find_local_images(html_content, html_dir)
        url_map = {}
        featured_media_id = None

        if images:
            print(f"\nFound {len(images)} local images:")
            for img in images:
                src = img["src"]
                if src in cache:
                    url_map[src] = cache[src]
                    print(f"  - {src} → cached: {cache[src]}")
                elif not args.skip_images and not args.dry_run:
                    print(f"  Uploading: {src}")
                    result = client.upload_media(img["local_path"], img["alt"])
                    if result:
                        url_map[src] = result["url"]
                        cache[src] = result["url"]
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

        # CSS/JS埋め込み
        print("\nInjecting CSS/JS...")
        html_content = inject_css_js(html_content, html_dir)

        update_data = {"content": html_content}
        if args.title:
            update_data["title"] = args.title
        if featured_media_id:
            update_data["featured_media"] = featured_media_id

        print(f"\nUpdating post ID: {args.post_id}...")
        if args.dry_run:
            print(f"  [DRY RUN] Would update post content ({len(html_content)} chars).")
            return

        result = client.update_post(args.post_id, **update_data)
        if result:
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

    # タイトル
    title = args.title
    if not title:
        # HTMLファイル名からタイトルを推測
        basename = os.path.splitext(os.path.basename(args.html))[0]
        title = basename.replace("_", " ")
        print(f"  Title (auto): {title}")

    print(f"\nTitle: {title}")
    print(f"Status: {args.status}")

    # 画像処理（キャッシュ対応）
    cache = load_image_cache()
    url_map = {}
    featured_media_id = None
    images = find_local_images(html_content, html_dir)

    if images:
        print(f"\nFound {len(images)} local images:")
        for img in images:
            src = img["src"]
            if src in cache and args.skip_images:
                url_map[src] = cache[src]
                print(f"  - {src} → cached: {cache[src]}")
            elif not args.skip_images and not args.dry_run:
                print(f"  Uploading: {src}")
                result = client.upload_media(img["local_path"], img["alt"])
                if result:
                    url_map[src] = result["url"]
                    cache[src] = result["url"]
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

    # CSS/JS埋め込み
    print("\nInjecting CSS/JS...")
    html_content = inject_css_js(html_content, html_dir)

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

        print(f"\n{'=' * 60}")
        print(f"Post created successfully!")
        print(f"  ID: {post_id}")
        print(f"  Status: {args.status}")
        print(f"  URL: {post_link}")
        print(f"  Edit: {edit_link}")
        print(f"{'=' * 60}")

        # 公開コマンドを表示
        if args.status == "draft":
            print(f"\nTo publish:")
            print(f"  python3.12 wp_post.py --publish --post-id {post_id} --site {args.site}")
    else:
        print("\nError: 投稿に失敗しました。")
        sys.exit(1)


if __name__ == "__main__":
    main()
