import hashlib
from typing import Any
from urllib.parse import urlparse


SURVEY_REQUIRED_TOKENS = (
    "おすすめ",
    "比較",
    "ランキング",
    "クリニック",
    "病院",
)

SURVEY_OPTIONAL_TOKENS = (
    "選び方",
    "選ぶ",
    "料金",
    "費用",
    "相場",
    "値段",
    "いくら",
    "安い",
    "オンライン",
    "オンライン診療",
    "通販",
)

SURVEY_OFF_TOKENS = (
    "副作用",
    "効果",
    "原因",
    "仕組み",
    "やめると",
    "いつから",
    "飲み方",
    "飲むタイミング",
    "使い方",
    "注意点",
    "症状",
    "初期症状",
    "体験談",
    "口コミだけ",
    "デメリット",
    "メリット",
    "とは",
)

LOCATION_TOKENS = (
    "北海道", "青森", "岩手", "宮城", "秋田", "山形", "福島",
    "茨城", "栃木", "群馬", "埼玉", "千葉", "東京", "神奈川",
    "新潟", "富山", "石川", "福井", "山梨", "長野",
    "岐阜", "静岡", "愛知", "三重",
    "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
    "鳥取", "島根", "岡山", "広島", "山口",
    "徳島", "香川", "愛媛", "高知",
    "福岡", "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島", "沖縄",
    "新宿", "渋谷", "池袋", "横浜", "名古屋", "栄", "梅田", "難波", "なんば",
    "天王寺", "心斎橋", "札幌", "仙台", "大宮", "川崎", "神戸",
    "博多", "天神", "金沢", "那覇",
)

NANDEMO_SURVEY_PATTERNS = [
    {
        "angle_name": "料金比較先行型",
        "question_style": "最初に見たい料金情報や比較表の見やすさを主軸にする",
        "chart_style": "順位バーを主役にし、補助指標は少なめの構成にする",
        "wording_style": "費用・継続・比較しやすさを中心に、実務的で端的な言い回しにする",
    },
    {
        "angle_name": "通いやすさ先行型",
        "question_style": "アクセス・診療時間・オンライン対応など継続しやすさを主軸にする",
        "chart_style": "継続条件の割合を目立たせ、比較項目の順位は簡潔に見せる",
        "wording_style": "通院導線や続けやすさを中心に、やや生活者目線の言い回しにする",
    },
    {
        "angle_name": "比較表改善型",
        "question_style": "読者が比較表に欲しい情報や表示の分かりやすさを主軸にする",
        "chart_style": "比較表改善ニーズのカードを主役にし、他指標は補助的に扱う",
        "wording_style": "読みやすさ、比較しやすさ、判断のしやすさを中心に構成する",
    },
    {
        "angle_name": "オンライン併用型",
        "question_style": "来院院とオンライン院をどう併用比較したいかを主軸にする",
        "chart_style": "オンライン需要の指標を必ず1つ大きめに見せる",
        "wording_style": "通院だけでなく、オンラインも比較したい読者像を前提にした表現にする",
    },
    {
        "angle_name": "不安解消型",
        "question_style": "費用の不透明さ、選び方の迷い、継続不安などの解消を主軸にする",
        "chart_style": "要約セクションを強めにし、数値の意味がすぐ伝わる見せ方にする",
        "wording_style": "判断材料を整理して不安を減らす方向のコピーにする",
    },
]

