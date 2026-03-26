# SEO記事自動生成パイプライン仕様書

## 全体フロー

```
キーワード入力
  ↓
Step 1: scrape.py — 競合記事スクレイピング
  ↓
Step 2: Claude — タグ構成設計
  ↓
Step 3: Claude — 本文作成（WordPress用HTML直接出力）
  ↓  ※HTMLルール（テーブルclass・strong強調・Googleマップ・画像配置）は
  ↓  このファイルのStep 5に定義。Claudeはそれに従ってHTML出力する
  ↓
Step 4: Claude — ファクトチェック（公式サイト照合→HTML修正）
  ↓
Step 5: capture_screenshots.py — 公式サイトスクリーンショット取得（Playwright）
  ↓
Step 6: generate_images.py — 記事画像生成（Gemini Imagen）
  ↓
Step 7: 画像をHTMLに挿入 → 最終HTML完成
  ↓
Step 8: wp_post.py — WordPress下書き投稿（REST API）
```

※ generate_html.py（MD→HTML変換スクリプト）は現Phase不要。
  ClaudeがHTMLルールに従って直接HTML出力するため。
  Phase 2（Claude API連携）でAPI側のプロンプトに組み込む際にスクリプト化を検討。

---

## Step 1: scrape.py（完成済み）

**入力**: キーワード + 上位記事URL（手動入力）
**出力**: `scraped_data/article_N_structure.md`, `scraped_data/summary.json`
**処理**: HTML取得 → ノイズ除去 → メインコンテンツ特定 → タグ構成分析 → ブロックタイプ分類

---

## Step 2-4: Claude（手動 — 将来的にAPI化）

### Step 2: タグ構成設計
- スクレイピング結果（3記事分の見出し構造・ブロック構成）を読み込む
- 競合3記事に共通するH2トピックを抽出し、統合・再構成する
- 競合にないがSEO的に有効なセクション（FAQ等）を追加検討する
- 各見出しの下に配置するブロック（テーブル、テキスト、ショートコード等）を定義
- クリニック等の掲載数は競合の平均〜やや多めを目安にする
- 出力: `output/{keyword}_タグ構成.md`

### Step 3: 本文作成
- タグ構成に従って**WordPress用HTMLで直接出力**する
- 下記「HTMLルール」セクションの全ルールに従う
- ショートコードはプレースホルダーで挿入位置を明示
- **記事の目的はSEOで1位を取ること**。CVR最適化は公開後のリライトで行うため、この段階では検索意図に対する情報網羅性・E-E-A-Tを優先する
- 出力: `output/{keyword}_記事.html`

**本文のライティングルール:**
- 1パラグラフの目安は80〜100文字以内。長くなる場合はstrongで視覚的にカバー
- 事実ベースで書く。「〜と言われています」等の曖昧表現は避ける
- 各セクションの冒頭1文で結論を述べ、その後に補足する構成

### Step 4: ファクトチェック
- 記事内の価格・住所・診療時間・アクセス・診察料等を**各クリニックの公式サイト**と照合
- 並列で複数のエージェントを使い、公式サイトを直接確認する
- 誤りがあればHTMLを直接修正する
- 公式サイトで確認できない情報にはHTMLコメントで`<!-- ※要確認 -->`等で注記
- 修正結果をHTMLに直接反映

---

## HTMLルール（Step 3でClaude が直接出力する際に従うルール）

### 概要
Claudeが記事HTMLを出力する際、以下のルールをすべて適用する。
これらは `aga_横浜_記事.html` を作成する過程で確定したルール。

#### クリニック/商材 個別セクション（H3）の構成テンプレート
各クリニック・商材のH3セクションは以下の順序で統一する。
**テーブルのカラム名・テキストに含めるべき情報はジャンル設定ファイル（`genres/{genre_id}.json`）を参照する。**

```html
<h3>クリニック名/商材名</h3>

<div class="clinic-screenshot">
<img src="images/screenshot_クリニック名.png" alt="クリニック名の公式サイト" width="1200" height="800" loading="lazy">
</div>
<!-- ↑ capture_screenshots.py で自動取得した公式HPスクリーンショット -->
<!-- アフィリンクバナーが用意できたら、ここをアフィカセットに差し替える -->

<table class="clinic-summary-table">
  <!-- カラム: genres/{genre_id}.json の summary_table.columns -->
</table>

<p>テキスト1〜4</p>
<!-- テキストは3〜4段落。各段落80〜100文字目安。要所をstrongで強調 -->
<!-- 含めるべき情報: genres/{genre_id}.json の text_requirements -->

<table class="clinic-detail-table">
  <!-- カラム: genres/{genre_id}.json の detail_table.columns -->
</table>

{shortcodes.口コミ}

<div class="clinic-map">
<iframe src="https://www.google.com/maps?q=クリニック名&output=embed" ...></iframe>
</div>
<!-- オンライン専門（店舗なし）はマップ挿入しない -->
```

