import os
import shutil


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ROOT = os.path.join(SCRIPT_DIR, "output")
SCRAPED_ROOT = os.path.join(SCRIPT_DIR, "scraped_data")


def keyword_to_slug(keyword: str) -> str:
    return keyword.replace(" ", "_")


def get_keyword_output_dir(keyword: str) -> str:
    return os.path.join(OUTPUT_ROOT, keyword_to_slug(keyword))


def ensure_keyword_output_dir(keyword: str) -> str:
    output_dir = get_keyword_output_dir(keyword)
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def ensure_keyword_images_dir(keyword: str) -> str:
    images_dir = os.path.join(ensure_keyword_output_dir(keyword), "images")
    os.makedirs(images_dir, exist_ok=True)
    return images_dir


def ensure_common_assets(keyword: str) -> None:
    output_dir = ensure_keyword_output_dir(keyword)
    for filename in ("article-common.css", "article-common.js"):
        src = os.path.join(OUTPUT_ROOT, filename)
        dest = os.path.join(output_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, dest)


def get_keyword_scraped_dir(keyword: str) -> str:
    return os.path.join(SCRAPED_ROOT, keyword_to_slug(keyword))


def ensure_keyword_scraped_dir(keyword: str) -> str:
    scraped_dir = get_keyword_scraped_dir(keyword)
    os.makedirs(scraped_dir, exist_ok=True)
    return scraped_dir
