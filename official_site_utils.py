import glob
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

from env_utils import load_project_env
from output_utils import OUTPUT_ROOT


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
    "newsrelea.se",
    "travelbook.co.jp",
    "gangnamunni.com",
    "kireisearch.jp",
    "kanja.jp",
    "family-dr.jp",
    "prtimes.jp",
    "befreee.jp",
    "lit.link",
    "doctorsfile.jp",
    "dmm-corp.com",
    "metatron-cosme.jp",
    "wellness.parco.jp",
    "osaka-c.ed.jp",
    "www3.osaka-c.ed.jp",
    "clairvoyancecorp.com",
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

OFFICIAL_DOMAIN_HINTS = {
    "クリニックフォア": {"clinicfor.life"},
    "clinic for": {"clinicfor.life"},
    "dmmオンラインクリニック": {"clinic.dmm.com"},
    "dクリニック": {"d-clinicgroup.jp"},
    "dr.agaクリニック": {"drskinclinic.jp"},
    "dr agaクリニック": {"drskinclinic.jp"},
    "イースト駅前クリニック": {"eastcl.com"},
    "agaヘアクリニック": {"agahairclinic.or.jp"},
    "湘南agaクリニック": {"sbc-aga.jp"},
    "湘南美容クリニック": {"s-b-c.net"},
    "ゴリラクリニック": {"gorilla.clinic"},
    "親和クリニック": {"shinwa-clinic.jp"},
    "ウィルagaクリニック": {"will-agaclinic.com"},
    "駅前agaクリニック": {"e-aga.jp"},
    "クレアージュ大阪": {"dclinic-osaka-women.com"},
    "agaメディカルケアクリニック": {"agacare.clinic"},
    "agaメディカルケア": {"agacare.clinic"},
    "スマイルagaクリニック": {"ams-smile.co.jp"},
    "smile aga clinic": {"ams-smile.co.jp"},
    "w clinic": {"mens.wclinic-osaka.jp", "wclinic-osaka.jp"},
    "w clinic men's": {"mens.wclinic-osaka.jp"},
    "w clinic mens": {"mens.wclinic-osaka.jp"},
}

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
    add(re.sub(r"[（(].*?[）)]", "", base))
    stripped = strip_generic_suffixes(name)
    add(stripped)

    if "・" in base:
        add(base.split("・", 1)[0])

    match = BRAND_SUFFIX_PATTERN.match(base)
    if match:
        add(match.group(1))

    return variants


@lru_cache(maxsize=1)
def load_known_official_url_cache() -> dict[str, list[str]]:
    cache: dict[str, list[str]] = {}
    pattern = os.path.join(OUTPUT_ROOT, "*", "*_urls.json")
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        reference_urls = {
            str(raw_url).strip()
            for raw_name, raw_url in payload.items()
            if isinstance(raw_name, str)
            and isinstance(raw_url, str)
            and "参考元記事" in raw_name
            and raw_url.strip()
        }
        for raw_name, raw_url in payload.items():
            if not isinstance(raw_name, str) or not isinstance(raw_url, str):
                continue
            if "参考元記事" in raw_name:
                continue
            url = raw_url.strip()
            if not url or is_banned_domain(url):
                continue
            if url in reference_urls:
                continue
            for variant in extract_lookup_name_variants(raw_name):
                normalized = normalize_text(variant)
                if not normalized:
                    continue
                bucket = cache.setdefault(normalized, [])
                if url not in bucket:
                    bucket.append(url)
    return cache


def find_cached_official_url(name: str) -> str | None:
    cache = load_known_official_url_cache()
    variants = extract_lookup_name_variants(name)
    normalized_variants = [normalize_text(variant) for variant in variants if normalize_text(variant)]

    for normalized_variant in normalized_variants:
        urls = cache.get(normalized_variant) or []
        if urls:
            return urls[0]

    for normalized_variant in normalized_variants:
        for cached_name, urls in cache.items():
            if not urls:
                continue
            if (
                normalized_variant == cached_name
                or normalized_variant in cached_name
                or cached_name in normalized_variant
            ):
                return urls[0]
    return None


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


def hinted_domains_for_name(name: str) -> set[str]:
    normalized_candidates = {
        normalize_text(name),
        normalize_text(normalize_clinic_lookup_name(name)),
        normalize_text(strip_generic_suffixes(name)),
    }
    hinted: set[str] = set()
    for key, domains in OFFICIAL_DOMAIN_HINTS.items():
        normalized_key = normalize_text(key)
        if any(
            normalized_key and (
                normalized_key == candidate
                or normalized_key in candidate
                or candidate in normalized_key
            )
            for candidate in normalized_candidates
            if candidate
        ):
            hinted.update(domains)
    return hinted


def score_official_candidate(name: str, title: str, snippet: str, url: str) -> int:
    if not url or is_banned_domain(url):
        return -999

    combined = " ".join([title or "", snippet or "", url or ""])
    normalized_combined = normalize_text(combined)
    normalized_name = normalize_text(name)
    hostname = get_hostname(url)
    path = urllib.parse.urlparse(url).path.lower()
    score = 0
    hinted_domains = hinted_domains_for_name(name)

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

    if hinted_domains and any(hostname == domain or hostname.endswith(f".{domain}") for domain in hinted_domains):
        score += 12

    if any(keyword in path for keyword in ["/tag/", "/tags/", "/category/", "/categories/", "/goods/", "/events/", "/introduce/", "/lp/", "/reservation", "/reserva/", "/business/", "/salon_search/"]):
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
    cached_url = find_cached_official_url(name)
    if cached_url:
        return cached_url, [{
            "url": cached_url,
            "title": "known official url cache",
            "snippet": "reused from existing project URL map",
            "score": MIN_OFFICIAL_SCORE + 20,
            "query": "cache",
        }]

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
