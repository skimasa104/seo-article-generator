import json
import os
import shutil


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ROOT = os.path.join(SCRIPT_DIR, "output")
SCRAPED_ROOT = os.path.join(SCRIPT_DIR, "scraped_data")
VARIANT_STATUS_FILENAME = ".variant_status.json"


def keyword_to_slug(keyword: str) -> str:
    return keyword.replace(" ", "_")


def resolve_output_key(keyword: str, output_key: str | None = None) -> str:
    return output_key or keyword


def get_output_dir_for_key(output_key: str) -> str:
    return os.path.join(OUTPUT_ROOT, keyword_to_slug(output_key))


def ensure_output_dir_for_key(output_key: str) -> str:
    output_dir = get_output_dir_for_key(output_key)
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def get_keyword_output_dir(keyword: str) -> str:
    return get_output_dir_for_key(keyword)


def ensure_keyword_output_dir(keyword: str) -> str:
    return ensure_output_dir_for_key(keyword)


def ensure_keyword_images_dir(keyword: str, output_key: str | None = None) -> str:
    images_dir = os.path.join(ensure_output_dir_for_key(resolve_output_key(keyword, output_key)), "images")
    os.makedirs(images_dir, exist_ok=True)
    return images_dir


def ensure_common_assets_for_key(output_key: str) -> None:
    output_dir = ensure_output_dir_for_key(output_key)
    for filename in ("article-common.css", "article-common.js"):
        src = os.path.join(OUTPUT_ROOT, filename)
        dest = os.path.join(output_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, dest)


def ensure_common_assets(keyword: str, output_key: str | None = None) -> None:
    ensure_common_assets_for_key(resolve_output_key(keyword, output_key))


def get_keyword_scraped_dir(keyword: str) -> str:
    return os.path.join(SCRAPED_ROOT, keyword_to_slug(keyword))


def ensure_keyword_scraped_dir(keyword: str) -> str:
    scraped_dir = get_keyword_scraped_dir(keyword)
    os.makedirs(scraped_dir, exist_ok=True)
    return scraped_dir


def get_variant_status_path(output_key: str) -> str:
    return os.path.join(get_output_dir_for_key(output_key), VARIANT_STATUS_FILENAME)


def load_variant_status(output_key: str) -> dict | None:
    path = get_variant_status_path(output_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_variant_status(output_key: str, payload: dict) -> str:
    path = get_variant_status_path(output_key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path
