#!/usr/local/bin/python3.12
"""
SEO記事自動生成パイプライン（統合スクリプト）

キーワードとサイト設定を受け取り、以下を一気通貫で実行する:
  Step 0: Google検索 → 上位記事URL取得
  Step 1: 競合記事スクレイピング
  Step 2-4: Claude APIで記事HTML生成（TODO: 実装予定）
  Step 5: 公式サイトスクリーンショット
  Step 6: AI画像生成
  Step 7: WordPress投稿

使い方:
  python pipeline.py --keyword "AGA 横浜" --site sites/aurora_clinic.json
  python pipeline.py --keyword "AGA 横浜" --site sites/aurora_clinic.json --category "AGA"
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")


def log(msg: str):
    """タイムスタンプ付きログ出力"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def run_step(name: str, cmd: list[str], timeout: int = 300) -> dict:
    """パイプラインの各ステップを実行"""
    log(f"▶ {name} 開始")
    start = time.time()

    try:
        result = subprocess.run(
            cmd,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = round(time.time() - start, 1)

        if result.returncode == 0:
            log(f"  ✓ {name} 完了 ({elapsed}秒)")
            return {"status": "ok", "elapsed": elapsed, "output": result.stdout}
        else:
            log(f"  ✗ {name} 失敗 (exit={result.returncode})")
            log(f"    stderr: {result.stderr[:300]}")
            return {"status": "error", "elapsed": elapsed, "error": result.stderr[:500]}

    except subprocess.TimeoutExpired:
        log(f"  ✗ {name} タイムアウト ({timeout}秒)")
        return {"status": "timeout", "elapsed": timeout}
    except Exception as e:
        log(f"  ✗ {name} 例外: {e}")
        return {"status": "exception", "error": str(e)}


def run_pipeline(keyword: str, site_config: str, category: str = "", title: str = "") -> dict:
    """パイプライン全体を実行"""

    keyword_slug = keyword.replace(" ", "_")
    results = {
        "keyword": keyword,
        "site_config": site_config,
        "started_at": datetime.now().isoformat(),
        "steps": {},
    }

    log(f"パイプライン開始: {keyword}")
    log(f"サイト設定: {site_config}")
    pipeline_start = time.time()

    # ==========================================
    # Step 0: Google検索 → 上位記事URL取得
    # ==========================================
    step = run_step(
        "Step 0: Google検索",
        [PYTHON, "search_keyword.py", keyword, "--count", "3"],
    )
    results["steps"]["search"] = step
    if step["status"] != "ok":
        results["final_status"] = "failed_at_search"
        return results

    # 検索結果JSONからURLを取得
    search_json = os.path.join(OUTPUT_DIR, f"{keyword_slug}_search_results.json")
    if not os.path.exists(search_json):
        results["final_status"] = "search_json_not_found"
        return results

    with open(search_json) as f:
        search_data = json.load(f)
    urls = [item["url"] for item in search_data.get("filtered", [])]
    log(f"  対象URL: {len(urls)}件")

    if not urls:
        results["final_status"] = "no_urls_found"
        return results

    # ==========================================
    # Step 1: 競合記事スクレイピング
    # ==========================================
    step = run_step(
        "Step 1: 競合記事スクレイピング",
        [PYTHON, "scrape.py", keyword] + urls,
        timeout=120,
    )
    results["steps"]["scrape"] = step
    if step["status"] != "ok":
        results["final_status"] = "failed_at_scrape"
        return results

    # ==========================================
    # Step 2-4: Claude APIで記事HTML生成
    # ==========================================
    # TODO: Claude API連携で自動生成
    # 現在は手動で作成済みのHTMLがある前提
    html_path = os.path.join(OUTPUT_DIR, f"{keyword_slug}_記事.html")

    if not os.path.exists(html_path):
        log(f"  ⚠ 記事HTMLが見つかりません: {html_path}")
        log(f"    → Step 2-4 (記事生成) は未実装のため、手動で作成してください")
        results["steps"]["generate_article"] = {
            "status": "skipped",
            "message": "記事HTMLが見つかりません。手動作成が必要です。",
            "expected_path": html_path,
        }
        results["final_status"] = "waiting_for_article"
        return results

    log(f"  記事HTML: {html_path}")
    results["steps"]["generate_article"] = {"status": "ok", "path": html_path}

    # ==========================================
    # Step 5: 公式サイトスクリーンショット
    # ==========================================
    step = run_step(
        "Step 5: スクリーンショット",
        [PYTHON, "capture_screenshots.py", "--html", html_path],
        timeout=300,
    )
    results["steps"]["screenshots"] = step

    # ==========================================
    # Step 6: AI画像生成
    # ==========================================
    step = run_step(
        "Step 6: AI画像生成",
        [PYTHON, "generate_images.py", "--keyword", keyword, "--html", html_path],
        timeout=600,
    )
    results["steps"]["images"] = step

    # ==========================================
    # Step 7: WordPress投稿
    # ==========================================
    wp_cmd = [
        PYTHON, "wp_post.py",
        "--html", html_path,
        "--site", site_config,
        "--status", "draft",
    ]
    if title:
        wp_cmd.extend(["--title", title])
    if category:
        wp_cmd.extend(["--category", category])

    step = run_step(
        "Step 7: WordPress投稿",
        wp_cmd,
        timeout=180,
    )
    results["steps"]["wordpress"] = step

    # ==========================================
    # 完了
    # ==========================================
    total_elapsed = round(time.time() - pipeline_start, 1)
    results["total_elapsed"] = total_elapsed
    results["finished_at"] = datetime.now().isoformat()

    failed_steps = [k for k, v in results["steps"].items() if v.get("status") not in ("ok", "skipped")]
    if failed_steps:
        results["final_status"] = f"completed_with_errors: {', '.join(failed_steps)}"
    else:
        results["final_status"] = "success"

    log(f"パイプライン完了: {results['final_status']} ({total_elapsed}秒)")

    # ログ保存
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{keyword_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log(f"ログ保存: {log_path}")

    return results


def main():
    parser = argparse.ArgumentParser(description="SEO記事自動生成パイプライン")
    parser.add_argument("--keyword", required=True, help="検索キーワード")
    parser.add_argument("--site", required=True, help="サイト設定JSONのパス")
    parser.add_argument("--category", default="", help="WordPressカテゴリ")
    parser.add_argument("--title", default="", help="記事タイトル（省略時は自動）")
    args = parser.parse_args()

    results = run_pipeline(
        keyword=args.keyword,
        site_config=args.site,
        category=args.category,
        title=args.title,
    )

    # 結果サマリー
    print("\n" + "=" * 60)
    print("パイプライン結果サマリー")
    print("=" * 60)
    for step_name, step_result in results.get("steps", {}).items():
        status = step_result.get("status", "?")
        elapsed = step_result.get("elapsed", "")
        elapsed_str = f" ({elapsed}秒)" if elapsed else ""
        print(f"  {step_name}: {status}{elapsed_str}")
    print(f"\n  最終結果: {results.get('final_status', '?')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
