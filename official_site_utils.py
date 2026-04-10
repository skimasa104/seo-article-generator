import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from env_utils import load_project_env


SERPER_API_URL = "https://google.serper.dev/search"
SEARCH_TIMEOUT = 20
MIN_OFFICIAL_SCORE = 8

NON_OFFICIAL_DOMAINS = {
    "google.com",
    "google.co.jp",
    "youtube.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "tiktok.com",
    "wikipedia.org",
    "note.com",
    "ameblo.jp",
    "line.me",
    "moteo.best",
    "aga-ikumo.com",
    "aga-clinic-navi.com",
    "nakanocentralpark-dentaloffice.com",
    "ranking.goo.ne.jp",
    "clinicfor.life",
    "newsrelea.se",
    "travelbook.co.jp",
    "gangnamunni.com",
    "kireisearch.jp",
    "kanja.jp",
    "prtimes.jp",
}

NON_OFFICIAL_KEYWORDS = [
    "ランキング",
    "比較",
    "おすすめ",
    "口コミ",
    "評判",
    "まとめ",
    "コラム",
    "メディア",
    "ブログ",
    "記事",
    "解説",
    "ナビ",
    "best",
    "media",
    "column",
    "blog",
    "tips",
]

OFFICIAL_POSITIVE_KEYWORDS = [
    "公式",
    "公式サイト",
    "オフィシャル",
    "クリニック公式",
]

DECORATION_PATTERNS = [
    r"【[^】]+】",
    r"（[^）]*公式[^）]*）",
    r"\([^)]*official[^)]*\)",
]

GENERIC_SUFFIX_PATTERNS = [
    r"\s*公式サイト$",
    r"\s*公式$",
    r"\s*クリニック$",
    r"\s*医院$",
    r"\s*本院$",
    r"\s*院$",
]

BRAND_SUFFIX_PATTERN = re.compile(
    r"^(.+?(?:メディカルクリニック|メディカルサロン|クリニック|医院|病院|皮膚科|内科|外来|サロン|センター))"
)

load_project_env()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", "", text or "")).lower()


def normalize_clinic_lookup_name(name: str) -> str:
    cleaned = name or ""
    for pattern in DECORATION_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def strip_generic_suffixes(name: str) -> str:
    cleaned = normalize_clinic_lookup_name(name)
    for pattern in GENERIC_SUFFIX_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_lookup_name_variants(name: str) -> list[str]:
    base = normalize_clinic_lookup_name(name)
    variants: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value or "").strip()
        if value and value not in variants:
            variants.append(value)

    add(base)
    stripped = strip_generic_suffixes(name)
    add(stripped)

    if "・" in base:
        add(base.split("・", 1)[0])

    match = BRAND_SUFFIX_PATTERN.match(base)
    if match:
        add(match.group(1))

    return variants


def tokenize_name(name: str) -> list[str]:
    ascii_tokens = re.findall(r"[a-z0-9]{3,}", (name or "").lower())
    jp_tokens = re.findall(r"[\u3040-\u30ff\u3400-\u9fff]{2,}", name or "")
    tokens = ascii_tokens + jp_tokens
    return [t for t in tokens if t not in {"clinic", "aga", "院", "新宿", "本院"}]


def build_query_variants(name: str) -> list[str]:
    queries = []
    for variant in extract_lookup_name_variants(name):
        queries.extend([
            f"{variant} 公式サイト",
            f"{variant} 公式",
            f"{variant} クリニック 公式",
        ])
    deduped = []
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in deduped:
            deduped.append(query)
    return deduped


def get_hostname(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def canonicalize_candidate_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return url
    clean = parsed._replace(query="", fragment="")
    canonical = urllib.parse.urlunparse(clean)
    if canonical.endswith("/") and clean.path not in {"", "/"}:
        canonical = canonical.rstrip("/")
    return canonical


def is_banned_domain(url: str) -> bool:
    hostname = get_hostname(url)
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in NON_OFFICIAL_DOMAINS)


def score_official_candidate(name: str, title: str, snippet: str, url: str) -> int:
    if not url or is_banned_domain(url):
        return -999

    combined = " ".join([title or "", snippet or "", url or ""])
    normalized_combined = normalize_text(combined)
    normalized_name = normalize_text(name)
    hostname = get_hostname(url)
    path = urllib.parse.urlparse(url).path.lower()
    score = 0

    if normalized_name and normalized_name in normalized_combined:
        score += 6

    normalized_title = normalize_text(title)
    if normalized_name and normalized_name in normalized_title:
        score += 4

    if any(keyword in combined for keyword in OFFICIAL_POSITIVE_KEYWORDS):
        score += 8

    tokens = tokenize_name(name)
    token_hits = 0
    lowered_combined = combined.lower()
    for token in tokens:
        lowered_token = token.lower()
        if lowered_token in lowered_combined or lowered_token in hostname:
            token_hits += 1
    score += min(token_hits * 2, 6)

    hostname_hits = [token for token in tokens if token.lower() in hostname]
    if tokens and not hostname_hits:
        if any(keyword in combined for keyword in OFFICIAL_POSITIVE_KEYWORDS):
            score -= 2
        else:
            score -= 6

    if any(keyword in path for keyword in ["/tag/", "/tags/", "/category/", "/categories/", "/goods/", "/events/", "/introduce/", "/lp/", "/reservation", "/reserva/"]):
        score -= 6

    if path in {"", "/"} or path.count("/") <= 2:
        score += 2
    if any(keyword in path for keyword in ["/clinic", "/clinics", "/aga", "/treatment", "/medical", "/menu"]):
        score += 2

    if any(keyword in combined for keyword in NON_OFFICIAL_KEYWORDS):
        score -= 4
    if any(keyword in path for keyword in ["/column", "/blog", "/media", "/article", "/tips", "/feature"]):
        score -= 4

    if re.search(r"/column|/blog|/media|/article|/tips|/feature", path):
        score -= 4

    return score


def search_official_candidates_by_serper(query: str, target_name: str, num: int = 5) -> list[dict[str, Any]]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        return []

    body = json.dumps({
        "q": query,
        "gl": "jp",
        "hl": "ja",
        "num": num,
    }).encode("utf-8")

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(SERPER_API_URL, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []

    candidates = []
    for item in data.get("organic", []):
        link = canonicalize_candidate_url(item.get("link", ""))
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        score = score_official_candidate(target_name, title, snippet, link)
        candidates.append({
            "url": link,
            "title": title,
            "snippet": snippet,
            "score": score,
            "query": query,
        })
    return candidates


def merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_url: dict[str, dict[str, Any]] = {}
    for item in candidates:
        url = item.get("url")
        if not url:
            continue
        existing = best_by_url.get(url)
        if existing is None or item.get("score", -999) > existing.get("score", -999):
            best_by_url[url] = item
    merged = list(best_by_url.values())
    merged.sort(key=lambda item: item.get("score", -999), reverse=True)
    return merged


def find_official_url(name: str) -> tuple[str | None, list[dict[str, Any]]]:
    queries = build_query_variants(name)
    candidates: list[dict[str, Any]] = []
    for query in queries:
        candidates.extend(search_official_candidates_by_serper(query, name))
    candidates = merge_candidates(candidates)
    if not candidates:
        return None, []

    best = candidates[0]
    if best.get("score", -999) < MIN_OFFICIAL_SCORE:
        return None, candidates
    return best.get("url"), candidates