#### ジャンル設定ファイル（`genres/{genre_id}.json`）

各ジャンルごとに以下を定義する:

| フィールド | 説明 | 例（AGA） |
|-----------|------|----------|
| `genre_id` | ジャンル識別子 | `aga` |
| `genre_name` | ジャンル表示名 | `AGA治療` |
| `article_type` | 記事タイプ | `クリニック比較` |
| `shortcodes` | 早見表・アフィカセット・口コミ等のショートコード/プレースホルダー | `[sc name="aga-hayamihyou"]` |
| `summary_table.columns` | 概要テーブルのカラム名 | `[予防プラン, 発毛プラン, 診療時間, ...]` |
| `detail_table.columns` | 詳細テーブルのカラム名 | `[クリニック名, AGA治療薬, 診察料, ...]` |
| `text_requirements` | テキストに含めるべき情報 | `[強み, 料金メリット, 治療法, ...]` |
| `factcheck_fields` | ファクトチェック対象項目 | `[価格, 住所, 診療時間, ...]` |
| `notes` | 記事末尾の注意書き | `税込表記・公式サイト確認の旨` |

**現在作成済みのジャンル設定:**
- `genres/aga.json` — AGA治療
- `genres/ed.json` — ED治療
- `genres/hair_removal.json` — 医療脱毛

**新ジャンル追加時:** `genres/` にJSONファイルを追加するだけで対応可能。

---

#### 5-1. テーブルデザイン
すべての`<table>`にCSSクラスを付与する。
**CSSは記事HTML内に`<style>`タグで書かない。** WordPress側の「カスタムCSS & JS」欄に`article-common.css`の内容を貼り付けて適用する。記事HTMLにはclass名だけ付ける。

```
クラス名の割り当てルール:
- クリニック個別セクション内の最初のテーブル → class="clinic-summary-table"
- クリニック個別セクション内の2番目のテーブル → class="clinic-detail-table"
- H2直下で thead を持つテーブル → class="treatment-compare-table"
```

**CSSルール:**
- th: 幅130px固定、背景#f5f5f5、左寄せ、white-space:nowrap
- td: padding 12px 16px、line-height 1.7
- border: 1px solid #ddd
- width: 100%、border-collapse: collapse

#### 5-2. テキスト強調（蛍光マーカー＋スクロールアニメーション）
**50文字前後に1箇所**の頻度で、以下のポイントを`<strong>`で囲む:
- 価格情報（「初月○○円」「月々○○円」等）
- クリニックの差別化ポイント（「予約不要」「完全個室」「22時まで」等）
- 数値を伴う実績（「99.4%」「250万人」「14万件」等）
- 制度・サービス名（「全額返金保証」「主治医制度」等）

**ルール:**
- 50文字前後に1箇所が目安。短い段落でも1箇所は入れる
- 文章の内容は変えない。視覚的なメリハリのみ追加

**デザイン（CSS + JS）:**
- `<strong>`には蛍光マーカー風アンダーライン（黄色グラデーション）を適用
- スクロールで画面に入ったタイミングでマーカーが左→右にアニメーション表示される
- CSS: `article-common.css` に `p strong` のスタイル（`background: linear-gradient(transparent 60%, #fff9c4 60%)` + `transition`）
- JS: `article-common.js` の `IntersectionObserver` で `is-visible` クラスを付与して `background-size` を 0→100% にアニメーション
- CSS・JSはともにWordPress側の「カスタムCSS & JS」に貼り付けて適用（記事HTMLには含めない）

#### 5-3. 詳細テーブルの薬価表示
`clinic-detail-table`内の「AGA治療薬」セルでは、複数の薬を読点（、）区切りではなく`<br>`改行で表示する。

```
変換前: フィナステリド：3,960円、デュタステリド：6,930円、ミノキシジル：8,250円
変換後: フィナステリド：3,960円<br>デュタステリド：6,930円<br>ミノキシジル：8,250円
```

#### 5-4. Googleマップ埋め込み
各クリニックの詳細テーブル直後に、Googleマップのiframeを埋め込む。

```html
<div class="clinic-map">
<iframe src="https://www.google.com/maps?q={クリニック名}&output=embed" allowfullscreen loading="lazy"></iframe>
</div>
```

**ルール:**
- クリニック名は記事中のH3見出しから取得
- オンライン専門クリニック（店舗なし）はマップを挿入しない
- CSSクラス `.clinic-map iframe` で width:100%, height:300px, border:0

