#!/usr/bin/env python3
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wp_post import WordPressClient

SITE_PATH = ROOT / "sites/nandemo.json"
LOCAL_HTML = ROOT / "output/AGA_名古屋__nandemo_v5/AGA_名古屋__nandemo_v5_記事_for-wp.html"
POST_ID = 25317


def extract_local_v1_block(html: str) -> str:
    start = html.find("<style>\n.ndm-aga-v1{")
    if start < 0:
        raise RuntimeError("local ndm-aga-v1 style block not found")
    sec = html.find('<section class="ndm-aga-v1">', start)
    end = html.find("</section>", sec)
    if sec < 0 or end < 0:
        raise RuntimeError("local ndm-aga-v1 section not found")
    return html[start:end + len("</section>")]


def main() -> int:
    site = json.loads(SITE_PATH.read_text(encoding="utf-8"))
    local_html = LOCAL_HTML.read_text(encoding="utf-8")
    replacement_v1 = extract_local_v1_block(local_html)

    client = WordPressClient(
        site_url=site["site_url"],
        username=site["username"],
        app_password=site["app_password"],
        rest_api_base=site.get("rest_api_base"),
        xmlrpc_url=site.get("xmlrpc_url"),
    )
    if not client.test_connection():
        raise SystemExit("WordPress connection failed.")

    resp = client.session.get(
        client._api_url(f"posts/{POST_ID}"),
        params={"context": "edit", "_fields": "content"},
        timeout=30,
    )
    resp.raise_for_status()
    current_raw = ((resp.json().get("content") or {}).get("raw") or "")
    original = current_raw

    # 1. Drop the stale leading h2 override block that comes before the common CSS.
    current_raw = re.sub(
        r'^<style>\.ndm-article h2:not\(\[class\*="ndm-aga"\]\).*?</style>\s*',
        "",
        current_raw,
        count=1,
        flags=re.DOTALL,
    )

    # 2. Replace the old v1h comparison block with the current local v1 comparison block.
    current_raw, replaced = re.subn(
        r'<style>\s*\.ndm-aga-v1h\{.*?</section>',
        replacement_v1,
        current_raw,
        count=1,
        flags=re.DOTALL,
    )

    print(f"leading_override_removed={original != current_raw}")
    print(f"v1h_block_replaced={replaced}")
    print(f"new_len={len(current_raw)} old_len={len(original)}")
    print(f"has_v1h={'ndm-aga-v1h' in current_raw}")
    print(f"has_v1={'<section class=\"ndm-aga-v1\">' in current_raw}")

    if current_raw == original:
        print("No changes detected; aborting update.")
        return 0

    update = client.session.post(
        client._api_url(f"posts/{POST_ID}"),
        json={"content": current_raw},
        timeout=60,
    )
    print(f"update_status={update.status_code}")
    print(update.text[:500])

    verify = client.session.get(
        client._api_url(f"posts/{POST_ID}"),
        params={"context": "edit", "_fields": "content"},
        timeout=30,
    )
    print(f"verify_status={verify.status_code}")
    if verify.status_code == 200:
        raw = ((verify.json().get("content") or {}).get("raw") or "")
        print(f"verify_has_v1h={'ndm-aga-v1h' in raw}")
        print(f"verify_has_v1={'<section class=\"ndm-aga-v1\">' in raw}")
        print(f"verify_startswith_common={raw.startswith('<style>.ndm-article{')}")
        print(f"verify_len={len(raw)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
