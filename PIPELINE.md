# SEO記事自動生成パイプライン仕様書

## 全体フロー

```
キーワード入力
  ↓
Step 0: search_keyword.py — Google検索結果取得（Serper）
  ↓
Step 1: scrape.py — 競合記事スクレイピング
  ↓
Step 2: Claude — タグ構成設計
  ↓
Step 3: Claude — 本文作成（WordPress用HTML直接出力）
  ↓
Step 4: Claude — ファクトチェック（公式サイト照合→HTML修正）
  ↓
Step 4.3: sanitize_article.py — 未確定情報・生成指示文の掃除
  ↓
Step 4.5: fill_reviews.py — 口コミ補完（必要な記事のみ）
  ↓
Step 4.6: fill_final_cta.py — 末尾CTA挿入
  ↓
Step 4.7: fill_maps.py — Googleマップ挿入
  ↓
Step 5: capture_screenshots.py — 公式サイトスクリーンショット取得（任意）
  ↓
Step 6: generate_images.py — 記事画像生成 + HTML差し込み
  ↓
Step 7: wp_post.py — WordPress下書き投稿（REST API）
```

---

## Step 1: scrape.py（完成済み）

**入力**: キーワード + Step 0で取得した上位記事URL
**出力**: `scraped_data/{keyword_slug}/article_N_structure.md`, `scraped_data/{keyword_slug}/summary.json`
**処理**: HTML取得 → ノイズ除去 → メインコンテンツ特定 → タグ構成分析 → ブロックタイプ分類

---

## Step 2-4: Claude API ベースの本文生成フロー

### Step 2: タグ構成設計
- スクレイピング結果（3記事分の見出し構造・ブロック構成）を読み込む
- 最初に検索意図・記事タイプ（比較系 / Know系 / 費用系 / 方法系など）を判定する
- 競合3記事に共通するH2トピックを抽出し、検索意図に合わせて統合・再構成する
- 競合にないがSEO的に有効なセクション（FAQ等）を追加検討する
- 各見出しの下に配置するブロック（テーブル、テキスト、口コミ、一覧ボックス等）を定義する
- `※以下同様` のような省略記法は禁止し、必要なH3は個別に明示する
- 出力: `output/{keyword_slug}/{keyword_slug}_タグ構成.md`

### Step 3: 本文作成
- タグ構成に従って**WordPress用HTMLで直接出力**する
- 下記「HTMLルール」セクションの全ルールに従う
- **記事の目的はSEOで1位を取ること**。CVR最適化は公開後のリライトで行うため、この段階では検索意図に対する情報網羅性・E-E-A-Tを優先する
- タグ構成にあるH2/H3をすべて本文内に出し切る
- 不明な情報は `※要確認` として残さず、その行・段落自体を出さない
- 出力: `output/{keyword_slug}/{keyword_slug}_記事.html`

**本文のライティングルール:**
- 1パラグラフの目安は80〜100文字以内。長くなる場合はstrongで視覚的にカバー
- 事実ベースで書く。「〜と言われています」等の曖昧表現は避ける
- 各セクションの冒頭1文で結論を述べ、その後に補足する構成

### Step 4: ファクトチェック
- 記事内の価格・住所・診療時間・アクセス・診察料等を**各クリニックの公式サイト**と照合
- 誤りがあればHTMLを直接修正する
- 公式サイトで確認できない項目は残さず、比較表の該当セルは一般的な案内文へ、詳細表の未確認行は削除する
- クリニック単位で部分修正し、HTML全体を壊さないように反映する
- 修正結果は `output/{keyword_slug}/{keyword_slug}_factcheck_report.json` にも保存する

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
<!-- スクリーンショットは任意。取得できない場合はこのブロック自体を省略してよい -->

<table class="clinic-summary-table">
  <!-- カラム: genres/{genre_id}.json の summary_table.columns -->
</table>

