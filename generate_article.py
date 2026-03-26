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
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

# ========================================
# 設定
# ========================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
SCRAPED_DIR = os.path.join(SCRIPT_DIR, "scraped_data")
GENRES_DIR = os.path.join(SCRIPT_DIR, "genres")

CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS_STEP2 = 8192
MAX_TOKENS_STEP3 = 16384


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


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


def load_scraped_data(scraped_dir: str = None) -> dict:
    """スクレイピングデータを読み込む"""
    scraped_dir = scraped_dir or SCRAPED_DIR

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

    max_retries = 3
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
                wait = [10, 30, 60][attempt]
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
                wait = [10, 30][attempt] if attempt < 2 else 60
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
- 競合3記事に共通するH2トピックを抽出し、統合・再構成する
- 競合にないがSEO的に有効なセクション（FAQ等）を追加検討する
- 各H2/H3の下に配置すべきブロック（テーブル、テキスト、ショートコード等）を定義する
- クリニック/商材の掲載数を決定する
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

### 1. 競合分析の方法
- 3記事のH2見出しを横断的に比較し、**共通するトピック**を特定する
- 共通トピックは必ず含める（検索意図に対する必須要素）
- 1記事にしかないトピックは、SEO的に有効かどうかを判断して含めるか決める
- 競合にないがSEO的に差別化できるセクション（FAQ、選び方ガイド等）を追加検討する

### 2. H2の設計
- 3記事の共通H2トピックを統合・再構成してH2見出しを決める
- 各H2にどのようなブロック（テキスト、テーブル、ショートコード等）を配置するかを定義する
- **設計根拠**として「競合のどの見出しを統合/再構成したか」をコメントで明記する

### 3. H3の設計
- H2の下にH3を配置する場合、その構成も定義する
- クリニック/商材の個別セクション（H3）はテンプレート形式で1つ定義し、「以下繰り返し」とする
- 掲載するクリニック/商材名は競合データから抽出し、リストアップする

### 4. クリニック/商材の掲載数
- 3記事の掲載数を確認し、中央値〜やや多めを目安にする
- 具体的なクリニック/商材名を列挙する
- 競合で多く取り上げられているものを優先する

### 5. 導入部分
- リード文の方向性（検索意図への共感 → 記事で解決できること）
- 早見表ショートコードの配置
- 一覧ボックスの配置

### 6. まとめセクション
- 記事全体の要約
- CTA（早見表ショートコード）
- 注意書き（税込表記・公式サイト確認の旨）

### 7. 情報の抽出
- 競合記事のテキストデータから、各クリニック/商材の**具体的な情報**（料金、住所、診療時間、特徴等）を抽出してメモとして含める
- この情報はStep 3（本文生成）で使う素材になる

## 出力形式
以下のフォーマットで出力してください:

```
# 「{keyword}」タグ構成設計

**titleタグ**: ...
**メタディスクリプション**: ...
**想定文字数**: ...

---

## 導入部分
**構成:**
1. テキスト（リード文: ...）
2. ショートコード（早見表）
3. ショートコード（一覧ボックス）
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
- ...（以下同様）
```"""


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
    keyword_slug = keyword.replace(" ", "_")
    tag_path = os.path.join(OUTPUT_DIR, f"{keyword_slug}_タグ構成.md")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
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

## 出力ルール
- HTMLのみ出力する。マークダウンや説明文は一切含めない
- ```html ``` のコードフェンスで囲まない
- CSSクラス名だけ付与する（CSSの定義自体は書かない）
- <style>タグ、<script>タグは含めない
- ショートコードやプレースホルダーはそのまま出力する"""

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
2. 早見表ショートコード（ジャンル設定の `shortcodes.早見表` をそのまま出力）
3. 一覧ボックスプレースホルダー（ジャンル設定の `shortcodes.一覧ボックス` を出力。`{{count}}` は実際の掲載数で置換）
4. HTMLコメント: `<!-- ↑ここまでが導入部分。この直後にWordPress自動生成の目次が入る -->`

## 2. H2セクション

