import os
import re
from pathlib import Path


NANDEMO_SITE_BASENAME = "nandemo.json"
MAX_NANDEMO_VARIANTS = 5
NANDEMO_OUTPUT_KEY_MARKER = "__nandemo_v"
NANDEMO_SHORTCODE_DIR = Path(__file__).parent / "shortcodes" / "nandemo"


def is_nandemo_site(site_config_path: str | None) -> bool:
    if not site_config_path:
        return False
    return os.path.basename(site_config_path) == NANDEMO_SITE_BASENAME


def normalize_variant_count(value: int | str | None) -> int:
    try:
        count = int(value or 1)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(MAX_NANDEMO_VARIANTS, count))


def is_nandemo_output_key(output_key: str | None) -> bool:
    return NANDEMO_OUTPUT_KEY_MARKER in (output_key or "")


def build_nandemo_aga_shortcode_name(variant_index: int) -> str:
    return f"aga-hayamihyou-{normalize_variant_count(variant_index)}"


def build_nandemo_aga_root_class(variant_index: int) -> str:
    return f"ndm-aga-v{normalize_variant_count(variant_index)}"


def get_nandemo_aga_embed_html(variant_index: int) -> str:
    filename = f"{build_nandemo_aga_shortcode_name(variant_index)}.html"
    path = NANDEMO_SHORTCODE_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"なんでも早見表HTMLが見つかりません: {path}")
    return path.read_text(encoding="utf-8").strip()


def extract_shortcode_name(shortcode: str) -> str:
    match = re.search(r'name="([^"]+)"', shortcode or "")
    return match.group(1) if match else ""


def extract_variant_parts_from_shortcode_name(shortcode_name: str) -> tuple[str, int] | None:
    name = shortcode_name or ""
    match = re.fullmatch(r"(aga|ed|houkei)-hayamihyou-(\d+)", name)
    if match:
        return match.group(1), normalize_variant_count(match.group(2))
    match = re.fullmatch(r"(aga|ed|houkei)-hayamihyou", name)
    if match:
        return match.group(1), 1
    return None


def extract_variant_index_from_shortcode_name(shortcode_name: str) -> int | None:
    parts = extract_variant_parts_from_shortcode_name(shortcode_name)
    if parts is None:
        return None
    return parts[1]


def get_variant_embed_marker_from_shortcode_name(shortcode_name: str) -> str:
    parts = extract_variant_parts_from_shortcode_name(shortcode_name)
    if parts is None:
        return ""
    genre_id, variant_index = parts
    return f"ndm-{genre_id}-v{variant_index}"


def resolve_variant_shortcode(
    shortcode: str,
    *,
    genre_id: str,
    output_key: str | None = None,
    variant_index: int = 1,
) -> str:
    if genre_id != "aga" or not is_nandemo_output_key(output_key):
        return shortcode
    variant_name = build_nandemo_aga_shortcode_name(variant_index)
    return re.sub(r'(?<=name=")aga-hayamihyou(?=")', variant_name, shortcode)


def normalize_variant_shortcodes_in_html(
    html: str,
    *,
    genre_id: str,
    output_key: str | None = None,
    variant_index: int = 1,
) -> str:
    if genre_id != "aga" or not is_nandemo_output_key(output_key):
        return html

    variant_name = build_nandemo_aga_shortcode_name(variant_index)
    return re.sub(
        r'\[sc name="aga-hayamihyou(?:-\d+)?"\s*\]\[/sc\]',
        f'[sc name="{variant_name}" ][/sc]',
        html or "",
    )


def resolve_variant_embed_html(
    shortcode: str,
    *,
    genre_id: str,
    output_key: str | None = None,
    variant_index: int = 1,
) -> str:
    resolved_shortcode = resolve_variant_shortcode(
        shortcode,
        genre_id=genre_id,
        output_key=output_key,
        variant_index=variant_index,
    )
    if genre_id != "aga" or not is_nandemo_output_key(output_key):
        return resolved_shortcode
    return get_nandemo_aga_embed_html(variant_index)


def inline_variant_shortcodes_in_html(
    html: str,
    *,
    genre_id: str,
    output_key: str | None = None,
    variant_index: int = 1,
) -> str:
    normalized_html = normalize_variant_shortcodes_in_html(
        html,
        genre_id=genre_id,
        output_key=output_key,
        variant_index=variant_index,
    )
    if not is_nandemo_output_key(output_key):
        # nandemo 以外は従来通り、ショートコードはそのまま残す（テーマ/プラグイン側で展開）
        return normalized_html
    return inline_all_nandemo_shortcodes(
        normalized_html,
        variant_index=variant_index,
    )[0]


