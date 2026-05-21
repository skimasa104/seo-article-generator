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

## HTMLルール（Step 3 で Claude が直接出力する際に従うルール）

このセクションのルールはすべて **`docs/ARTICLE_RULES.md`** に移動しました。`PIPELINE.md` はパイプラインのステップ実行手順だけを持ち、ルール定義は `ARTICLE_RULES.md` を参照してください。

矛盾を見つけた場合は **`ARTICLE_RULES.md`** を直してください。本ファイルにルールを書き戻さないでください。

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

## Step 7.5: build_for_wp.py（手動投稿用HTML生成）

**入力**: `output/{keyword_slug}/{keyword_slug}_記事.html`
**出力**: `output/{keyword_slug}/{keyword_slug}_記事_for-wp.html`
**API**: 不要（ネット通信なし。`output/.image_url_cache.json` のみ参照）

WordPress REST API / XML-RPC が WAFで塞がれている等で `wp_post.py` を使えない場合に、**WordPress 管理画面のコードエディターに直接貼り付ける用** のHTMLを生成する。記事を作るたびに自動生成しておくのが本プロジェクトの規約。

### 出力HTMLの構造
1. 先頭: `output/article-common.css` を `<style>` でインライン化（`<!-- seo-article-common-css:start -->` マーカー付き）
2. 続いて: `output/article-common.js` を `<script>` でインライン化（同様にJSマーカー付き）
3. 本文: `_記事.html` の中身。`src="images/foo.png"` のローカル参照は `output/.image_url_cache.json` にあるWP配信URLに置換済み
4. キャッシュにヒットしなかった画像は **そのまま `images/...` で残る**（手動アップロード後に置換する想定）

`wp_post.py` と同じマーカーを使うので、再生成しても重複は発生しない（既存マーカー区間は剥がしてから再注入する）。

### 使い方
```bash
# 単一記事を変換
python3 build_for_wp.py --html output/AGA_名古屋__nandemo_v1/AGA_名古屋__nandemo_v1_記事.html

# フォルダ単位（再帰的に *_記事.html を全て処理）
python3 build_for_wp.py --dir output/AGA_名古屋__nandemo_v1
python3 build_for_wp.py --dir output  # output 配下を一括処理
```

### 規約
- 各記事フォルダには **`_記事.html` を作ったら必ず `_記事_for-wp.html` も生成する**
- `_記事_for-wp.html` は WordPress 管理画面 → 投稿編集 → コードエディターに丸ごと貼り付けて使う
- 画像URLキャッシュ `output/.image_url_cache.json` は `wp_post.py` が更新する。`build_for_wp.py` は読み取り専用
- `_記事_for-wp.html` は `_記事.html` の派生物なので、本文修正は **常に `_記事.html` 側で行い、`build_for_wp.py` で再生成**する

---

## バリアント記事の規約（v2〜v5）

`__nandemo_v2`〜`v5` のような同一キーワードの別バリアント記事を生成するときは、**必ず title タグとメタディスクリプションを v1 と別物にする**。本文だけ書き換えて title/meta が共通だと、検索エンジンに重複コンテンツとして認識されてしまうため。

### 必ずやること
1. `output/{slug}__nandemo_v{N}/{slug}__nandemo_v{N}_タグ構成.md` を必ず作成
2. その中の `**titleタグ**` と `**メタディスクリプション**` を v1 と**異なる訴求軸・別語順**で書く
3. リード文も差別化する（v1 のリードコピーを流用しない）
4. `build_for_wp.py` は同フォルダの `*_タグ構成.md` から自動で title/meta を抽出し、`_記事_for-wp.html` の冒頭に下記のHTMLコメントを差し込む（手動投稿時に貼り間違いを防ぐ）:
    ```html
    <!--
      ====================================
      WordPress 投稿時に設定する項目（HTML本文には含まれない）
      ====================================
      Title: ...
      Meta description: ...
    -->
    ```

### 訴求軸の振り分け例
| バリアント | 訴求軸の例 |
|---|---|
| v1 | 初月料金の安さ・全額返金保証 |
| v2 | 12ヶ月総額・続けやすさ |
| v3 | 対面 vs オンラインの選び分け |
| v4 | 治療法の幅・進行度別の選び方 |
| v5 | 受診のしやすさ・夜間/土日対応 |

データテーブル・画像・テーブルレイアウトはバリアント間で使い回してよい。差別化するのは title／meta／リード／クリニック紹介の地の文／まとめ／FAQ回答などの**散文**部分。

