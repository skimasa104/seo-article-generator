#!/usr/local/bin/python3.12
"""
SEO記事自動生成スクリプト (Step 2-3)
Claude APIを使い、競合記事データから記事HTMLを自動生成する。

  Step 2: タグ構成設計（競合分析→H2/H3構成を動的に決定）
  Step 3: 本文HTML生成（タグ構成に従ってHTML出力）

使い方:
  python generate_article.py --keyword "AGA 横浜" --genre aga
  python generate_article.py --keyword "AGA 横浜" --genre aga --step 2   # Step 2のみ
  python generate_article.py --keyword "AGA 横浜" --genre aga --step 3   # Step 3のみ

環境変数:
  ANTHROPIC_API_KEY: Claude API キー
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

from article_audit import validate_article_html
from bs4 import BeautifulSoup
from env_utils import load_project_env
from output_utils import ensure_common_assets, ensure_keyword_output_dir, keyword_to_slug
from output_utils import get_keyword_scraped_dir

# ========================================
# 設定
# ========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENRES_DIR = os.path.join(SCRIPT_DIR, "genres")

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS_STEP2 = 8192
MAX_TOKENS_STEP3 = 16384
MAX_SECTION_REPAIR_ATTEMPTS = 2
CLAUDE_RETRY_DELAYS = [30, 90, 180, 300]

load_project_env()


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def cleanup_generated_html(content: str, keyword_slug: str) -> str:
    """後続ステップで差し込む画像プレースホルダーを除去する。"""
    patterns = [
        rf'\s*<img\s+[^>]*src="images/{re.escape(keyword_slug)}_top\.(?:png|jpg|jpeg|webp)"[^>]*>\s*',
        rf'\s*<img\s+[^>]*src="images/{re.escape(keyword_slug)}_h2_\d+\.(?:png|jpg|jpeg|webp)"[^>]*>\s*',
    ]

    cleaned = content
    for pattern in patterns:
        cleaned = re.sub(pattern, "\n", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_code_fences(content: str) -> str:
    content = content.strip()
    if content.startswith("```html"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = re.sub(r"</?(?:html|body)\b[^>]*>", "", content, flags=re.IGNORECASE)
    return content.strip()


def normalize_html_fragment(content: str) -> str:
    """Claudeが返したHTML断片を、余分なラッパーを除去した形に整える。"""
    content = strip_code_fences(content)
    if not content:
        return ""

    wrapped = f'<div id="__codex_root__">{content}</div>'
    soup = BeautifulSoup(wrapped, "html.parser")
    root = soup.find(id="__codex_root__")
    if root is None:
        return content.strip()
    return root.decode_contents().strip()


def normalize_heading_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", "", text)
    return text.strip()


def canonical_h2_key(text: str) -> str:
    normalized = normalize_heading_text(text)
    if not normalized:
        return ""
    if "よくある質問" in normalized or "faq" in normalized.lower():
        return "faq"
    if "まとめ" in normalized:
        return "summary"
    if "オンライン診療" in normalized and "クリニック" in normalized:
        return "online_clinic"
    if "費用相場" in normalized or "料金相場" in normalized:
        return "cost"
    if "治療方法別" in normalized or "治療法別" in normalized:
        return "treatment_type"
    return normalized


def replace_or_append_h2_section(content: str, target_h2: str, section_html: str) -> str:
    """対象H2セクションを差し替える。存在しない場合は末尾に追加する。"""
    pattern = re.compile(
        rf'<h2[^>]*>\s*{re.escape(target_h2)}\s*</h2>.*?(?=<h2\b|$)',
        re.IGNORECASE | re.DOTALL,
    )
    section_html = section_html.strip()
    if pattern.search(content):
        return pattern.sub(section_html + "\n\n", content, count=1).strip()

    target_key = canonical_h2_key(target_h2)
    if target_key:
        actual_h2s, _ = extract_actual_headings(content)
        for actual_h2 in actual_h2s:
            if actual_h2 == target_h2:
                continue
            if canonical_h2_key(actual_h2) != target_key:
                continue
            similar_pattern = re.compile(
                rf'<h2[^>]*>\s*{re.escape(actual_h2)}\s*</h2>.*?(?=<h2\b|$)',
                re.IGNORECASE | re.DOTALL,
            )
            if similar_pattern.search(content):
                return similar_pattern.sub(section_html + "\n\n", content, count=1).strip()

    return (content.rstrip() + "\n\n" + section_html).strip()


def extract_expected_headings(tag_structure: str) -> tuple[list[str], list[str]]:
    h2s = [m.strip() for m in re.findall(r"^### \[H2\] (.+)$", tag_structure, re.MULTILINE)]
    h3s = [m.strip() for m in re.findall(r"^#### \[H3\] (.+)$", tag_structure, re.MULTILINE)]
    return h2s, h3s


def parse_tag_structure_sections(tag_structure: str) -> list[dict]:
    sections = []
    current = None
    for line in tag_structure.splitlines():
        if line.startswith("### [H2] "):
            if current:
                sections.append(current)
            current = {"h2": line.replace("### [H2] ", "", 1).strip(), "h3s": []}
        elif line.startswith("#### [H3] ") and current is not None:
            current["h3s"].append(line.replace("#### [H3] ", "", 1).strip())
    if current:
        sections.append(current)
    return sections


def extract_section_markdown(tag_structure: str, target_h2: str) -> str:
    pattern = re.compile(
        rf"(^### \[H2\] {re.escape(target_h2)}.*?)(?=^### \[H2\] |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(tag_structure)
    return match.group(1).strip() if match else ""


def extract_actual_headings(html: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    h2s = [node.get_text(" ", strip=True) for node in soup.find_all("h2")]
    h3s = [node.get_text(" ", strip=True) for node in soup.find_all("h3")]
    return h2s, h3s


def find_missing_headings(tag_structure: str, html: str) -> tuple[list[str], list[str]]:
    expected_h2s, expected_h3s = extract_expected_headings(tag_structure)
    actual_h2s, actual_h3s = extract_actual_headings(html)
    actual_h2_keys = {canonical_h2_key(heading) for heading in actual_h2s}
    missing_h2s = [
        heading
        for heading in expected_h2s
        if canonical_h2_key(heading) not in actual_h2_keys
    ]
    missing_h3s = [heading for heading in expected_h3s if heading not in actual_h3s]
    return missing_h2s, missing_h3s


# ========================================
# 環境変数・設定読み込み
# ========================================
def load_api_key() -> str:
    """ANTHROPIC_API_KEYを取得"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        env_path = os.path.join(SCRIPT_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip("'\"")
                        break
    if not api_key:
        print("Error: ANTHROPIC_API_KEY が設定されていません。")
        print("  .env に ANTHROPIC_API_KEY=your-key を追加してください。")
        sys.exit(1)
    return api_key