タグ構成設計書で定義されたH2をそのまま使う。
各H2の直下にH2見出し画像を配置:
```
<img src="images/{keyword_slug}_h2_{{n}}.jpg" alt="{{H2見出しテキスト}}" width="1200" height="675" loading="lazy">
```
- nは1から連番
- タグ構成設計書で定義されたブロック構成に従う

## 3. クリニック/商材 個別セクション（H3）のテンプレート

タグ構成設計書でクリニック/商材の個別H3セクションが定義されている場合、以下のテンプレートに従って各クリニック/商材を出力する:

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
<div class="review-bubble"><div class="review-avatar"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#999" width="22" height="22"><path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/></svg></div><div class="review-body"><div class="review-meta"><span class="review-stars">★★★★★</span><span>投稿者名</span></div>口コミ本文</div></div>
</div>

<div class="clinic-map">
<iframe src="https://www.google.com/maps?q=クリニック名&output=embed" allowfullscreen loading="lazy"></iframe>
</div>

<div style="text-align:center;margin:1.5em 0;">
<a href="公式サイトURL" target="_blank" rel="noopener noreferrer" style="display:inline-block;background:#4a90d9;color:#fff;padding:14px 40px;border-radius:6px;text-decoration:none;font-size:15px;font-weight:bold;">クリニック名の公式サイトはこちら</a>
</div>
```

### 3-1. H3のid命名規則
- `id="clinic-xxx"` の形式（英字小文字、ハイフン区切り）
- H2直下の比較テーブルのアンカーリンクと一致させる

### 3-2. オンライン専門クリニック
店舗を持たないオンライン専門クリニックの場合:
- Googleマップは挿入しない
- 代わりに `<!-- クリニック名はオンライン専門のためマップなし -->` のコメントを入れる

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

各クリニック2件ずつ。口コミは競合記事のデータに含まれるものを使う（捏造しない）。
競合データに口コミがない場合は `{{{{後で追加:口コミ — クリニック名}}}}` プレースホルダーを出力する。

## 9. ショートコード

ジャンル設定の `shortcodes` を参照して出力する。

## 10. まとめセクション

記事末尾に以下を配置:
1. まとめテキスト（2段落）
2. 早見表ショートコード（CTA）
3. 注意書き: `<p><small>※本記事に記載の料金はすべて税込表記です。料金・診療時間・治療内容等は変更される場合がありますので、最新情報は各クリニックの公式サイトをご確認ください。</small></p>`

## 11. 情報の正確性

- 競合記事データの情報はそのまま利用してよい
- 矛盾する情報には `<!-- ※要確認 -->` を付ける
- 公式サイトURLが不明な場合は `#` を仮置きし `<!-- ※要確認: 公式サイトURL -->` を付ける

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

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    log(f"  完了: {len(content):,} 文字出力 (入力: {input_tokens:,} tokens, 出力: {output_tokens:,} tokens)")

    # 後処理: コードフェンスを除去
    content = content.strip()
    if content.startswith("```html"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()

    # 保存
    html_path = os.path.join(OUTPUT_DIR, f"{keyword_slug}_記事.html")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
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
    scraped_data = load_scraped_data(scraped_dir)

    keyword_slug = keyword.replace(" ", "_")
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
        result["tag_structure_path"] = os.path.join(OUTPUT_DIR, f"{keyword_slug}_タグ構成.md")

    # Step 3: 本文HTML生成
    if step is None or step == 3:
        # Step 2の出力を取得（Step 2を実行していない場合はファイルから読み込み）
        if tag_structure is None:
            tag_path = os.path.join(OUTPUT_DIR, f"{keyword_slug}_タグ構成.md")
            if not os.path.exists(tag_path):
                print(f"Error: タグ構成ファイルが見つかりません: {tag_path}")
                print("  先に --step 2 を実行してください。")
                sys.exit(1)
            with open(tag_path, encoding="utf-8") as f:
                tag_structure = f.read()

        run_step3(keyword, genre, tag_structure, scraped_data, api_key)
        result["html_path"] = os.path.join(OUTPUT_DIR, f"{keyword_slug}_記事.html")

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