NANDEMO_VARIANT_DESIGN_THEMES = {
    1: {
        "theme_name": "nandemo-v1-balanced-editorial",
        "direction": "最も標準的でバランスの良い比較図表。情報整理を主役にする",
        "rules": [
            "見出し、棒グラフ、要約の3要素を素直に整理する",
            "配色は中立寄りで、本文に最も馴染みやすいトーンにする",
            "初見でも読みやすい標準版として扱う",
            "ボックスやバッジの角丸は最小限にし、直線的な紙面デザインに寄せる",
        ],
    },
    2: {
        "theme_name": "nandemo-v2-data-card-emphasis",
        "direction": "数値や比較差分が目に入りやすいカード寄りの図表",
        "rules": [
            "割合や順位の視認性を少し強める",
            "カードや区切りを使って情報の差分を把握しやすくする",
            "標準版より少し比較・ランキング感を強める",
            "丸いボタン風・LP風の装飾は避け、角の立った整理型デザインにする",
        ],
    },
    3: {
        "theme_name": "nandemo-v3-clean-report",
        "direction": "レポート風で整った、やや調査資料寄りの図表",
        "rules": [
            "罫線や余白で整理し、装飾より構造を優先する",
            "数字とラベルの対応が一目でわかる見せ方にする",
            "新聞・調査レポートに近い印象を意識する",
            "角丸はほぼ使わず、表やレポート資料のような直線主体にする",
        ],
    },
    4: {
        "theme_name": "nandemo-v4-soft-guide",
        "direction": "やややわらかい印象で、読者ガイドとして見やすい図表",
        "rules": [
            "比較の圧を出しすぎず、案内・ガイド感を持たせる",
            "柔らかい配色と軽い強弱で読み疲れしにくくする",
            "選び方記事や初心者向け記事にも馴染む見せ方にする",
            "やわらかい配色でも、囲みやラベルの角は丸くしすぎない",
        ],
    },
    5: {
        "theme_name": "nandemo-v5-compact-mobile-first",
        "direction": "縦に伸びにくい、スマホ閲覧を強く意識したコンパクト図表",
        "rules": [
            "情報を詰めすぎず、スマホでの短いスクロールで読めるようにする",
            "項目名・割合・比較軸を近く配置し、判断の速さを優先する",
            "5版の中では最もコンパクトな見せ方にする",
            "モバイルでも角丸を強くせず、四角寄りで情報密度を保つ",
        ],
    },
}

SITE_DOMAIN_DESIGN_THEMES = {
    "aurora-clinic.jp": {
        "theme_name": "aurora-editorial-soft",
        "direction": "やわらかいベージュ基調で、比較記事に馴染む上品な調査図表",
        "rules": [
            "装飾は控えめで、医療メディアらしい落ち着いたトーンにする",
            "角丸や影は強くしすぎず、本文に自然に馴染ませる",
            "強調色は暖色寄りで統一し、派手なネオン感は避ける",
            "カードや囲みは直線寄りにし、AIっぽい丸い見た目を避ける",
        ],
    },
    "ashitano.clinic": {
        "theme_name": "ashitano-clean-light",
        "direction": "明るく清潔感のある比較図表。青系の信頼感を主軸にする",
        "rules": [
            "白地をベースに、淡いブルーやグレーで情報を整理する",
            "医療情報の可読性を優先し、過剰な装飾を避ける",
            "順位・割合・要点がすぐ読める整理型デザインにする",
            "囲みやラベルの角丸は最小限にし、清潔感のある直線主体で整える",
        ],
    },
    "mame-clinic.net": {
        "theme_name": "mame-compact-practical",
        "direction": "情報優先でコンパクト、やや実務的な比較図表",
        "rules": [
            "余白を使いすぎず、スマホでも縦に伸びすぎないようにする",
            "数値と見出しを近く配置して、判断材料がすぐ伝わるようにする",
            "やわらかさは保ちつつも、装飾より情報整理を優先する",
            "ボックスの角を丸めすぎず、実務的な比較表現に寄せる",
        ],
    },
    "utu-yobo.com": {
        "theme_name": "utu-yobo-gentle-neutral",
        "direction": "刺激の少ないニュートラル配色で、安心感を重視した図表",
        "rules": [
            "強い赤や強い煽り表現を避け、穏やかなコントラストにする",
            "本文に自然に馴染むカード/罫線ベースの見せ方を優先する",
            "やさしい印象を保ちつつ、情報の階層は明確にする",
            "印象はやさしくても、形状は四角寄りで落ち着かせる",
        ],
    },
}

