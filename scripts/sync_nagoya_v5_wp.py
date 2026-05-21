#!/usr/bin/env python3
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_post import WordPressClient


POST_ID = 25317
HTML_PATH = ROOT / "output/AGA_名古屋__nandemo_v5/AGA_名古屋__nandemo_v5_記事_for-wp.html"
SITE_PATH = ROOT / "sites/nandemo.json"


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
    print(f"ACF update: {acf_resp.status_code}")
    if acf_resp.status_code != 200:
        print(acf_resp.text[:500])

    current_post = client.session.get(
        client._api_url(f"posts/{POST_ID}"),
        params={"context": "edit"},
        timeout=30,
    )
    current_raw = ""
    if current_post.status_code == 200:
        current_raw = ((current_post.json().get("content") or {}).get("raw") or "")

    update_result = client.update_post(POST_ID, content=html)
    print(f"post_content update: {'ok' if update_result else 'failed'}")
    if not update_result:
        fallback = client.session.post(
            f"{client.site_url}/wp-json/search-regex/v1/source/posts/row/{POST_ID}",
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
        print(f"search-regex fallback: {fallback.status_code}")
        if fallback.status_code != 200:
            print(fallback.text[:500])

    verify = client.session.get(
        client._api_url(f"posts/{POST_ID}"),
        params={"context": "edit"},
        timeout=30,
    )
    print(f"verify fetch: {verify.status_code}")
    if verify.status_code == 200:
        data = verify.json()
        raw = (data.get("content") or {}).get("raw", "")
        print(f"contains sendai-v5-list-tbl: {'sendai-v5-list-tbl' in raw}")
        print(f"contains ndm-aga-v1h: {'ndm-aga-v1h' in raw}")
        print(f"contains fancy h2 copy: {'名古屋でAGA治療を選ぶなら夜間・土日診療と通いやすさから見よう' in raw}")
        print(f"raw length: {len(raw)}")
    else:
        print(verify.text[:500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