#### 5-5. ショートコード
**ジャンル設定ファイル（`genres/{genre_id}.json`）の`shortcodes`を参照して出力する。**

- 既存ショートコード（WordPress側に実装済み）→ そのまま出力
- 未作成コンポーネント → `{{後で作成:〇〇}}`プレースホルダーで出力

ジャンルごとにショートコード名が異なる（AGA→`aga-hayamihyou`、ED→`ed-hayamihyou`等）。
記事作成時は必ず該当ジャンルのJSONを確認してから出力する。

#### 5-6. 導入部分の構成
H2の前（記事冒頭）に以下の順序で配置:
1. リード文（2段落。検索意図への共感 → 記事で何がわかるかの提示）
2. 早見表ショートコード
3. 一覧ボックスプレースホルダー
4. HTMLコメント（「ここで目次が自動挿入される」旨）

#### 5-7. まとめセクション
記事末尾に以下を配置:
1. まとめテキスト（2段落）
2. 早見表ショートコード（CTA）
3. 注意書き（`<small>`タグ。税込表記・最新情報は公式サイト確認の旨）

---

## Step 5: capture_screenshots.py（完成済み）

**入力**: `output/{keyword}_記事.html`（+ オプション: URLリストJSON）
**出力**: `output/images/screenshot_クリニック名.png`, `output/{keyword}_記事_urls.json`
**ツール**: Playwright（headless Chromium）

### 処理フロー
1. 記事HTMLからH3見出し（クリニック/商材名）を抽出
2. URLリストJSONがあればそこからURL取得、なければGoogle検索で自動特定
3. 各公式サイトのファーストビュー（1200x800px）をスクリーンショット取得
4. `{{後で作成:アフィカセット — クリニック名}}` プレースホルダーをスクショ画像タグに置換
5. 発見したURLリストをJSONに保存（次回実行時の再利用用）

### 使い方
```bash
# URLリストなしで実行（自動検索）
python3.12 capture_screenshots.py --html output/aga_横浜_記事.html

# URLリストを指定して実行（推奨）
python3.12 capture_screenshots.py --html output/aga_横浜_記事.html --urls output/aga_横浜_記事_urls.json

# URLリストのテンプレート生成（手動でURL入力用）
python3.12 capture_screenshots.py --html output/aga_横浜_記事.html --generate-urls
```

### 注意事項
- アフィリンクバナーが用意できたら、スクショをアフィカセットに差し替える
- Botブロックされるサイトは手動でURLを変更するか、スクショを手動で用意する
- Cookie同意バナーは自動で閉じようとするが、完全には対応できない場合がある

---

## Step 6: generate_images.py（完成済み）

**入力**: `output/{keyword}_記事.html` + キーワード
**出力**: `output/images/{keyword}_*.png`
**API**: Google Gemini Imagen（gemini-2.0-flash-preview-image-generation）

### 画像生成ルール

#### 6-1. 必須画像
| 画像 | ファイル名 | 配置位置 | プロンプト方針 |
|------|-----------|---------|-------------|
| トップ画像 | `{keyword}_top.png` | リード文の直前 | キーワードの地域名＋治療ジャンルが連想できる画像。「この記事を読みたい」と思わせるもの |
| H2見出し画像 | `{keyword}_h2_{n}.png` | 各H2の直下 | そのセクションの内容を連想させる画像 |

#### 6-2. トップ画像のプロンプト設計
キーワードを分解して画像要素を決定する:
- 地域名（横浜→横浜の街並み・ランドマーク）
- ジャンル（AGA→クリニック・医療・頭髪）
- 記事タイプ（おすすめ・比較→選択・チェックリスト）

例: 「AGA 横浜」の場合
→ 横浜の都市景観をベースに、医療・クリニック選びを連想させる清潔感のあるイラスト

#### 6-3. H2見出し画像のプロンプト設計
| H2の内容 | 画像の方向性 |
|---------|-----------|
| クリニック一覧・比較 | 複数のクリニックを比較しているイメージ |
| 費用・治療法 | 治療薬・費用を連想させるイメージ |
| 選び方・ポイント | チェックリスト・選択のイメージ |
| FAQ・よくある質問 | Q&A・疑問解決のイメージ |
| まとめ | 最終決定・アクションのイメージ |

#### 6-4. 画像仕様
- 形式: PNG
- サイズ: 1200x630px（OGP兼用）※トップ画像。H2画像は1200x400px
- スタイル: 清潔感のあるフラットイラスト or 写実的な写真風
- テキスト: 画像内にテキストは入れない（altタグで対応）