<p>テキスト1〜4</p>
<!-- テキストは3〜4段落。各段落80〜100文字目安。要所をstrongで強調 -->
<!-- 含めるべき情報: genres/{genre_id}.json の text_requirements -->

<table class="clinic-detail-table">
  <!-- カラム: genres/{genre_id}.json の detail_table.columns -->
</table>

<div class="review-section">
  <!-- 口コミが必要な記事タイプのみ。fill_reviews.py で補完可 -->
</div>

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
| `shortcodes` | 早見表などのショートコード | `[sc name="aga-hayamihyou"]` |
| `summary_table.columns` | 概要テーブルのカラム名 | `[予防プラン, 発毛プラン, 診療時間, ...]` |
| `detail_table.columns` | 詳細テーブルのカラム名 | `[クリニック名, AGA治療薬, 診察料, ...]` |
| `text_requirements` | テキストに含めるべき情報 | `[強み, 料金メリット, 治療法, ...]` |
| `factcheck_fields` | ファクトチェック対象項目 | `[価格, 住所, 診療時間, ...]` |
| `notes` | 記事末尾の注意書き | `税込表記・公式サイト確認の旨` |

**現在作成済みのジャンル設定:**
- `genres/aga.json` — AGA治療
- `genres/ed.json` — ED治療
- `genres/hair_removal.json` — 医療脱毛
- `genres/phimosis.json` — 包茎治療
- `genres/diet.json` — 医療ダイエット

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
- 一覧ボックスや口コミはプレースホルダーではなく、Step 3 で直接HTML化するか、後続処理で自動補完する

ジャンルごとにショートコード名が異なる（AGA→`aga-hayamihyou`、ED→`ed-hayamihyou`等）。
記事作成時は必ず該当ジャンルのJSONを確認してから出力する。

#### 5-6. 導入部分の構成
H2の前（記事冒頭）に以下の順序で配置:
1. リード文（2段落。検索意図への共感 → 記事で何がわかるかの提示）
2. 早見表ショートコード
3. 一覧ボックスHTML（比較記事のみ）
4. HTMLコメント（「ここで目次が自動挿入される」旨）

#### 5-7. まとめセクション
記事末尾に以下を配置:
1. まとめテキスト（2段落）
2. 早見表ショートコード（CTA）
3. 注意書き（`<small>`タグ。税込表記・最新情報は公式サイト確認の旨）

---

## Step 5: capture_screenshots.py（完成済み）

**入力**: `output/{keyword_slug}/{keyword_slug}_記事.html`
**出力**: `output/{keyword_slug}/images/screenshot_クリニック名.png`, `output/{keyword_slug}/{keyword_slug}_urls.json`
**ツール**: Playwright（headless Chromium）

### 処理フロー
1. 記事HTMLからH3見出し（クリニック/商材名）を抽出
2. Step 4 で保存済みのURLリストJSONがあればそこからURL取得、なければ公式サイト検索で自動特定
3. 各公式サイトのファーストビュー（1200x800px）をスクリーンショット取得
4. スクリーンショット画像タグと公式サイトボタンを差し込む
5. 発見したURLリストをJSONに保存（次回実行時の再利用用）

### 使い方
```bash
# 自動実行（同じkeywordフォルダの urls.json を優先利用）
python3.12 capture_screenshots.py --html output/aga_横浜/aga_横浜_記事.html

# URLリストを明示指定して実行
python3.12 capture_screenshots.py --html output/aga_横浜/aga_横浜_記事.html --urls output/aga_横浜/aga_横浜_urls.json
```

### 注意事項
- スクリーンショットは任意。取得できなくても記事全体の生成は継続してよい
- Botブロックされるサイトは手動でURLを変更するか、スクショを手動で用意する
- Cookie同意バナーは自動で閉じようとするが、完全には対応できない場合がある

---

## Step 6: generate_images.py（完成済み）

**入力**: `output/{keyword_slug}/{keyword_slug}_記事.html` + キーワード + サイト設定JSON
**出力**: `output/{keyword_slug}/images/{keyword_slug}_*.png`
**API**: Google Gemini Native Image（Nano Banana 2 相当: `gemini-3.1-flash-image-preview`）