### バリアント別の見た目テーマ
v1〜v5 は本文の散文だけでなく**ビジュアルも別物**にする。`output/article-theme-v{N}.css` を `build_for_wp.py` が自動で base CSS の後ろに連結注入するため、各バリアントごとに color / table / font が違うサイトとして見える。

| variant | テーマ名 | 主色 | アクセント | 蛍光マーカー | 特徴 |
|---|---|---|---|---|---|
| v1 | Cool Navy | `#1e3a8a` ネイビー | `#06b6d4` シアン | 黄 `#fef08a` | 既定（`article-theme-v1.css` 不要、ベースのみ） |
| v2 | Warm Orange | `#c2410c` 燃え赤 | `#fbbf24` アンバー | ピーチ `#fed7aa` | 字間タイト・活発感 |
| v3 | Earthy Beige | `#78350f` ブラウン | `#d97706` バーンドオレンジ | クリーム `#fef3c7` | **丸みのある和文ゴシック**で親しみやすく |
| v4 | Soft Editorial Blue | `#89a6c9` ダスティブルー | `#d8a887` サンドベージュ | アプリコット `rgba(236,197,151,.72)` | 明るい比較表・淡色H2・V5系レビューアバター |
| v5 | Modern Lavender | `#75668f` ラベンダー | `#9f8dbe` ライトパープル | ラベンダー `#e8dff4` | モダンで少し締まった見た目 |

**仕組み:** `build_for_wp.py` がパスから `__nandemo_v(\d+)` を読み取り、`output/article-theme-v{N}.css` が存在すれば base CSS の末尾に連結して inline 注入する。テーマCSSは `!important` で base CSS の値を上書き。

**v1 はテーマファイル不要**: `article-common.css` がそのまま v1 の Cool Navy テーマになっているため、`article-theme-v1.css` は作らない。

### AGA 早見表のバリアント運用

`なんでも` の AGA 記事では、本文内の比較早見表だけでなく導入部・末尾 CTA に入る早見表も variant ごとの HTML を source of truth とする。

- `v1` は [shortcodes/nandemo/aga-hayamihyou-1.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-1.html)
- `v2` は [shortcodes/nandemo/aga-hayamihyou-2.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-2.html)
- `v3` は [shortcodes/nandemo/aga-hayamihyou-3.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-3.html)
- `v4` は [shortcodes/nandemo/aga-hayamihyou-4.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-4.html)
- `v5` は [shortcodes/nandemo/aga-hayamihyou-5.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-5.html)

V3 の source of truth は [shortcodes/nandemo/aga-hayamihyou-3.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-3.html) と [output/article-theme-v3.css](/Users/nihe/Downloads/seo-article-generator-main/output/article-theme-v3.css)。

### V3 完成ルール

V3 は現行デザインを正式版として固定する。今後 V3 を再生成・複製するときは、生成済み HTML を都度手修正するのではなく、必ず下記の source of truth を編集して反映する。

- 本文テーマの正本: [output/article-theme-v3.css](/Users/nihe/Downloads/seo-article-generator-main/output/article-theme-v3.css)
- AGA 早見表の正本: [shortcodes/nandemo/aga-hayamihyou-3.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-3.html)
- `*_記事_for-wp.html` は配布物であり、V3 の恒久ルール定義ファイルとしては扱わない

V3 の固定仕様:

- `.ndm-article` の背景は `transparent` を維持し、本文全体にうっすらしたベージュ背景を敷かない
- スマホ時の文字サイズは V3/V4/V5 で揃える
- 本文・`p`・`li`: `14px`
- `h2`: `18px`
- `h3`: `17px`
- V3 の違いは主に行間と配色・装飾で表現し、モバイル文字サイズ差では差別化しない

このルールにより、V3 は「やわらかい丸ゴシック寄り + アーシーベージュ + 透明な本文背景」の完成版として再利用する。

`variant_utils.py` と `generate_article.py` / `fill_final_cta.py` が `__nandemo_vN` を見て自動で対応ファイルへ差し替えるため、**キーワードが変わっても同じ variant 番号なら同じ見た目になる**。

### V4 / V5 末尾 CTA ルール

V4 と V5 の末尾 CTA は、外側に `.article-final-cta` ラッパーを持たせず、本文の流れにそのまま接続するプレーン配置を正とする。

- V4: 説明2段落 + `aga-hayamihyou-4.html` + 注意書き
- V5: 説明2段落 + `aga-hayamihyou-5.html` + 注意書き

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