GENRE_SURVEY_BLUEPRINTS = {
    "aga": {
        "genre_label": "AGA治療",
        "comparison_axes": [
            "月額費用のわかりやすさ",
            "予防プランの料金",
            "発毛プランの料金帯",
            "オンライン診療への対応有無",
            "診察料・再診料の有無",
            "治療の始めやすさ",
            "プライバシー配慮",
        ],
        "regional_axes": [
            "駅からの通いやすさ",
            "仕事帰りに通える診療時間",
            "オンライン診療への対応有無",
            "月額費用のわかりやすさ",
            "完全個室などのプライバシー配慮",
            "初診予約の取りやすさ",
        ],
        "online_axes": [
            "オンラインだけで完結できるか",
            "薬の配送スピード",
            "診察料・再診料の有無",
            "月額費用のわかりやすさ",
            "診療時間の広さ",
            "予防/発毛プランの分かりやすさ",
        ],
        "cost_axes": [
            "月額費用のわかりやすさ",
            "予防プランの料金",
            "発毛プランの料金帯",
            "診察料・再診料の有無",
            "追加費用の少なさ",
            "返金保証や割引の有無",
        ],
        "selection_axes": [
            "料金プランのわかりやすさ",
            "通いやすさ・続けやすさ",
            "オンライン対応",
            "治療内容の説明の丁寧さ",
            "予防/発毛の比較しやすさ",
            "プライバシー配慮",
        ],
        "metric_candidates": [
            "通いやすさが継続判断に影響すると感じる割合",
            "比較表に「予防」と「発毛」を分けて表示してほしいと感じる割合",
            "オンライン診療も同じ記事内で比較したいと感じる割合",
            "料金の見方が明確な記事ほど候補を絞りやすいと感じる割合",
            "駅近・アクセス情報があると検討しやすいと感じる割合",
        ],
        "summary_angles": [
            "費用の見方補足があると比較しやすい",
            "アクセス情報は大阪・都市部記事で特に効く",
            "予防と発毛は分けて見せたほうが伝わる",
        ],
    },
    "ed": {
        "genre_label": "ED治療",
        "comparison_axes": [
            "料金のわかりやすさ",
            "オンライン完結のしやすさ",
            "診察料・送料の明確さ",
            "即日発送や受け取りやすさ",
            "プライバシー配慮",
            "取扱薬の選びやすさ",
            "予約のしやすさ",
        ],
        "regional_axes": [
            "駅からの通いやすさ",
            "オンライン診療への対応",
            "待ち時間の少なさ",
            "プライバシー配慮",
            "料金のわかりやすさ",
            "夜間・休日の診療",
        ],
        "online_axes": [
            "オンラインだけで完結できるか",
            "発送スピード",
            "診察料・送料の明確さ",
            "プライバシー配慮",
            "予約可能時間の広さ",
            "薬の選びやすさ",
        ],
        "cost_axes": [
            "1回あたりの費用",
            "診察料・送料の有無",
            "まとめ買い時の安さ",
            "追加費用の少なさ",
            "取扱薬の選びやすさ",
            "オンライン完結のしやすさ",
        ],
        "selection_axes": [
            "料金のわかりやすさ",
            "オンライン完結のしやすさ",
            "プライバシー配慮",
            "取扱薬の選びやすさ",
            "発送スピード",
            "診察時間の柔軟さ",
        ],
        "metric_candidates": [
            "オンライン完結できると受診ハードルが下がると感じる割合",
            "プライバシー配慮が比較時の安心感につながると感じる割合",
            "発送スピードが継続利用の判断に影響すると感じる割合",
            "診察料と送料が明確だと候補を絞りやすいと感じる割合",
            "通院院とオンライン院を同じ記事で比較したいと感じる割合",
        ],
        "summary_angles": [
            "EDは料金だけでなく匿名性・完結性も重要",
            "オンライン比較需要が強い",
            "送料や発送条件の明確化が判断材料になる",
        ],
    },
    "diet": {
        "genre_label": "医療ダイエット",
        "comparison_axes": [
            "月額費用のわかりやすさ",
            "薬や治療法の違いのわかりやすさ",
            "副作用説明の丁寧さ",
            "オンライン診療の有無",
            "継続サポートの内容",
            "通院頻度の少なさ",
            "血液検査などの安全面",
        ],
        "regional_axes": [
            "駅からの通いやすさ",
            "通院頻度の少なさ",
            "オンライン診療の有無",
            "月額費用のわかりやすさ",
            "診療時間の柔軟さ",
            "継続サポートの内容",
        ],
        "online_axes": [
            "オンラインだけで進めやすいか",
            "薬の選択肢のわかりやすさ",
            "副作用説明の丁寧さ",
            "月額費用のわかりやすさ",
            "相談しやすさ",
            "配送や受け取りのしやすさ",
        ],
        "cost_axes": [
            "月額費用のわかりやすさ",
            "初期費用の少なさ",
            "薬代以外の追加費用",
            "継続しやすい料金設計",
            "サポート内容とのバランス",
            "オンライン対応",
        ],
        "selection_axes": [
            "費用の見通し",
            "薬や施術の違いのわかりやすさ",
            "副作用説明の丁寧さ",
            "継続サポートの内容",
            "通院負担の少なさ",
            "オンライン相談のしやすさ",
        ],
        "metric_candidates": [
            "費用よりも継続しやすさを重視すると感じる割合",
            "副作用説明が丁寧だと候補を絞りやすいと感じる割合",
            "オンライン相談があると始めやすいと感じる割合",
            "薬の違いが整理されている記事ほど読みやすいと感じる割合",
            "通院頻度が少ないことが継続判断に影響すると感じる割合",
        ],
        "summary_angles": [
            "医療ダイエットは費用だけでなく継続負担も重要",
            "薬の違いと副作用説明の整理が必要",
            "オンライン相談の有無が比較軸になりやすい",
        ],
    },
    "phimosis": {
        "genre_label": "包茎治療",
        "comparison_axes": [
            "総額費用のわかりやすさ",
            "傷跡の目立ちにくさ",
            "術式の違いのわかりやすさ",
            "アフターケアの内容",
            "プライバシー配慮",
            "通院回数の少なさ",
            "追加費用の少なさ",
        ],
        "regional_axes": [
            "駅からの通いやすさ",
            "プライバシー配慮",
            "通院回数の少なさ",
            "総額費用のわかりやすさ",
            "診療時間の柔軟さ",
            "アフターケアの内容",
        ],
        "online_axes": [
            "事前相談をオンラインで進めやすいか",
            "総額費用のわかりやすさ",
            "術式説明の丁寧さ",
            "プライバシー配慮",
            "来院回数の少なさ",
            "アフターケアの内容",
        ],
        "cost_axes": [
            "総額費用のわかりやすさ",
            "麻酔代や薬代など追加費用の少なさ",
            "術式ごとの料金差のわかりやすさ",
            "保証や再診費用の明確さ",
            "アフターケアとのバランス",
            "通院回数の少なさ",
        ],
        "selection_axes": [
            "総額費用のわかりやすさ",
            "傷跡の目立ちにくさ",
            "術式説明の丁寧さ",
            "アフターケアの内容",
            "プライバシー配慮",
            "通院回数の少なさ",
        ],
        "metric_candidates": [
            "総額が明確だと比較しやすいと感じる割合",
            "傷跡の目立ちにくさがクリニック選びに影響すると感じる割合",
            "プライバシー配慮が安心感につながると感じる割合",
            "術式の違いが整理されている記事ほど読みやすいと感じる割合",
            "アフターケアの充実度が検討に影響すると感じる割合",
        ],
        "summary_angles": [
            "包茎治療は総額と傷跡の両方が比較軸になりやすい",
            "術式の違いを整理すると読みやすい",
            "プライバシーとアフターケアが安心材料になる",
        ],
    },
    "hair_removal": {
        "genre_label": "医療脱毛",
        "comparison_axes": [
            "総額費用のわかりやすさ",
            "予約の取りやすさ",
            "痛みへの配慮",
            "照射機器の違いのわかりやすさ",
            "通いやすさ",
            "追加費用の少なさ",
            "施術範囲のわかりやすさ",
        ],
        "regional_axes": [
            "駅からの通いやすさ",
            "予約の取りやすさ",
            "診療時間の柔軟さ",
            "総額費用のわかりやすさ",
            "痛みへの配慮",
            "施術範囲のわかりやすさ",
        ],
        "online_axes": [
            "カウンセリング予約のしやすさ",
            "料金のわかりやすさ",
            "プランの比較しやすさ",
            "追加費用の少なさ",
            "通院回数の見通し",
            "痛みへの配慮の説明",
        ],
        "cost_axes": [
            "総額費用のわかりやすさ",
            "追加費用の少なさ",
            "部位ごとの料金の見やすさ",
            "キャンセル料などの明確さ",
            "通いやすさとのバランス",
            "予約の取りやすさ",
        ],
        "selection_axes": [
            "総額費用のわかりやすさ",
            "予約の取りやすさ",
            "痛みへの配慮",
            "機器の違いのわかりやすさ",
            "通いやすさ",
            "追加費用の少なさ",
        ],
        "metric_candidates": [
            "予約の取りやすさが継続判断に影響すると感じる割合",
            "総額が明確だと比較しやすいと感じる割合",
            "痛みへの配慮がクリニック選びに影響すると感じる割合",
            "機器の違いが整理されている記事ほど読みやすいと感じる割合",
            "通いやすさが継続判断につながると感じる割合",
        ],
        "summary_angles": [
            "医療脱毛は総額だけでなく予約の取りやすさも重要",
            "機器差の整理が比較表の読みやすさにつながる",
            "通いやすさと痛み配慮が継続意欲に影響する",
        ],
    },
}

