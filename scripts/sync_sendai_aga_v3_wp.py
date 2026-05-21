#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_post import WordPressClient


POST_ID = 25286
HTML_PATH = ROOT / "output/AGA_仙台__nandemo_v3/AGA_仙台__nandemo_v3_記事_for-wp.html"
SITE_PATH = ROOT / "sites/nandemo.json"


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def fetch_post(client: WordPressClient) -> dict:
    resp = client.session.get(
        client._api_url(f"posts/{POST_ID}"),
        params={"context": "edit", "_fields": "id,status,title,content,acf"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    site = json.loads(SITE_PATH.read_text(encoding="utf-8"))
    html = HTML_PATH.read_text(encoding="utf-8")

    client = WordPressClient(
        site_url=site["site_url"],
        username=site["username"],
        app_password=site["app_password"],
        rest_api_base=site.get("rest_api_base"),
        xmlrpc_url=site.get("xmlrpc_url"),
    )
    if not client.test_connection():
        raise SystemExit("WordPress connection failed.")

    acf_resp = client.session.post(
        client._api_url(f"posts/{POST_ID}"),
        json={"acf": {"custom_html_content": html}},
        timeout=30,
    )

    update_result = client.update_post(
        POST_ID,
        content=html,
        title="仙台のAGA治療｜対面13院×オンライン2院を診療スタイル別に比較【2026年最新15院】V3",
    )

    verify = fetch_post(client)
    post_content = ((verify.get("content") or {}).get("raw")) or ""
    acf_content = ((verify.get("acf") or {}).get("custom_html_content")) or ""

    print(
        json.dumps(
            {
                "post_id": POST_ID,
                "title": ((verify.get("title") or {}).get("raw")) or "",
                "status": verify.get("status") or "",
                "acf_update_status": acf_resp.status_code,
                "post_update_ok": update_result is not None,
                "post_content_match": post_content == html,
                "post_content_match_rstrip": post_content == html.rstrip("\n"),
                "acf_content_match": acf_content == html,
                "local_len": len(html),
                "post_len": len(post_content),
                "acf_len": len(acf_content),
                "local_sha1": sha1_text(html),
                "post_sha1": sha1_text(post_content),
                "acf_sha1": sha1_text(acf_content),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
