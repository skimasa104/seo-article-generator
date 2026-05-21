# ブラウザ経由の記事画像生成

`generate_images_browser.py` は、ChatGPT のブラウザUIを Playwright で開いて、
記事用のトップ画像 / H2画像を生成するためのスクリプトです。

## 目的

- API を叩かずに画像生成したい
- いつもの ChatGPT 画面をブラウザで使いたい
- 生成した画像を `output/.../images/` に保存したい
- H2画像は記事HTMLにも差し込みたい

## 初回セットアップ

```bash
python -m playwright install chromium
```

## 実行例

```bash
python3 generate_images_browser.py \
  --keyword "ED治療 大阪" \
  --html output/ED治療_大阪__nandemo_v1/ED治療_大阪__nandemo_v1_記事.html
```

## 動き方

1. ChatGPT をブラウザで開く
2. ログイン済みでなければ、手動でログインする
3. 既存の記事内容からトップ画像 / H2画像のプロンプトを組み立てる
4. Playwright が入力欄にプロンプトを流し込み、送信する
5. 生成された画像を保存する
6. H2画像は `*_記事.html` と `*_記事_for-wp.html` に差し込む

## よく使うオプション

- `--only top`
  トップ画像だけ生成

- `--only h2_3`
  3つ目のH2画像だけ生成

- `--force`
  既存画像があっても再生成

- `--skip-insert`
  画像保存だけ行い、HTML差し込みをしない

- `--user-data-dir <path>`
  ChatGPT のログイン状態を保持する Playwright プロファイル保存先

- `--browser-channel chrome`
  Chrome を使って起動する

## 保存先

- 画像: `output/<記事フォルダ>/images/`
- メタ情報: `*.png.meta.json`

## 注意点

- ChatGPT の画面構造が変わると、入力欄や画像取得のセレクタ調整が必要になることがあります。
- 画像取得は、まず元画像データの保存を試み、難しい場合はブラウザ上の画像要素をキャプチャします。
- 参照画像アップロードまでは自動化していません。まずは文章プロンプトのみで回す前提です。