SURVEY_DATA_RULES = [
    "順位バーの設問は5〜6項目で作り、割合は必ず合計100%にする",
    "1位の割合は極端に高くしすぎず、23〜34%程度を目安にする",
    "2位以下は段階的に下がる自然な分布にし、意味なく等間隔にしない",
    "補助カードの割合は68〜91%程度の高めの数値で、比較軸の納得感を示す",
    "100%、99%、50%など不自然に記号的な数値を多用しない",
    "31%、84%、27%、76%のように端数を含む現実味のある数値を混ぜる",
    "同じ記事内で全数値が5刻み・10刻みにならないようにする",
    "競合記事で実際によく扱われる比較軸を優先し、記事本文と無関係な設問を作らない",
    "実施済みアンケートに見せず、あくまで編集部作成の仮データ図表として明記する",
]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def build_survey_content_blueprint(keyword: str, genre_id: str, article_type: str) -> dict[str, Any]:
    blueprint = GENRE_SURVEY_BLUEPRINTS.get(genre_id) or GENRE_SURVEY_BLUEPRINTS["aga"]
    if article_type == "regional_comparison":
        ranking_axes = blueprint.get("regional_axes", [])
    elif article_type == "online_comparison":
        ranking_axes = blueprint.get("online_axes", [])
    elif article_type == "cost_comparison":
        ranking_axes = blueprint.get("cost_axes", [])
    elif article_type == "selection_guide":
        ranking_axes = blueprint.get("selection_axes", [])
    else:
        ranking_axes = blueprint.get("comparison_axes", [])

    ranking_axes = _dedupe_preserve_order(
        list(ranking_axes) + list(blueprint.get("comparison_axes", []))
    )[:6]

    metric_candidates = list(blueprint.get("metric_candidates", []))
    if article_type == "regional_comparison":
        metric_priority = [item for item in metric_candidates if "通いやすさ" in item or "駅" in item]
    elif article_type == "online_comparison":
        metric_priority = [item for item in metric_candidates if "オンライン" in item or "発送" in item]
    elif article_type == "cost_comparison":
        metric_priority = [item for item in metric_candidates if "料金" in item or "総額" in item or "費用" in item]
    elif article_type == "selection_guide":
        metric_priority = [item for item in metric_candidates if "比較" in item or "読みやすい" in item or "候補" in item]
    else:
        metric_priority = metric_candidates[:]

    metric_focus = _dedupe_preserve_order(metric_priority + metric_candidates)[:3]

    return {
        "genre_label": blueprint.get("genre_label", genre_id),
        "ranking_question": "比較するとき最初に確認したい項目",
        "ranking_axes": ranking_axes,
        "metric_focus": metric_focus,
        "summary_angles": list(blueprint.get("summary_angles", [])),
        "data_rules": SURVEY_DATA_RULES,
        "keyword_context_rules": [
            "地域名キーワードなら、通いやすさ・駅からの導線・診療時間の少なくとも1つを入れる",
            "オンライン系キーワードなら、オンライン完結性・配送/受け取り・予約時間の少なくとも1つを入れる",
            "料金系キーワードなら、料金のわかりやすさや追加費用を上位候補に入れる",
            "選び方系キーワードなら、比較表の見やすさ・説明の丁寧さ・継続しやすさのいずれかを入れる",
        ],
        "article_match_rule": (
            f"キーワード「{keyword}」の記事本文で実際に比較・解説する論点に沿って設問を組み立て、"
            "図表だけが浮かないようにする"
        ),
    }


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def keyword_has_location(keyword: str) -> bool:
    return _contains_any(keyword or "", LOCATION_TOKENS)


