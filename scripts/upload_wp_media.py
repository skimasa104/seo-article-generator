#!/usr/bin/env python3
"""
単一画像を WordPress メディアライブラリへアップロードする補助スクリプト。

使い方:
  .venv/bin/python scripts/upload_wp_media.py \
    --site sites/nandemo.json \
    --file output/包茎_大阪__nandemo_v2/images/screenshot_MSクリニック.png \
    --alt "MSクリニックの公式サイト"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from wp_post import WordPressClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a single media file to WordPress")
    parser.add_argument("--site", required=True, help="sites/*.json")
    parser.add_argument("--file", required=True, help="local media file path")
    parser.add_argument("--alt", default="", help="alt text")
    args = parser.parse_args()

    if not os.path.exists(args.site):
        print(f"site config not found: {args.site}", file=sys.stderr)
        return 1
    if not os.path.exists(args.file):
        print(f"media file not found: {args.file}", file=sys.stderr)
        return 1

    with open(args.site, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    client = WordPressClient(
        cfg["site_url"],
        cfg["username"],
        cfg["app_password"],
        rest_api_base=cfg.get("rest_api_base"),
        xmlrpc_url=cfg.get("xmlrpc_url"),
    )

    if not client.test_connection():
        print("failed to connect to WordPress", file=sys.stderr)
        return 1

    result = client.upload_media(args.file, args.alt)
    if not result:
        print("upload failed", file=sys.stderr)
        return 1

    print(result["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