### 画像生成ルール

- サイト設定JSONの `site_url` を参照し、サイトの世界観に合うトーンで生成する
- 初回のみ対象ドメインのトップページを取得し、Geminiで世界観（配色・余白・上品さ・避ける表現）を解析して `site_style_cache/` に保存する
- 2回目以降は同じドメインのキャッシュを再利用し、毎回トップページを見直さない
- `aurora-clinic.jp` の場合は、白ベース・淡いラベンダー・余白多め・やさしい高級感・上品でクリーンな美容医療メディアの世界観に合わせる
- ただしSEO記事のサムネイル/H2画像として、本文より少し視認性を高めつつ、チラシ風・煽り広告風・情報過多の見た目にはしない
- トップページの被写体そのものは真似せず、記事テーマに合わせたモチーフで、世界観だけを継承する
- 「凝った」見た目は要素数を増やすことではなく、視線誘導・余白・主役と補助要素の関係・非対称レイアウトなど、構図設計で作る

#### 6-1. 必須画像
| 画像 | ファイル名 | 配置位置 | プロンプト方針 |
|------|-----------|---------|-------------|
| トップ画像 | `{keyword}_top.png` | リード文の直前 | キーワードのテーマがひと目で伝わるSEO記事用トップサムネ。サイトの世界観に合わせつつ、記事全体の入口になる明快な構図 |
| H2見出し画像 | `{keyword}_h2_{n}.png` | 各H2の直下 | そのセクションの内容を連想させる画像 |

#### 6-2. トップ画像のプロンプト設計
- タグ構成ファイルの `titleタグ` を優先し、`｜` の前後があればメイン見出しと補足見出しに分ける
- 単なる医療イラストではなく、SEO記事のサムネイルとして「何の記事か」が一目で伝わる構図にする
- サイト世界観は守るが、チラシ風・煽り広告風・情報過多にはしない
- 例:
  - メイン: `AGA治療おすすめクリニック15選`
  - 補足: `効果的な治療法と失敗しない選び方を徹底解説`

#### 6-3. H2見出し画像のプロンプト設計
| H2の内容 | 画像の方向性 |
|---------|-----------|
| クリニック一覧・比較 | 複数のクリニックを比較しているイメージ |
| 費用・治療法 | 治療薬・費用を連想させるイメージ |
| 選び方・ポイント | チェックリスト・選択のイメージ |
| FAQ・よくある質問 | Q&A・疑問解決のイメージ |
| まとめ | 最終決定・アクションのイメージ |

- H2見出し全文をそのまま画像文字に使わず、内容が端的に伝わる短いラベルへ短縮する
- 例:
  - `AGA治療おすすめクリニック15選を徹底比較` → `おすすめクリニック`
  - `AGA治療の費用相場と安いクリニックの選び方` → `費用相場と料金`
  - `失敗しないAGAクリニックの選び方5つのポイント` → `失敗しない選び方`

#### 6-4. 画像仕様
- 形式: PNG
- 比率: 横長固定（16:9）
- スタイル: 日本語Webメディアのサムネイルとして成立する、シンプルで清潔感のある医療イラスト
- テキスト: Gemini側で短い見出し文字を画像内に直接レンダリングする
- 文字量: トップ画像は短いメイン見出し1本のみ。H2画像も短い見出し1本のみ
- 禁止: 長文、二重見出し、余計な英字、意味不明な記号、写実的すぎる人物写真風の絵
- ガードレール: 余計な文字、ロゴ、ウォーターマーク、偽テキストは不可
- 画像品質の確認は「サイト世界観に合うか」「何の章か一目で伝わるか」を優先し、必要なら再生成する