def extract_site_domain(site_url: str | None) -> str:
    if not site_url:
        return ""
    try:
        parsed = urlparse(site_url)
    except Exception:
        return ""
    return (parsed.netloc or "").lower()


def infer_survey_article_type(keyword: str) -> str:
    keyword = keyword or ""
    has_required = _contains_any(keyword, SURVEY_REQUIRED_TOKENS)
    has_optional = _contains_any(keyword, SURVEY_OPTIONAL_TOKENS)
    has_off = _contains_any(keyword, SURVEY_OFF_TOKENS)
    has_location = keyword_has_location(keyword)

    if has_off and not (has_required or has_optional or has_location):
        return "knowledge"
    if has_required:
        return "comparison"
    if has_location and not has_off:
        return "regional_comparison"
    if "オンライン" in keyword and not has_off:
        return "online_comparison"
    if any(token in keyword for token in ("料金", "費用", "相場", "値段", "いくら", "安い")) and not has_off:
        return "cost_comparison"
    if any(token in keyword for token in ("選び方", "選ぶ")) and not has_off:
        return "selection_guide"
    return "knowledge" if has_off else "general"


def build_survey_policy(
    keyword: str,
    genre_id: str,
    *,
    is_nandemo: bool = False,
    site_url: str | None = None,
    variant_index: int = 1,
    variant_count: int = 1,
) -> dict[str, Any]:
    article_type = infer_survey_article_type(keyword)
    if article_type in {"comparison", "regional_comparison", "online_comparison"}:
        mode = "required"
    elif article_type in {"cost_comparison", "selection_guide"}:
        mode = "optional"
    else:
        mode = "off"

    site_domain = extract_site_domain(site_url)
    base_policy: dict[str, Any] = {
        "keyword": keyword,
        "genre_id": genre_id,
        "article_type_hint": article_type,
        "mode": mode,
        "initial_check_step": "search",
        "final_decision_step": "tag_structure",
        "insert_position": "導入文と比較表のあと、各院詳細に入る前",
        "purpose": "読者の比較軸を可視化し、比較表のあとで判断材料を整理する",
        "allowed_for": [
            "比較記事",
            "おすすめ記事",
            "地域比較記事",
            "選び方記事",
            "料金比較記事",
            "オンライン比較記事",
        ],
        "disallowed_for": [
            "副作用解説記事",
            "効果解説記事",
            "原因解説記事",
            "医学知識中心の記事",
            "FAQ中心の記事",
        ],
        "site_mode": "nandemo" if is_nandemo else "standard",
        "site_domain": site_domain,
    }

    if not is_nandemo:
        design_theme = SITE_DOMAIN_DESIGN_THEMES.get(site_domain) or {
            "theme_name": "standard-editorial-neutral",
            "direction": "中立的で記事本文に馴染む、装飾控えめの調査図表",
            "rules": [
                "サイト全体のデザインを壊さないように、本文に馴染む配色を使う",
                "同じドメインでは毎回同じデザインテーマを使い、見た目の一貫性を保つ",
                "情報整理を優先し、可読性を落とす過剰装飾は避ける",
            ],
        }
        base_policy["design_theme"] = design_theme
        return base_policy

    seed = f"{keyword}|{genre_id}|{variant_index}|{variant_count}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    pattern = NANDEMO_SURVEY_PATTERNS[int(digest[:8], 16) % len(NANDEMO_SURVEY_PATTERNS)]
    variant_theme_index = max(1, min(5, int(variant_index or 1)))
    base_policy["nandemo_uniqueness"] = {
        "angle_name": pattern["angle_name"],
        "question_style": pattern["question_style"],
        "chart_style": pattern["chart_style"],
        "wording_style": pattern["wording_style"],
        "rules": [
            "他記事と同じ設問順・同じ設問文・同じ要約見出しを避ける",
            "主役にする比較軸を1つ決め、他の記事と重心をずらす",
            "数値の見せ方は同じでも、ラベルや補足文の切り口を変える",
            "同一キーワードの別編集版だけでなく、他ジャンルの比較記事とも似すぎない文面にする",
        ],
    }
    base_policy["design_theme"] = NANDEMO_VARIANT_DESIGN_THEMES[variant_theme_index]
    return base_policy