def load_genre(genre_id: str) -> dict:
    """ジャンル設定を読み込む"""
    genre_path = os.path.join(GENRES_DIR, f"{genre_id}.json")
    if not os.path.exists(genre_path):
        available = [os.path.splitext(os.path.basename(f))[0]
                     for f in glob.glob(os.path.join(GENRES_DIR, "*.json"))]
        print(f"Error: ジャンル設定が見つかりません: {genre_path}")
        print(f"  利用可能なジャンル: {', '.join(available) if available else 'なし'}")
        sys.exit(1)
    with open(genre_path, encoding="utf-8") as f:
        return json.load(f)


def load_scraped_data(keyword: str, scraped_dir: str = None) -> dict:
    """スクレイピングデータを読み込む"""
    scraped_dir = scraped_dir or get_keyword_scraped_dir(keyword)

    summary_path = os.path.join(scraped_dir, "summary.json")
    if not os.path.exists(summary_path):
        print(f"Error: スクレイピングデータが見つかりません: {summary_path}")
        print("  先に scrape.py を実行してください。")
        sys.exit(1)

    with open(summary_path, encoding="utf-8") as f:
        summary = json.load(f)

    structures = []
    for i in range(1, 4):
        path = os.path.join(scraped_dir, f"article_{i}_structure.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                structures.append(f.read())
        else:
            structures.append("")

    return {"summary": summary, "structures": structures}


# ========================================
# Claude API呼び出し
# ========================================
def call_claude(system_prompt: str, user_prompt: str, api_key: str,
                max_tokens: int = 8192):
    """Claude APIを呼び出す

    Returns:
        (content, stop_reason, usage)
    """
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    req = urllib.request.Request(CLAUDE_API_URL, data=body, headers=headers)

    max_retries = 1 + len(CLAUDE_RETRY_DELAYS)
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())

            content = data["content"][0]["text"]
            stop_reason = data.get("stop_reason", "")
            usage = data.get("usage", {})
            return content, stop_reason, usage

        except urllib.error.HTTPError as e:
            error_body = e.read().decode()[:500]
            if e.code in (429, 529) and attempt < max_retries - 1:
                retry_after = 0
                try:
                    retry_after = int((e.headers or {}).get("retry-after", "0"))
                except Exception:
                    retry_after = 0
                wait = max(CLAUDE_RETRY_DELAYS[attempt], retry_after)
                log(f"  API {e.code} エラー。{wait}秒後にリトライ... ({attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            elif e.code == 401:
                print(f"Error: APIキーが無効です (401)")
                sys.exit(1)
            else:
                print(f"Error: Claude API エラー ({e.code})")
                print(f"  {error_body}")
                sys.exit(1)

        except Exception as e:
            if attempt < max_retries - 1:
                wait = CLAUDE_RETRY_DELAYS[min(attempt, len(CLAUDE_RETRY_DELAYS) - 1)]
                log(f"  接続エラー: {e}。{wait}秒後にリトライ...")
                time.sleep(wait)
                continue
            print(f"Error: Claude API 接続失敗: {e}")
            sys.exit(1)


# ========================================
# Step 2: タグ構成設計
# ========================================
STEP2_SYSTEM = """あなたはSEO記事の構成設計者です。
競合記事のスクレイピングデータ（見出し構造・ブロック構成・テキスト内容）を分析し、
そのキーワードで検索1位を取るための最適な記事構成（タグ構成）を設計してください。

## あなたの役割
- まず検索意図を判定し、記事タイプを決める
- 競合3記事に共通するH2トピックを抽出し、統合・再構成する
- 競合にないがSEO的に有効なセクション（FAQ等）を追加検討する
- 各H2/H3の下に配置すべきブロック（テーブル、テキスト、ショートコード等）を定義する
- 必要な場合のみクリニック/商材の掲載数を決定する
- 出力はマークダウンで行う（HTMLは出力しない）"""

STEP2_USER = """以下の競合記事データを分析し、「{keyword}」で検索1位を取るためのタグ構成を設計してください。

## 基本情報
- キーワード: {keyword}
- ジャンル: {genre_name}

## ジャンル設定
```json
{genre_json}
```

## 競合記事のサマリー
```json
{scraped_summary}
```

## 競合記事の詳細構成
### 記事1
{article_1_structure}

### 記事2
{article_2_structure}

### 記事3
{article_3_structure}

---

## 構成設計の指針

### 0. 検索意図の判定
- キーワードの主目的を最初に判定する
- 代表例:
  - 比較・おすすめ系（例: おすすめ, 比較, ランキング, クリニック）
  - Know系の解説（例: 副作用, 効果, 原因, 仕組み, いつから, やめると）
  - 費用・料金系（例: 値段, 相場, 費用, いくら）
  - 方法・対策系（例: 治し方, 予防, 改善, 対処法）
- 記事構成は、この検索意図に最適化する
- どのキーワードでも同じテンプレートを押し込まず、意図に合う見出しだけを採用する

### 1. 競合分析の方法
- 3記事のH2見出しを横断的に比較し、**共通するトピック**を特定する
- 共通トピックは必ず含める（検索意図に対する必須要素）
- 1記事にしかないトピックは、SEO的に有効かどうかを判断して含めるか決める
- 競合にないがSEO的に差別化できるセクション（FAQ、選び方ガイド等）を追加検討する

### 2. H2の設計
- 3記事の共通H2トピックを統合・再構成してH2見出しを決める
- 各H2にどのようなブロック（テキスト、テーブル、ショートコード等）を配置するかを定義する
- **設計根拠**として「競合のどの見出しを統合/再構成したか」をコメントで明記する
- H2は検索意図に必要なものだけを採用し、無理に網羅しすぎない

### 3. H3の設計
- H2の下にH3を配置する場合、その構成も定義する
- クリニック/商材の個別セクション（H3）は、掲載するすべての見出しを個別に列挙する
- 「以下同様」「以下繰り返し」「テンプレートで流用」などの省略表現は禁止
- 各H3ごとにブロック構成を具体化し、Step 3が迷わず本文化できる粒度まで書く
- 掲載するクリニック/商材名は、比較系またはおすすめ系のときのみ競合データから抽出し、リストアップする
- Know系や解説系では、H3は論点分解（例: 原因、注意点、対処法、受診目安）を優先する

### 4. クリニック/商材の掲載数
- これは比較系・おすすめ系のときだけ適用する
- 3記事の掲載数を確認し、中央値〜やや多めを目安にする
- 具体的なクリニック/商材名を列挙する
- 競合で多く取り上げられているものを優先する

### 4.5. 検索意図との整合性
- キーワードに地域名が含まれない場合、titleタグ・メタディスクリプション・主要H2を特定地域に固定しない
- 競合が地域特化でも、記事全体の主題はキーワードの検索意図に合わせる
- 地域情報を使う場合は補足観点に留め、記事全体を地域比較記事にしない

### 5. 導入部分
- リード文の方向性（検索意図への共感 → 記事で解決できること）
- 比較系・おすすめ系なら早見表ショートコードと一覧ボックスを配置する
- Know系・解説系なら、要点整理や結論先出しを優先し、比較向けショートコードを無理に使わない

### 6. まとめセクション
- 記事全体の要約
- 比較系ならCTA（早見表ショートコード）
- Know系なら結論整理と次の行動（受診目安、公式確認など）
- 注意書き（税込表記・公式サイト確認の旨）

### 7. 情報の抽出
- 比較系なら、各クリニック/商材の**具体的な情報**（料金、住所、診療時間、特徴等）を抽出してメモとして含める
- Know系なら、症状・原因・副作用・受診目安・対処法などの論点別ファクトを抽出してメモとして含める
- この情報はStep 3（本文生成）で使う素材になる

### 8. 禁止事項
- H3の省略記法（例: 「以下同様」「以下繰り返し」「テンプレートで15院を紹介」）
- 根拠のない地域固定化
- Step 3に丸投げする前提の雑な設計
- 競合本文のコピペに近い羅列
- 「要確認」前提の設計
- 比較系でもないのにクリニック一覧を無理に主軸にすること
- Know系なのに「おすすめ◯選」に寄せること

## 出力形式
以下のフォーマットで出力してください:

```
# 「{keyword}」タグ構成設計

**titleタグ**: ...
**メタディスクリプション**: ...
**検索意図**: ...（比較/おすすめ系・Know系・費用系・方法系 など）
**記事タイプ**: ...（比較記事 / 解説記事 / 費用解説記事 / 対処法記事 など）
**想定文字数**: ...

---

## 導入部分
**構成:**
1. テキスト（リード文: ...）
2. 必要に応じたブロック（比較系なら早見表ショートコード、Know系なら要点整理）
3. 必要に応じたブロック（比較系なら一覧ボックスHTML）
4. 目次（WordPress自動生成）

---

## タグ構成

### [H2] 見出しテキスト
※この見出しを設計した根拠

**ブロック構成:**
- テキスト（概要説明）
- 比較テーブル（カラム: ...）
- テキスト（補足）

#### [H3] 見出しテキスト
**ブロック構成:**
- テキスト（このH3で最初に伝える結論）
- 詳細テーブル（必要な項目）
- テキスト（差別化ポイント）
- 必要なら口コミブロック or 比較補足

#### [H3] 見出しテキスト
**ブロック構成:**
- ...
```
比較系でない場合は、クリニックごとのテンプレートを無理に使わず、検索意図に適したH2/H3設計にしてください。"""


def run_step2(keyword: str, genre: dict, scraped_data: dict, api_key: str) -> str:
    """Step 2: タグ構成設計"""
    log("Step 2: タグ構成設計 開始")

    genre_json = json.dumps(genre, ensure_ascii=False, indent=2)
    summary_json = json.dumps(scraped_data["summary"], ensure_ascii=False, indent=2)

    user_prompt = STEP2_USER.format(
        keyword=keyword,
        genre_name=genre.get("genre_name", ""),
        genre_json=genre_json,
        scraped_summary=summary_json,
        article_1_structure=scraped_data["structures"][0] if len(scraped_data["structures"]) > 0 else "（データなし）",
        article_2_structure=scraped_data["structures"][1] if len(scraped_data["structures"]) > 1 else "（データなし）",
        article_3_structure=scraped_data["structures"][2] if len(scraped_data["structures"]) > 2 else "（データなし）",
    )

    total_chars = len(STEP2_SYSTEM) + len(user_prompt)
    log(f"  入力: {total_chars:,} 文字")
    log("  Claude API 呼び出し中...")

    content, stop_reason, usage = call_claude(
        STEP2_SYSTEM, user_prompt, api_key, max_tokens=MAX_TOKENS_STEP2
    )

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    log(f"  完了: {len(content):,} 文字出力 (入力: {input_tokens:,} tokens, 出力: {output_tokens:,} tokens)")

    if stop_reason == "max_tokens":
        log("  ⚠ 出力が途中で切れています（max_tokens到達）")

    # 保存
    keyword_slug = keyword_to_slug(keyword)
    output_dir = ensure_keyword_output_dir(keyword)
    tag_path = os.path.join(output_dir, f"{keyword_slug}_タグ構成.md")
    with open(tag_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"  保存: {tag_path}")

    return content


# ========================================
# Step 3: 本文HTML生成
# ========================================
STEP3_SYSTEM = """あなたはSEO記事のHTMLライターです。
タグ構成設計書と競合記事データを受け取り、WordPress用のHTML記事を出力してください。

## あなたの役割
- タグ構成設計書に従った構成・見出しでHTML記事を書くこと
- 競合記事のデータから具体的な情報（料金・住所・診療時間等）を正確に反映すること
- 検索キーワードで Google 1位を取れる記事を書くこと（情報網羅性・E-E-A-T優先）
- タグ構成設計書に書かれた検索意図・記事タイプに従って、比較記事にもKnow記事にも対応すること

## 出力ルール
- HTMLのみ出力する。マークダウンや説明文は一切含めない
- ```html ``` のコードフェンスで囲まない
- CSSクラス名だけ付与する（CSSの定義自体は書かない）
- <style>タグ、<script>タグは含めない
- ショートコードやプレースホルダーは、タグ構成設計書で必要なものだけ出力する
- 不明な情報に対して `※要確認` や説明コメントを出力しない。確認できない項目は、その文・行・セルを省略する"""

STEP3_USER = """以下のタグ構成設計書に従って、WordPress用のHTML記事を出力してください。

## 基本情報
- キーワード: {keyword}
- キーワードスラッグ: {keyword_slug}（スペースを_に置換）
- ジャンル: {genre_name}

## ジャンル設定
```json
{genre_json}
```

## タグ構成設計書（Step 2の出力）
{tag_structure}

## 競合記事の詳細データ（情報ソース）
{scraped_structures}

---

# HTMLルール

以下のルールをすべて守ってHTMLを出力してください。

## 1. 導入部分（H2の前）

タグ構成設計書の「導入部分」に従って以下を配置:
1. リード文（2段落）
   - 1段落目: 検索意図への共感
   - 2段落目: この記事で何がわかるかの提示
2. タグ構成設計書の導入構成にショートコードがある場合のみ出力
3. タグ構成設計書の導入構成に一覧ボックスがある場合のみ、`clinic-index-box` の実HTMLを出力
4. HTMLコメント: `<!-- ↑ここまでが導入部分。この直後にWordPress自動生成の目次が入る -->`

## 2. H2セクション

タグ構成設計書で定義されたH2をそのまま使う。
各H2の直下にH2見出し画像を配置:
```
<img src="images/{keyword_slug}_h2_{{n}}.png" alt="{{H2見出しテキスト}}" width="1200" height="675" loading="lazy">
```
- nは1から連番
- タグ構成設計書で定義されたブロック構成に従う
- タグ構成設計書にないH2を追加しない
- タグ構成設計書にあるH2を省略しない

## 3. クリニック/商材 個別セクション（H3）のテンプレート

タグ構成設計書が比較記事・おすすめ記事で、クリニック/商材の個別H3セクションを定義している場合のみ、以下のテンプレートに従って各クリニック/商材を出力する:

```
<!-- ============================== -->
<!-- クリニック名 -->
<!-- ============================== -->
<h3 id="clinic-xxx">クリニック名</h3>

<div class="clinic-screenshot">
<img src="images/screenshot_クリニック名.png" alt="クリニック名の公式サイト" width="1200" height="800" loading="lazy">
</div>

<table class="clinic-summary-table">
<tbody>
<tr><th>カラム1</th><td>値</td></tr>
</tbody>
</table>

<p>テキスト（3〜4段落。ジャンル設定の text_requirements に含まれる情報を盛り込む）</p>

<table class="clinic-detail-table">
<tbody>
<tr><th>カラム1</th><td>値</td></tr>
</tbody>
</table>

<div class="review-section">
<h4>口コミ・評判</h4>
<div class="review-bubble"><div class="review-avatar"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#999" width="22" height="22"><path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/></svg></div><div class="review-body"><div class="review-meta"><span class="review-stars">Google口コミ</span><span>口コミ1</span></div>口コミ本文</div></div>
</div>

<div class="clinic-map">
<iframe src="https://www.google.com/maps?q=クリニック名&output=embed" allowfullscreen loading="lazy"></iframe>
</div>

<p class="official-site-button-wrap" style="margin:1em 0 1.5em;text-align:center;"></p>
```

### 3-1. H3のid命名規則
- `id="clinic-xxx"` の形式（英字小文字、ハイフン区切り）
- H2直下の比較テーブルのアンカーリンクと一致させる

### 3-2. オンライン専門クリニック
店舗を持たないオンライン専門クリニックの場合:
- Googleマップは挿入しない
- 代わりに `<!-- クリニック名はオンライン専門のためマップなし -->` のコメントを入れる

### 3-3. 構造の禁止事項
- 同じクリニック名を記事内で2回以上見出し化しない
- `.clinic-list` や `.clinic-item` のような追加ラッパーを勝手に作らない
- `.reviews` や `<blockquote class="review">` の旧口コミUIを出力しない
- H3クリニックセクションの外で、そのクリニック名をH4見出しとして再掲しない
- `<div>` の閉じ忘れ・二重閉じをしない
- H2画像は `.png` プレースホルダーだけを使い、`.jpg` は出力しない
- トップ画像 `images/{keyword_slug}_top.png` は本文に出力しない
- Know系・解説系では、上記のクリニック比較テンプレートを無理に使わない
- H3はタグ構成設計書で定義されたテキストをそのまま使い、省略しない

## 4. テーブルのCSSクラスルール

| 配置場所 | クラス名 |
|---------|---------|
| H3セクション内の最初のテーブル | `clinic-summary-table` |
| H3セクション内の2番目のテーブル | `clinic-detail-table` |
| H2直下で thead を持つ比較テーブル | `treatment-compare-table` |

- `treatment-compare-table` のみ `<thead>` を付ける
- `clinic-summary-table` と `clinic-detail-table` は `<tbody>` のみ
- 比較テーブルは `<div style="overflow-x:auto;">` で囲む

## 5. テキストのライティングルール

### 5-1. 段落の長さ
- 1パラグラフ **80〜100文字以内**
- 長くなる場合は `<strong>` で視覚的にカバーする

### 5-2. 文体
- 事実ベースで書く。「〜と言われています」等の曖昧表現は避ける
- 各セクションの冒頭1文で結論を述べ、その後に補足する構成
- 「です・ます」調で統一

### 5-3. strong強調ルール
**50文字前後に1箇所** の頻度で `<strong>` を使う。短い段落でも最低1箇所は入れる。

強調すべきポイント:
- 価格情報（「初月○○円」「月々○○円」等）
- 差別化ポイント（「予約不要」「完全個室」「22時まで」等）
- 数値を伴う実績（「99.4%」「250万人」等）
- 制度・サービス名（「全額返金保証」「主治医制度」等）

## 6. 詳細テーブル内の表記ルール

`clinic-detail-table` 内で複数の薬や料金を列挙する場合、読点ではなく `<br>` 改行で表示する。

## 7. Googleマップ埋め込み

```
<div class="clinic-map">
<iframe src="https://www.google.com/maps?q=クリニック名&output=embed" allowfullscreen loading="lazy"></iframe>
</div>
```

## 8. 口コミのHTMLフォーマット

比較記事で口コミブロックがタグ構成設計書に含まれる場合のみ出力する。
口コミは競合記事のデータに含まれるものを使う（捏造しない）。
競合データに口コミが十分にない場合は、口コミブロック自体を省略する。
年代付きの架空口コミや、作り話の引用文は出力しない。
口コミUIは `review-section` 形式だけを使う。

## 9. ショートコード

ジャンル設定の `shortcodes` を参照して出力する。
ただし、タグ構成設計書で不要なショートコードは無理に出力しない。

## 10. まとめセクション

記事末尾に以下を配置:
1. まとめテキスト（2段落）
2. タグ構成設計書でCTAやショートコードが指定されている場合のみ出力
3. 注意書き: `<p><small>※本記事に記載の料金はすべて税込表記です。料金・診療時間・治療内容等は変更される場合がありますので、最新情報は各クリニックの公式サイトをご確認ください。</small></p>`

## 11. 情報の正確性

- 競合記事データの情報は、タグ構成設計書で必要な範囲に限って利用してよい
- 確認できない情報は書かない。表の行や本文を削る
- 公式サイトURLが不明な場合は、CTAボタンそのものを出力しない
- `{{後で作成:...}}` 形式のプレースホルダーは一切出力しない
- 生成指示文・要確認コメント・説明コメントを本文に残さない

## 12. タグ構成への忠実性

- タグ構成設計書の `検索意図` と `記事タイプ` に合う出力を行う
- タグ構成設計書で定義されたH2/H3はすべて出力する
- タグ構成設計書にないH2/H3を勝手に増やさない
- 比較記事でない場合、比較表・一覧ボックス・口コミ・マップを機械的に挿入しない

---

## 出力

タグ構成設計書のH2/H3構成に従い、上記ルールすべてを適用して、WordPress用のHTML記事を出力してください。
タグ構成設計書にない見出しを勝手に追加したり、定義されている見出しを省略したりしないでください。"""


def run_step3(keyword: str, genre: dict, tag_structure: str,
              scraped_data: dict, api_key: str) -> str:
    """Step 3: 本文HTML生成"""
    log("Step 3: 本文HTML生成 開始")

    keyword_slug = keyword.replace(" ", "_")
    genre_json = json.dumps(genre, ensure_ascii=False, indent=2)
    scraped_summary = json.dumps(scraped_data["summary"], ensure_ascii=False, indent=2)

    # 競合記事構成を結合
    scraped_text = ""
    for i, structure in enumerate(scraped_data["structures"], 1):
        if structure:
            scraped_text += f"\n### 記事{i}\n{structure}\n"

    user_prompt = STEP3_USER.format(
        keyword=keyword,
        keyword_slug=keyword_slug,
        genre_name=genre.get("genre_name", ""),
        genre_json=genre_json,
        tag_structure=tag_structure,
        scraped_structures=scraped_text,
    )

    total_chars = len(STEP3_SYSTEM) + len(user_prompt)
    log(f"  入力: {total_chars:,} 文字")
    log("  Claude API 呼び出し中（時間がかかります）...")

    content, stop_reason, usage = call_claude(
        STEP3_SYSTEM, user_prompt, api_key, max_tokens=MAX_TOKENS_STEP3
    )

    # 出力が途中で切れた場合、続きを要求
    while stop_reason == "max_tokens":
        log("  出力が途中で切れたため、続きを要求中...")
        continuation_prompt = "続きを出力してください。前回の出力の末尾:\n" + content[-500:]
        next_content, stop_reason, next_usage = call_claude(
            STEP3_SYSTEM, continuation_prompt, api_key, max_tokens=MAX_TOKENS_STEP3
        )
        content += next_content
        # usageを加算
        for key in next_usage:
            if key in usage and isinstance(usage[key], int):
                usage[key] += next_usage[key]

    content = normalize_html_fragment(content)
    missing_h2s, missing_h3s = find_missing_headings(tag_structure, content)
    sections = parse_tag_structure_sections(tag_structure)
    section_map = {section["h2"]: section for section in sections}
    h3_to_h2 = {
        h3: section["h2"]
        for section in sections
        for h3 in section["h3s"]
    }
    repair_count = 0
    max_repair_attempts = MAX_SECTION_REPAIR_ATTEMPTS * max(len(sections), 1)
    while (missing_h2s or missing_h3s) and repair_count < max_repair_attempts:
        if missing_h2s:
            target_h2 = missing_h2s[0]
        else:
            target_h2 = h3_to_h2.get(missing_h3s[0], "")
        section = section_map.get(target_h2, {"h2": target_h2, "h3s": []})
        section_markdown = extract_section_markdown(tag_structure, target_h2)
        repair_count += 1
        log(
            "  不足セクションを補完中... "
            f"(対象H2: {target_h2}, 不足H2: {len(missing_h2s)}件, 不足H3: {len(missing_h3s)}件, {repair_count}/{max_repair_attempts})"
        )
        repair_prompt = f"""以下の1セクションだけをHTMLで出力してください。

## 重要
- 出力はこのH2セクションだけに限定する
- 最初の行は必ず `<h2>{section["h2"]}</h2>` から始める
- 下記のH3が指定されている場合は、そのH3をこの順序どおりにすべて含める
- 既存セクションの再出力は禁止
- `※要確認` や説明コメントを出力しない
- 比較記事でない場合は、比較記事向けテンプレートを無理に使わない

## 対象H2
{section["h2"]}

## このH2配下に必ず含めるH3
{chr(10).join("- " + h for h in section["h3s"]) if section["h3s"] else "なし"}

## このH2のタグ構成抜粋
{section_markdown}

## 競合記事のサマリー
{scraped_summary}

## 既存記事の直前コンテキスト
{content[-2500:]}
"""
        repair_html, repair_stop_reason, repair_usage = call_claude(
            STEP3_SYSTEM, repair_prompt, api_key, max_tokens=MAX_TOKENS_STEP3
        )
        for key in repair_usage:
            if key in usage and isinstance(usage[key], int):
                usage[key] += repair_usage[key]

        while repair_stop_reason == "max_tokens":
            log("  セクション補完が途中で切れたため、続きを要求中...")
            continuation_prompt = (
                f"前回のH2 `{section['h2']}` セクションの続きを、重複なしで続けてHTMLだけ出力してください。末尾:\n"
                + repair_html[-500:]
            )
            next_content, repair_stop_reason, next_usage = call_claude(
                STEP3_SYSTEM, continuation_prompt, api_key, max_tokens=MAX_TOKENS_STEP3
            )
            repair_html += next_content
            for key in next_usage:
                if key in usage and isinstance(usage[key], int):
                    usage[key] += next_usage[key]

        repair_html = normalize_html_fragment(repair_html)
        content = replace_or_append_h2_section(content, target_h2, repair_html)
        missing_h2s, missing_h3s = find_missing_headings(tag_structure, content)

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    log(f"  完了: {len(content):,} 文字出力 (入力: {input_tokens:,} tokens, 出力: {output_tokens:,} tokens)")

    # 後処理: コードフェンス除去とHTML断片の正規化
    content = normalize_html_fragment(content)
    content = cleanup_generated_html(content, keyword_slug)
    missing_h2s, missing_h3s = find_missing_headings(tag_structure, content)
    if missing_h2s or missing_h3s:
        print("Error: タグ構成どおりに本文を出力し切れていません。")
        if missing_h2s:
            print("  不足H2: " + " / ".join(missing_h2s[:5]))
        if missing_h3s:
            print("  不足H3: " + " / ".join(missing_h3s[:5]))
        sys.exit(1)
    issues = validate_article_html(content, keyword_slug)
    if issues:
        print("Error: 生成HTMLの構造検証に失敗しました。")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)

    # 保存
    output_dir = ensure_keyword_output_dir(keyword)
    ensure_common_assets(keyword)
    html_path = os.path.join(output_dir, f"{keyword_slug}_記事.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"  保存: {html_path}")

    return content


# ========================================
# メインのオーケストレーション
# ========================================
def generate_article(keyword: str, genre_id: str, scraped_dir: str = None,
                     step: int = None) -> dict:
    """記事自動生成のメイン関数

    Args:
        keyword: 検索キーワード
        genre_id: ジャンルID（aga, ed, hair_removal等）
        scraped_dir: スクレイピングデータのディレクトリ
        step: None=両方, 2=Step2のみ, 3=Step3のみ

    Returns:
        dict: 実行結果
    """
    api_key = load_api_key()
    genre = load_genre(genre_id)
    scraped_data = load_scraped_data(keyword, scraped_dir)

    keyword_slug = keyword_to_slug(keyword)
    output_dir = ensure_keyword_output_dir(keyword)
    result = {
        "keyword": keyword,
        "genre_id": genre_id,
        "tag_structure_path": None,
        "html_path": None,
    }

    # Step 2: タグ構成設計
    tag_structure = None
    if step is None or step == 2:
        tag_structure = run_step2(keyword, genre, scraped_data, api_key)
        result["tag_structure_path"] = os.path.join(output_dir, f"{keyword_slug}_タグ構成.md")

    # Step 3: 本文HTML生成
    if step is None or step == 3:
        # Step 2の出力を取得（Step 2を実行していない場合はファイルから読み込み）
        if tag_structure is None:
            tag_path = os.path.join(output_dir, f"{keyword_slug}_タグ構成.md")
            if not os.path.exists(tag_path):
                print(f"Error: タグ構成ファイルが見つかりません: {tag_path}")
                print("  先に --step 2 を実行してください。")
                sys.exit(1)
            with open(tag_path, encoding="utf-8") as f:
                tag_structure = f.read()

        run_step3(keyword, genre, tag_structure, scraped_data, api_key)
        result["html_path"] = os.path.join(output_dir, f"{keyword_slug}_記事.html")

    log("記事生成 完了")
    return result


# ========================================
# CLI
# ========================================
def main():
    parser = argparse.ArgumentParser(description="SEO記事自動生成（Claude API）")
    parser.add_argument("--keyword", required=True, help="検索キーワード（例: AGA 横浜）")
    parser.add_argument("--genre", required=True, help="ジャンルID（例: aga, ed, hair_removal）")
    parser.add_argument("--step", type=int, choices=[2, 3],
                        help="実行ステップ（省略時は2→3を連続実行）")
    parser.add_argument("--scraped-dir",
                        help="スクレイピングデータのディレクトリ（デフォルト: scraped_data/）")

    args = parser.parse_args()

    print("=" * 50)
    print("SEO記事自動生成（Claude API）")
    print(f"  キーワード: {args.keyword}")
    print(f"  ジャンル: {args.genre}")
    print(f"  ステップ: {args.step or '2→3 連続実行'}")
    print("=" * 50)
    print()

    result = generate_article(
        keyword=args.keyword,
        genre_id=args.genre,
        scraped_dir=args.scraped_dir,
        step=args.step,
    )

    print()
    print("=" * 50)
    print("結果:")
    if result.get("tag_structure_path"):
        print(f"  タグ構成: {result['tag_structure_path']}")
    if result.get("html_path"):
        print(f"  記事HTML: {result['html_path']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
