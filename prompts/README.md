# prompts/

`generate_article.py` が読み込む Claude API 用プロンプトファイル。

## ファイル

| ファイル | 用途 |
|---|---|
| `step2_system.md` | Step 2（タグ構成設計）のシステムプロンプト |
| `step2_user.md` | Step 2 のユーザープロンプト（テンプレート） |
| `step3_system.md` | Step 3（本文HTML生成）のシステムプロンプト |
| `step3_user.md` | Step 3 のユーザープロンプト（テンプレート） |

## ルール正本との関係

詳細な記事作成ルールは **`docs/ARTICLE_RULES.md`** が正本。
各プロンプトファイル内の `{article_rules}` プレースホルダーは、`generate_article.py` の `_load_prompt()` 関数によって `docs/ARTICLE_RULES.md` の全文に置換されてからユーザープロンプトに渡される。

つまり: **新しいルールを追加・修正したいときは `docs/ARTICLE_RULES.md` を編集すれば、次の生成から自動で反映される**。
プロンプトファイルを編集する必要があるのは、ルールではなく「プロンプトの構造・呼び方」を変えたいときだけ。

## プレースホルダー

`{...}` で囲まれた変数は `generate_article.py` の `.format(...)` で埋められる:

- `{keyword}`, `{keyword_slug}`, `{genre_name}`, `{current_year}`
- `{genre_json}` — `genres/{genre_id}.json` の中身
- `{seo_brief}` — `build_seo_brief()` が組み立てた競合集計
- `{scraped_summary}` — `scraped_data/{slug}/summary.json`
- `{article_1_structure}` 〜 `{article_3_structure}` — `scraped_data/{slug}/article_N_structure.md`
- `{tag_structure}` — Step 2 が出力したタグ構成設計書
- `{scraped_structures}` — 競合記事を結合したテキスト
- `{editorial_variant_instruction}` — variant 別の編集方針
- `{article_rules}` — `docs/ARTICLE_RULES.md` の全文（システムプロンプト側）

## 廃止ファイル

- `generate_article.md.deprecated` — 旧仕様の単一プロンプトファイル。コードからは参照されていなかったので削除予定。ARTICLE_RULES.md と各 step プロンプトに統合済み。
