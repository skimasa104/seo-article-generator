#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_post import WordPressClient


SITE_PATH = ROOT / "sites/nandemo.json"

VARIANTS = {
    2: {
        "post_id": 25282,
        "html_path": ROOT / "output/AGA_名古屋__nandemo_v2/AGA_名古屋__nandemo_v2_記事_for-wp.html",
    },
    3: {
        "post_id": None,
        "html_path": ROOT / "output/AGA_名古屋__nandemo_v3/AGA_名古屋__nandemo_v3_記事_for-wp.html",
        "note": "WordPress下書きが見つからないためスキップ",
    },
    4: {
        "post_id": 25288,
        "html_path": ROOT / "output/AGA_名古屋__nandemo_v4/AGA_名古屋__nandemo_v4_記事_for-wp.html",
    },
    5: {
        "post_id": 25317,
        "html_path": ROOT / "output/AGA_名古屋__nandemo_v5/AGA_名古屋__nandemo_v5_記事_for-wp.html",
    },
}


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def update_acf(client: WordPressClient, post_id: int, html: str) -> tuple[bool, str]:
    resp = client.session.post(
        client._api_url(f"posts/{post_id}"),
        json={"acf": {"custom_html_content": html}},
        timeout=30,
    )
    if resp.status_code == 200:
        return True, "ok"
    return False, f"{resp.status_code} {resp.text[:200]}"


def fallback_replace_post_content(client: WordPressClient, post_id: int, current_raw: str, html: str) -> bool:
    fallback = client.session.post(
        f"{client.site_url}/wp-json/search-regex/v1/source/posts/row/{post_id}",
        json={
            "replacement": {
                "column": "post_content",
                "operation": "replace",
                "source": "posts",
                "searchValue": current_raw,
                "replaceValue": html,
                "posId": 0,
            },
            "searchPhrase": current_raw,
            "source": ["posts"],
            "action": "replace",
        },
        timeout=60,
    )
    return fallback.status_code == 200


def fetch_post(client: WordPressClient, post_id: int) -> dict:
    resp = client.session.get(
        client._api_url(f"posts/{post_id}"),
        params={"context": "edit", "_fields": "id,status,title,content,acf"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def sync_variant(client: WordPressClient, variant: int, config: dict) -> dict:
    post_id = config["post_id"]
    html_path = config["html_path"]
    html = html_path.read_text(encoding="utf-8")

    current = fetch_post(client, post_id)
    current_raw = ((current.get("content") or {}).get("raw")) or ""

    acf_ok, acf_msg = update_acf(client, post_id, html)

    update_result = client.update_post(post_id, content=html)
    post_ok = update_result is not None
    if not post_ok and current_raw:
        post_ok = fallback_replace_post_content(client, post_id, current_raw, html)

    verify = fetch_post(client, post_id)
    post_content = ((verify.get("content") or {}).get("raw")) or ""
    acf_content = ((verify.get("acf") or {}).get("custom_html_content")) or ""

    return {
        "variant": variant,
        "post_id": post_id,
        "title": ((verify.get("title") or {}).get("raw")) or "",
        "status": verify.get("status") or "",
        "html_path": str(html_path),
        "acf_update_ok": acf_ok,
        "acf_update_msg": acf_msg,
        "post_update_ok": post_ok,
        "post_content_match": post_content == html,
        "acf_content_match": acf_content == html,
        "local_len": len(html),
        "post_len": len(post_content),
        "acf_len": len(acf_content),
        "local_sha1": sha1_text(html),
        "post_sha1": sha1_text(post_content),
        "acf_sha1": sha1_text(acf_content),
    }


def main() -> int:
    site = json.loads(SITE_PATH.read_text(encoding="utf-8"))
    client = WordPressClient(
        site_url=site["site_url"],
        username=site["username"],
        app_password=site["app_password"],
        rest_api_base=site.get("rest_api_base"),
        xmlrpc_url=site.get("xmlrpc_url"),
    )
    if not client.test_connection():
        raise SystemExit("WordPress connection failed.")

    results = []
    for variant, config in VARIANTS.items():
        if not config.get("post_id"):
            results.append(
                {
                    "variant": variant,
                    "post_id": None,
                    "title": "",
                    "status": "missing",
                    "html_path": str(config["html_path"]),
                    "note": config.get("note", "skip"),
                }
            )
            continue
        results.append(sync_variant(client, variant, config))

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