# ============================================================
# nandemo 向け汎用ショートコード→ローカルHTMLインライン展開
# ============================================================
# 「なんでも」サイトに投稿する記事は、別ドメイン（テーマも違う可能性あり）に
# 流用される前提なので、`[sc name="X"][/sc]` を一切残さず、対応する
# ローカルHTMLを本文に直接埋め込んで自己完結させる。
#
# ローカルHTMLは `shortcodes/nandemo/<name>.html` に配置する。
# - hayamihyou 系（aga / houkei / ed）は `<name>-<variant>.html` も探索
# - 見つからない場合は元のショートコード文字列を残し、unresolved に追加
def get_nandemo_shortcode_html(
    shortcode_name: str,
    *,
    variant_index: int | None = None,
) -> str | None:
    """`shortcodes/nandemo/<name>.html` のローカルキャッシュを返す。
    hayamihyou 系で variant_index が指定されたら `<name>-<variant>.html` を優先。"""
    if not shortcode_name:
        return None

    candidates: list[str] = []
    if variant_index is not None and shortcode_name.endswith("hayamihyou"):
        idx = normalize_variant_count(variant_index)
        candidates.append(f"{shortcode_name}-{idx}")
    candidates.append(shortcode_name)

    for name in candidates:
        path = NANDEMO_SHORTCODE_DIR / f"{name}.html"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return None


_SHORTCODE_RE = re.compile(r'\[sc name="([^"]+)"\s*\](?:\[/sc\])?')


def inline_all_nandemo_shortcodes(
    html: str,
    *,
    variant_index: int = 1,
) -> tuple[str, list[str]]:
    """HTML 内のすべての `[sc name="..."]...[/sc]` を、ローカルHTMLで置換する。
    戻り値: (置換後HTML, 解決できなかったショートコード名のリスト)"""
    if not html:
        return html or "", []

    unresolved: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        # `aga-hayamihyou`（無印）は variant 適用、それ以外はそのまま
        if name == "aga-hayamihyou":
            embed = get_nandemo_shortcode_html("aga-hayamihyou", variant_index=variant_index)
        elif name.endswith("hayamihyou") and not re.search(r"-\d+$", name):
            # houkei-hayamihyou / ed-hayamihyou など、無印ベース名にも variant を適用
            embed = get_nandemo_shortcode_html(name, variant_index=variant_index)
        else:
            embed = get_nandemo_shortcode_html(name)

        if embed is None:
            unresolved.append(name)
            return match.group(0)
        return embed

    return _SHORTCODE_RE.sub(_replace, html), unresolved


def find_unresolved_shortcodes(html: str) -> list[str]:
    """HTML内に残っている `[sc name="..."]` を列挙。検証用。"""
    return [m.group(1) for m in _SHORTCODE_RE.finditer(html or "")]


def build_variant_output_key(keyword: str, variant_index: int) -> str:
    return f"{keyword}__nandemo_v{variant_index}"


def build_variant_profile(variant_index: int, variant_count: int) -> dict[str, object]:
    count = normalize_variant_count(variant_count)
    index = max(1, min(count, int(variant_index or 1)))
    rotation = max(0, index - 1)

    lead_styles = [
        "冒頭では結論を短く示したあとに、比較・判断材料を補足する。",
        "冒頭では悩みへの共感を1段落入れてから、結論と比較ポイントへ入る。",
        "冒頭では比較の判断軸を先に示してから、結論へつなぐ。",
        "冒頭では結論を出したあとに、選ぶ際の注意点をすぐ添える。",
        "冒頭では要点を箇条書き寄りに整理してから本文へ入る。",
    ]
    sentence_styles = [
        "1文をやや短めにし、断定→補足の順でテンポよく書く。",
        "接続詞を少し柔らかくし、理由→結論の順で自然につなぐ。",
        "結論文のあとに具体例を置く頻度を増やし、説明を後ろに展開する。",
        "同じ意味の言い回しを避け、比較表の説明は簡潔に、本文で補足を厚めにする。",
        "語尾や接続表現を散らし、段落の長さに軽いメリハリを付ける。",
    ]
    cta_styles = [
        "CTA直前は『比較表で候補を絞る』流れで自然につなぐ。",
        "CTA直前は『自分に合う条件を確認する』流れで自然につなぐ。",
        "CTA直前は『料金・続けやすさを見比べる』流れで自然につなぐ。",
        "CTA直前は『候補を2〜3件に絞る』流れで自然につなぐ。",
        "CTA直前は『公式情報を確認しながら比較する』流れで自然につなぐ。",
    ]
    image_styles = [
        "画像は基準版として最もオーソドックスな構図にする。",
        "画像は同じテーマでも、人物配置や視線方向を少し変える。",
        "画像は見出しコピーの改行位置と主役モチーフの置き方を変える。",
        "画像は背景モチーフを簡略化し、余白の取り方を変える。",
        "画像は配色バランスと図解モチーフの見せ方を少し変える。",
    ]

    return {
        "variant_index": index,
        "variant_count": count,
        "rotation": rotation,
        "lead_style": lead_styles[(index - 1) % len(lead_styles)],
        "sentence_style": sentence_styles[(index - 1) % len(sentence_styles)],
        "cta_style": cta_styles[(index - 1) % len(cta_styles)],
        "image_style": image_styles[(index - 1) % len(image_styles)],
    }


