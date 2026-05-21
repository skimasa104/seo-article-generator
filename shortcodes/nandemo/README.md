# なんでも用 AGA早見表ショートコード

`なんでも` サイトの AGA 記事で使う、バリアント別ショートコード本体です。

## 想定ショートコード名

- `aga-hayamihyou-1`
- `aga-hayamihyou-2`
- `aga-hayamihyou-3`
- `aga-hayamihyou-4`
- `aga-hayamihyou-5`

## 使い方

WordPress 側のショートコード管理プラグインに、各ファイルの中身をそのまま登録します。

- [aga-hayamihyou-1.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-1.html)
- [aga-hayamihyou-2.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-2.html)
- [aga-hayamihyou-3.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-3.html)
- [aga-hayamihyou-4.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-4.html)
- [aga-hayamihyou-5.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-5.html)

## リポジトリ側の挙動

このリポジトリは、`なんでも` の `AGA` 記事で `__nandemo_v1〜v5` を生成するときだけ、
導入部・末尾 CTA・本文内の早見表ショートコードを次のように切り替えます。

- `v1` -> `[sc name="aga-hayamihyou-1" ][/sc]`
- `v2` -> `[sc name="aga-hayamihyou-2" ][/sc]`
- `v3` -> `[sc name="aga-hayamihyou-3" ][/sc]`
- `v4` -> `[sc name="aga-hayamihyou-4" ][/sc]`
- `v5` -> `[sc name="aga-hayamihyou-5" ][/sc]`

他サイトは従来どおり `[sc name="aga-hayamihyou" ][/sc]` のままです。

## source of truth

- V3 の見た目ルールは [aga-hayamihyou-3.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-3.html) と [output/article-theme-v3.css](/Users/nihe/Downloads/seo-article-generator-main/output/article-theme-v3.css) を正とする
- V4 の見た目ルールは [aga-hayamihyou-4.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-4.html) と [output/article-theme-v4.css](/Users/nihe/Downloads/seo-article-generator-main/output/article-theme-v4.css) を正とする
- V5 の見た目ルールは [aga-hayamihyou-5.html](/Users/nihe/Downloads/seo-article-generator-main/shortcodes/nandemo/aga-hayamihyou-5.html) と [output/article-theme-v5.css](/Users/nihe/Downloads/seo-article-generator-main/output/article-theme-v5.css) を正とする
- `fill_final_cta.py` は `__nandemo_v4` / `__nandemo_v5` の末尾 CTA を外側ラッパーなしのプレーン配置で生成する
- つまり、キーワードが変わっても `__nandemo_v3` なら V3、`__nandemo_v4` なら V4、`__nandemo_v5` なら V5 の同一スタイルが自動で適用される
