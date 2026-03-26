#!/usr/local/bin/python3.12
"""
SEO記事自動生成 Flaskサーバー

スプレッドシート(GAS)からのリクエストを受け取り、パイプラインを実行する。

起動:
  python server.py
  python server.py --port 5001

エンドポイント:
  POST /run     パイプライン実行（非同期）
  GET  /status  ジョブの状態確認
  GET  /health  ヘルスチェック
"""

import argparse
import json
import os
import sys
import threading
import uuid
from datetime import datetime

from flask import Flask, request, jsonify

# パイプラインのインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import run_pipeline

app = Flask(__name__)

# ジョブ管理（メモリ上）
jobs = {}


# ========================================
# エンドポイント
# ========================================
@app.route("/health", methods=["GET"])
def health():
    """ヘルスチェック"""
    return jsonify({
        "status": "ok",
        "time": datetime.now().isoformat(),
        "active_jobs": sum(1 for j in jobs.values() if j["status"] == "running"),
    })


@app.route("/run", methods=["POST"])
def run():
    """パイプライン実行（非同期）"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400

    site = data.get("site", "sites/aurora_clinic.json")
    category = data.get("category", "")
    title = data.get("title", "")

    # サイト設定ファイルの存在確認
    script_dir = os.path.dirname(os.path.abspath(__file__))
    site_path = os.path.join(script_dir, site) if not os.path.isabs(site) else site
    if not os.path.exists(site_path):
        return jsonify({"error": f"site config not found: {site}"}), 400

    # ジョブ作成
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "keyword": keyword,
        "site": site,
        "category": category,
        "status": "running",
        "started_at": datetime.now().isoformat(),
        "result": None,
    }

    # バックグラウンドで実行
    def execute():
        try:
            result = run_pipeline(
                keyword=keyword,
                site_config=site_path,
                category=category,
                title=title,
            )
            jobs[job_id]["result"] = result
            jobs[job_id]["status"] = result.get("final_status", "unknown")
            jobs[job_id]["finished_at"] = datetime.now().isoformat()
        except Exception as e:
            jobs[job_id]["status"] = "exception"
            jobs[job_id]["error"] = str(e)
            jobs[job_id]["finished_at"] = datetime.now().isoformat()

    thread = threading.Thread(target=execute, daemon=True)
    thread.start()

    return jsonify({
        "job_id": job_id,
        "keyword": keyword,
        "status": "running",
        "message": f"パイプライン開始: {keyword}",
    })


@app.route("/status", methods=["GET"])
def status():
    """ジョブ状態確認"""
    job_id = request.args.get("job_id")

    if job_id:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "job not found"}), 404
        return jsonify(job)

    # 全ジョブ一覧
    return jsonify({
        "jobs": list(jobs.values()),
        "total": len(jobs),
    })


@app.route("/jobs", methods=["GET"])
def list_jobs():
    """全ジョブ一覧"""
    return jsonify({
        "jobs": list(jobs.values()),
        "total": len(jobs),
    })


# ========================================
# メイン
# ========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SEO記事自動生成サーバー")
    parser.add_argument("--port", type=int, default=5001, help="ポート番号（デフォルト: 5001）")
    parser.add_argument("--host", default="0.0.0.0", help="ホスト（デフォルト: 0.0.0.0）")
    args = parser.parse_args()

    print("=" * 50)
    print("SEO記事自動生成サーバー")
    print(f"  http://{args.host}:{args.port}")
    print("=" * 50)
    print()
    print("エンドポイント:")
    print(f"  POST http://localhost:{args.port}/run")
    print(f"  GET  http://localhost:{args.port}/status?job_id=xxx")
    print(f"  GET  http://localhost:{args.port}/health")
    print()

    app.run(host=args.host, port=args.port, debug=False)