#### 6-5. HTMLへの挿入
```html
<!-- トップ画像 -->
<img src="images/{keyword}_top.png" alt="{タイトルから生成したalt}" width="1200" height="630" loading="eager">

<!-- H2画像 -->
<img src="images/{keyword}_h2_{n}.png" alt="{H2見出しテキスト}" width="1200" height="400" loading="lazy">
```

---

## Step 7: wp_post.py（完成済み）

**入力**: `output/{keyword_slug}/{keyword_slug}_記事.html` + サイト設定JSON
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
# 下書き投稿（基本運用）
python3.12 wp_post.py \
  --html output/aga_横浜/aga_横浜_記事.html \
  --site sites/example.json \
  --title "横浜のAGAおすすめクリニック11選" \
  --category "AGA"

# dry-run（実際には投稿しない）
python3.12 wp_post.py --html output/aga_横浜/aga_横浜_記事.html --site sites/example.json --dry-run
```

### サイト設定ファイル（`sites/{site_name}.json`）
```json
{
  "site_url": "https://your-site.com",
  "username": "admin",
  "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx",
  "rest_api_base": "https://your-site.com/wp-json/wp/v2",
  "skip_wordpress": false
}
```

WordPress管理画面 → ユーザー → プロフィール → 「アプリケーションパスワード」で生成する。
`sites/*.json`は`.gitignore`対象。テンプレートは`sites/_template.json`を参照。

`skip_wordpress: true` を設定すると、Step 6 まで実行して `wp_post.py` は自動スキップする。

---

## ディレクトリ構成

```
SEO記事作成/
├── PIPELINE.md          ← この仕様書（全ルール定義）
├── search_keyword.py    ← Step 0: Google検索結果取得
├── scrape.py            ← Step 1: スクレイピング
├── capture_screenshots.py ← Step 5: 公式サイトスクショ取得（任意）
├── generate_images.py   ← Step 6: 記事画像生成 + HTML差し込み
├── wp_post.py           ← Step 7: WordPress下書き投稿（REST API）
├── sites/               ← WordPress サイト設定（※gitignore対象）
│   ├── _template.json   ← テンプレート
│   └── {site_name}.json ← サイトごとの接続情報
├── genres/              ← ジャンル設定ファイル（ジャンル追加時はここにJSONを追加）
│   ├── aga.json         ← AGA治療
│   ├── ed.json          ← ED治療
│   └── hair_removal.json ← 医療脱毛
├── scraped_data/        ← スクレイピング結果
│   └── {keyword_slug}/
│       ├── article_N_structure.md
│       └── summary.json
└── output/              ← 記事出力
    ├── article-common.css   ← 全ジャンル共通CSS（WPの「カスタムCSS & JS」に貼る。1回だけ）
    ├── article-common.js    ← 全ジャンル共通JS（WPの「カスタムCSS & JS」に貼る。1回だけ）
    └── {keyword_slug}/
        ├── {keyword_slug}_タグ構成.md
        ├── {keyword_slug}_記事.html
        ├── {keyword_slug}_urls.json
        ├── {keyword_slug}_factcheck_report.json
        └── images/
            ├── {keyword_slug}_top.png
            ├── {keyword_slug}_h2_N.png
            └── screenshot_クリニック名.png
```

---

## 今後のシステム化ロードマップ

### Phase 1（現在）: 自動パイプラインの安定化
- search_keyword.py: 稼働
- scrape.py: 稼働
- タグ構成設計〜本文作成〜ファクトチェック: Claude API 連携済み
- capture_screenshots.py: 稼働（任意）
- generate_images.py: 稼働（Gemini Native Image / Nano Banana 系）
- wp_post.py: 稼働（WordPress REST API下書き投稿）

### Phase 2: 品質ゲートの強化
- 画像品質レビューの自動化
- Step 0〜7 の一発完走安定性向上
- キュー制御と429耐性の強化

### Phase 3: 完全自動化
- キーワード入力 → Google検索 → URL自動取得 → 全工程自動実行
- キーワード自動選定（トラフィック分析）
- 公開後のリライト・CVR改善
