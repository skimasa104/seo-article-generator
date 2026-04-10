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
from collections import deque
from datetime import datetime

from flask import Flask, request, jsonify
from auto_repair import attempt_auto_repair
from env_utils import load_project_env

# パイプラインのインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import (
    run_pipeline,
    get_latest_log,
    infer_resume_step_from_log,
    load_runtime_state,
    infer_resume_step_from_runtime_state,
)

load_project_env()

app = Flask(__name__)

# ジョブ管理（メモリ上）
jobs = {}
job_queue = deque()
jobs_lock = threading.Lock()
worker_started = False
MAX_AUTO_REPAIR_ATTEMPTS = 3


def count_running_jobs() -> int:
    return sum(1 for j in jobs.values() if j.get("phase") == "executing")


def count_queued_jobs() -> int:
    return sum(1 for j in jobs.values() if j.get("phase") == "queued")


def start_worker_once():
    global worker_started
    if worker_started:
        return
    worker_started = True

    def worker():
        while True:
            job_id = None
            with jobs_lock:
                if job_queue:
                    job_id = job_queue.popleft()
                    job = jobs.get(job_id)
                    if job:
                        job["phase"] = "executing"
                        job["message"] = f"{job.get('resume_step')} から再開中" if job.get("resume_step") else "実行中"
                        job["started_execution_at"] = datetime.now().isoformat()
            if not job_id:
                threading.Event().wait(1)
                continue

            job = jobs.get(job_id)
            if not job:
                continue

            try:
                repair_history = []
                current_resume_step = job.get("resume_step")
                latest_result = None

                for repair_attempt in range(1, MAX_AUTO_REPAIR_ATTEMPTS + 1):
                    with jobs_lock:
                        job["resume_step"] = current_resume_step
                        job["message"] = (
                            f"{current_resume_step} から再開中"
                            if current_resume_step
                            else "実行中"
                        )

                    latest_result = run_pipeline(
                        keyword=job["keyword"],
                        site_config=job["site_path"],
                        genre_id=job["genre"],
                        category=job["category"],
                        title=job["title"],
                        start_step=current_resume_step,
                    )

                    final_status = latest_result.get("final_status", "unknown")
                    if final_status in {"success", "completed_with_errors"}:
                        break

                    repair_result = attempt_auto_repair(
                        job["keyword"],
                        latest_result,
                        load_runtime_state(job["keyword"]),
                        get_latest_log(job["keyword"]),
                    )
                    repair_history.append(repair_result)

                    if not repair_result.get("repaired") or not repair_result.get("resume_step"):
                        break

                    current_resume_step = repair_result["resume_step"]
                    with jobs_lock:
                        job["message"] = f"自動修復: {repair_result.get('reason', '')}"

                result = latest_result or {"final_status": "unknown"}
                if repair_history:
                    result["auto_repair_history"] = repair_history

                with jobs_lock:
                    job["result"] = result
                    job["status"] = result.get("final_status", "unknown")
                    job["phase"] = "finished"
                    job["message"] = "完了" if job["status"] == "success" else f"失敗: {job['status']}"
                    job["finished_at"] = datetime.now().isoformat()
            except Exception as e:
                with jobs_lock:
                    job["status"] = "exception"
                    job["phase"] = "finished"
                    job["error"] = str(e)
                    job["message"] = f"例外: {e}"
                    job["finished_at"] = datetime.now().isoformat()

    threading.Thread(target=worker, daemon=True).start()


# ========================================
# エンドポイント
# ========================================
@app.route("/health", methods=["GET"])
def health():
    """ヘルスチェック"""
    return jsonify({
        "status": "ok",
        "time": datetime.now().isoformat(),
        "active_jobs": count_running_jobs(),
        "queued_jobs": count_queued_jobs(),
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

    genre = data.get("genre", "").strip()
    if not genre:
        return jsonify({"error": "genre is required"}), 400

    site = data.get("site", "sites/aurora_clinic.json")
    category = data.get("category", "")
    title = data.get("title", "")
    resume = data.get("resume", True)

    # サイト設定ファイルの存在確認
    script_dir = os.path.dirname(os.path.abspath(__file__))
    site_path = os.path.join(script_dir, site) if not os.path.isabs(site) else site
    if not os.path.exists(site_path):
        return jsonify({"error": f"site config not found: {site}"}), 400

    # ジョブ作成
    job_id = str(uuid.uuid4())[:8]
    latest_log = get_latest_log(keyword) if resume else None
    runtime_state = load_runtime_state(keyword) if resume else None
    resume_step = None
    resume_source = None
    if resume:
        resume_step = infer_resume_step_from_runtime_state(runtime_state)
        if resume_step:
            resume_source = runtime_state.get("_state_path")
        else:
            resume_step = infer_resume_step_from_log(latest_log)
            if resume_step:
                resume_source = latest_log.get("_log_path")
    start_worker_once()
    with jobs_lock:
        job_queue.append(job_id)
        queue_position = len(job_queue)
        jobs[job_id] = {
            "id": job_id,
            "keyword": keyword,
            "genre": genre,
            "site": site,
            "site_path": site_path,
            "category": category,
            "title": title,
            "status": "running",
            "phase": "queued",
            "started_at": datetime.now().isoformat(),
            "resume_step": resume_step,
            "resume_source": resume_source,
            "result": None,
            "queue_position": queue_position,
            "message": f"待機中: キュー{queue_position}番目",
        }

    return jsonify({
        "job_id": job_id,
        "keyword": keyword,
        "status": "running",
        "phase": "queued",
        "resume_step": resume_step,
        "queue_position": queue_position,
        "message": f"待機中: キュー{queue_position}番目" if queue_position > 1 else (f"{resume_step} から再開待機" if resume_step else f"実行待機: {keyword}"),
    })


@app.route("/status", methods=["GET"])
def status():
    """ジョブ状態確認"""
    job_id = request.args.get("job_id")

    if job_id:
        with jobs_lock:
            job = jobs.get(job_id)
            if job and job.get("phase") == "queued":
                try:
                    job["queue_position"] = list(job_queue).index(job_id) + 1
                except ValueError:
                    job["queue_position"] = 0
                if job["queue_position"] > 0:
                    job["message"] = f"待機中: キュー{job['queue_position']}番目"
        if not job:
            return jsonify({"error": "job not found"}), 404
        return jsonify(job)

    # 全ジョブ一覧
    with jobs_lock:
        queue_snapshot = list(job_queue)
        job_list = []
        for job in jobs.values():
            if job.get("phase") == "queued":
                try:
                    job["queue_position"] = queue_snapshot.index(job["id"]) + 1
                except ValueError:
                    job["queue_position"] = 0
            job_list.append(job)
    return jsonify({
        "jobs": job_list,
        "total": len(jobs),
    })


@app.route("/jobs", methods=["GET"])
def list_jobs():
    """全ジョブ一覧"""
    with jobs_lock:
        queue_snapshot = list(job_queue)
        job_list = []
        for job in jobs.values():
            if job.get("phase") == "queued":
                try:
                    job["queue_position"] = queue_snapshot.index(job["id"]) + 1
                except ValueError:
                    job["queue_position"] = 0
            job_list.append(job)
    return jsonify({
        "jobs": job_list,
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