#### 6-5. HTMLへの挿入
```html
<!-- トップ画像 -->
<img src="images/{keyword}_top.png" alt="{タイトルから生成したalt}" width="1200" height="630" loading="eager">

<!-- H2画像 -->
<img src="images/{keyword}_h2_{n}.png" alt="{H2見出しテキスト}" width="1200" height="400" loading="lazy">
```

---

## Step 8: wp_post.py（完成済み）

**入力**: `output/{keyword}_記事.html` + サイト設定JSON
**出力**: WordPress下書き記事（画像はメディアライブラリに自動アップロード）
**API**: WordPress REST API（アプリケーションパスワード認証）

### 処理フロー
1. サイト設定JSONから接続情報を読み込み、API接続テスト
2. HTML内のローカル画像を `/wp-json/wp/v2/media` にアップロード → WordPressメディアURL取得
3. HTML内の画像パスをWordPressメディアURLに差し替え
4. トップ画像(`_top.png`)があればアイキャッチ画像に設定
5. カテゴリ指定があれば検索 or 自動作成
6. `/wp-json/wp/v2/posts` で下書き投稿

### 使い方
```bash
# 下書き投稿
python3.12 wp_post.py \
  --html output/aga_横浜_記事.html \
  --site sites/example.json \
  --title "横浜のAGAおすすめクリニック11選" \
  --category "AGA"

# dry-run（実際には投稿しない）
python3.12 wp_post.py --html output/aga_横浜_記事.html --site sites/example.json --dry-run

# 下書きを公開
python3.12 wp_post.py --publish --post-id 123 --site sites/example.json
```

### サイト設定ファイル（`sites/{site_name}.json`）
```json
{
  "site_url": "https://your-site.com",
  "username": "admin",
  "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx"
}
```

WordPress管理画面 → ユーザー → プロフィール → 「アプリケーションパスワード」で生成する。
`sites/*.json`は`.gitignore`対象。テンプレートは`sites/_template.json`を参照。

---

## ディレクトリ構成

```
SEO記事作成/
├── PIPELINE.md          ← この仕様書（全ルール定義）
├── scrape.py            ← Step 1: スクレイピング（完成済み）
├── capture_screenshots.py ← Step 5: 公式サイトスクショ取得（Playwright・完成済み）
├── generate_images.py   ← Step 6: 記事画像生成（Gemini Imagen・完成済み）
├── wp_post.py           ← Step 8: WordPress下書き投稿（REST API・完成済み）
├── sites/               ← WordPress サイト設定（※gitignore対象）
│   ├── _template.json   ← テンプレート
│   └── {site_name}.json ← サイトごとの接続情報
├── genres/              ← ジャンル設定ファイル（ジャンル追加時はここにJSONを追加）
│   ├── aga.json         ← AGA治療
│   ├── ed.json          ← ED治療
│   └── hair_removal.json ← 医療脱毛
├── scraped_data/        ← スクレイピング結果
│   ├── article_N_structure.md
│   └── summary.json
└── output/              ← 記事出力
    ├── article-common.css   ← 全ジャンル共通CSS（WPの「カスタムCSS & JS」に貼る。1回だけ）
    ├── article-common.js    ← 全ジャンル共通JS（WPの「カスタムCSS & JS」に貼る。1回だけ）
    ├── {keyword}_タグ構成.md
    ├── {keyword}_記事.html  ← ClaudeがHTMLルールに従って直接出力（styleタグなし、class名のみ）
    ├── {keyword}_記事_urls.json ← 公式サイトURLリスト（capture_screenshots.pyが自動保存）
    └── images/
        ├── {keyword}_top.png
        ├── {keyword}_h2_N.png
        └── screenshot_クリニック名.png ← 公式サイトスクリーンショット
```

---

## 今後のシステム化ロードマップ

### Phase 1（現在）: 半自動パイプライン
- scrape.py: 完成
- タグ構成設計〜本文作成〜ファクトチェック: Claude手動（PIPELINE.mdのルールに従う）
- capture_screenshots.py: 完成（公式サイトスクショ自動取得）
- generate_images.py: 完成（Gemini Imagen・APIキー設定後に利用可能）
- wp_post.py: 完成（WordPress REST API下書き投稿）

### Phase 2: Claude API連携
- scrape.py → Claude API でタグ構成自動設計
- タグ構成 → Claude API で本文自動生成
- 本文 → Claude API でファクトチェック自動実行

### Phase 3: 完全自動化
- キーワード入力 → Google検索 → URL自動取得 → 全工程自動実行
- キーワード自動選定（トラフィック分析）
- 公開後のリライト・CVR改善