def build_editorial_variant_instruction(variant_index: int, variant_count: int) -> str:
    profile = build_variant_profile(variant_index, variant_count)
    if profile["variant_count"] <= 1:
        return "通常版として、検索意図に最も素直な構成と文章で作成する。"

    return (
        "この記事は同一キーワードで作る別編集版の1本です。"
        "ただし記事戦略・検索意図・結論・比較軸は変えないでください。"
        "記事の切り口を変えるのではなく、"
        f"導入の入り方は『{profile['lead_style']}』、"
        f"文章運びは『{profile['sentence_style']}』、"
        "H2/H3の順序は不自然にならない範囲で少し入れ替え、"
        f"比較対象やFAQの並び順は {profile['rotation']} 件ぶん回転させる意識で調整してください。"
        "同じ内容でも、段落の順番・比較表の補足位置・接続表現が別記事として見えるようにしてください。"
        f"CTAのつなぎ方は『{profile['cta_style']}』を意識し、"
        f"画像の見せ方は『{profile['image_style']}』を意識してください。"
    )


def rotate_list(values: list, offset: int) -> list:
    if not values:
        return values
    step = offset % len(values)
    if step == 0:
        return list(values)
    return list(values[step:] + values[:step])


def _extract_h2_heading(section_text: str) -> str:
    match = re.search(r"^### \[H2\] (.+)$", section_text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _split_h3_blocks(section_text: str) -> tuple[str, list[str]]:
    matches = list(re.finditer(r"^#### \[H3\] .+$", section_text, re.MULTILINE))
    if not matches:
        return section_text, []

    prefix = section_text[:matches[0].start()]
    blocks = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(section_text)
        blocks.append(section_text[start:end].rstrip())
    return prefix.rstrip() + "\n\n", blocks


def _should_rotate_h3_blocks(h2_heading: str, h3_blocks: list[str]) -> bool:
    if len(h3_blocks) < 3:
        return False
    heading = h2_heading or ""
    lowered = heading.lower()
    if "よくある質問" in heading or "faq" in lowered:
        return True
    if any(token in heading for token in ("比較", "おすすめ", "クリニック", "一覧", "選び方")):
        return True
    return False


def _rotate_h3_blocks(section_text: str, rotation: int) -> str:
    h2_heading = _extract_h2_heading(section_text)
    prefix, h3_blocks = _split_h3_blocks(section_text)
    if not _should_rotate_h3_blocks(h2_heading, h3_blocks):
        return section_text
    rotated = rotate_list(h3_blocks, rotation)
    return prefix + "\n\n".join(rotated).strip() + "\n"


def apply_editorial_variant_to_tag_structure(
    tag_structure: str,
    variant_index: int,
    variant_count: int,
) -> str:
    profile = build_variant_profile(variant_index, variant_count)
    rotation = int(profile["rotation"])
    if rotation <= 0:
        return tag_structure

    marker = re.search(r"^## タグ構成\s*$", tag_structure, re.MULTILINE)
    if not marker:
        return tag_structure

    prefix = tag_structure[:marker.end()].rstrip() + "\n\n"
    body = tag_structure[marker.end():].lstrip("\n")
    matches = list(re.finditer(r"^### \[H2\] .+$", body, re.MULTILINE))
    if not matches:
        return tag_structure

    sections = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        section_text = body[start:end].strip() + "\n"
        section_text = _rotate_h3_blocks(section_text, rotation)
        sections.append(section_text)

    if len(sections) <= 2:
        return prefix + "\n".join(sections).strip() + "\n"

    first = sections[:1]
    tail = []
    middle = sections[1:]
    while middle:
        heading = _extract_h2_heading(middle[-1])
        if "まとめ" in heading or "よくある質問" in heading or "FAQ" in heading.upper():
            tail.insert(0, middle.pop())
            continue
        break

    reordered_middle = rotate_list(middle, rotation)
    reordered_sections = first + reordered_middle + tail
    return prefix + "\n".join(section.strip() for section in reordered_sections).strip() + "\n"