def build_survey_prompt_instruction(policy: dict[str, Any]) -> str:
    mode = policy.get("mode", "off")
    article_type = policy.get("article_type_hint", "general")
    final_step = policy.get("final_decision_step", "tag_structure")
    position = policy.get("insert_position", "")
    keyword = policy.get("keyword", "")
    genre_id = policy.get("genre_id", "")
    content_blueprint = build_survey_content_blueprint(keyword, genre_id, article_type)

    lines = [
        f"アンケート図表方針: {mode}",
        f"事前判定: Step `{policy.get('initial_check_step', 'search')}` ではキーワードから仮判定し、最終判定は Step `{final_step}` の記事タイプ確定時に行う。",
    ]

    if mode == "required":
        lines.append(
            f"記事タイプ仮説 `{article_type}` はアンケート図表と相性が高いため、タグ構成では原則としてアンケート図表を採用し、挿入位置は `{position}` とする。"
        )
    elif mode == "optional":
        lines.append(
            f"記事タイプ仮説 `{article_type}` はアンケート図表を任意採用できる。比較軸整理に寄与する場合のみ採用し、採用時の挿入位置は `{position}` とする。"
        )
    else:
        lines.append(
            f"記事タイプ仮説 `{article_type}` はアンケート図表と相性が弱いため、原則として採用しない。"
        )

    if policy.get("site_mode") == "nandemo":
        uniqueness = policy.get("nandemo_uniqueness", {})
        lines.append(
            "なんでも向け差別化ルール: 他記事と同じ作者に見えないよう、設問の重心・ラベル・補足文・図表の主役を毎回ずらす。"
        )
        if uniqueness:
            lines.append(f"- 主役テーマ: {uniqueness.get('angle_name', '')}")
            lines.append(f"- 設問設計: {uniqueness.get('question_style', '')}")
            lines.append(f"- 図表構成: {uniqueness.get('chart_style', '')}")
            lines.append(f"- 文体方針: {uniqueness.get('wording_style', '')}")
            for rule in uniqueness.get("rules", []):
                lines.append(f"- {rule}")

    design_theme = policy.get("design_theme", {})
    if design_theme:
        lines.append("アンケート図表デザイン方針:")
        lines.append(f"- デザインテーマ: {design_theme.get('theme_name', '')}")
        lines.append(f"- 方向性: {design_theme.get('direction', '')}")
        for rule in design_theme.get("rules", []):
            lines.append(f"- {rule}")
        lines.append("- ボタン風の大きな丸み、過剰なピル型ラベル、角の強い丸みは避ける")

    if mode in {"required", "optional"}:
        lines.append("アンケート内容設計方針:")
        lines.append(f"- ジャンル前提: {content_blueprint.get('genre_label', genre_id)}")
        lines.append(f"- Q1の主題: {content_blueprint.get('ranking_question', '')}")
        ranking_axes = content_blueprint.get("ranking_axes", [])
        if ranking_axes:
            lines.append(f"- Q1候補項目: {', '.join(ranking_axes)}")
        metric_focus = content_blueprint.get("metric_focus", [])
        if metric_focus:
            lines.append(f"- Q2〜Q4候補テーマ: {', '.join(metric_focus)}")
        summary_angles = content_blueprint.get("summary_angles", [])
        if summary_angles:
            lines.append(f"- 要約で拾う観点: {', '.join(summary_angles)}")
        lines.append(f"- 記事整合ルール: {content_blueprint.get('article_match_rule', '')}")
        for rule in content_blueprint.get("keyword_context_rules", []):
            lines.append(f"- {rule}")
        lines.append("アンケート数値の信憑性ルール:")
        for rule in content_blueprint.get("data_rules", []):
            lines.append(f"- {rule}")
        lines.append(
            "- 数値はあくまで編集部作成の仮データとして作るが、競合記事で実際によく見られる比較軸や検索意図に沿わせ、"
            "読者が『たしかにその観点を気にする』と感じる内容にする"
        )
        lines.append(
            "- アンケート図表のタイトル・設問・補足文・要約文は、本文で後から解説する論点と接続させる"
        )

    return "\n".join(lines)
