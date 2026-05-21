#!/usr/local/bin/python3.12
"""
パイプライン失敗時の自動修復。

まずは既知エラーをローカルで安全に修復し、それでも判断できない場合のみ
Codex API に限定アクションの提案を依頼する。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from codex_repair_client import suggest_repair
from output_utils import ensure_keyword_output_dir, keyword_to_slug
from pipeline import (
    LOG_DIR,
    STEP_SEQUENCE,
    get_latest_log,
    get_runtime_state_path,
    infer_resume_step,
    save_runtime_state,
    validate_html_output,
    validate_list_box,
    validate_unresolved_facts,
)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable
REPAIR_LOG_DIR = os.path.join(LOG_DIR, "auto_repair")
MAX_CONTEXT_CHARS = 4000


def _artifact_paths(keyword: str) -> dict[str, str]:
    keyword_slug = keyword_to_slug(keyword)
    output_dir = ensure_keyword_output_dir(keyword)
    return {
        "keyword_slug": keyword_slug,
        "output_dir": output_dir,
        "html_path": os.path.join(output_dir, f"{keyword_slug}_記事.html"),
        "tag_structure_path": os.path.join(output_dir, f"{keyword_slug}_タグ構成.md"),
        "urls_path": os.path.join(output_dir, f"{keyword_slug}_urls.json"),
    }


def _next_step(step_key: str | None) -> str | None:
    if not step_key or step_key not in STEP_SEQUENCE:
        return None
    index = STEP_SEQUENCE.index(step_key) + 1
    if index >= len(STEP_SEQUENCE):
        return None
    return STEP_SEQUENCE[index]


def _read_text(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _strip_html_wrappers(html_path: str) -> dict[str, Any]:
    html = _read_text(html_path)
    if not html:
        return {"status": "error", "reason": f"HTMLが見つかりません: {html_path}"}
    if not re.search(r"</?(?:html|body)\b", html, re.IGNORECASE):
        return {"status": "ok", "reason": "html/bodyタグは元からありません"}

    soup = BeautifulSoup(html, "lxml")
    if soup.body is not None:
        cleaned = soup.body.decode_contents().strip()
    else:
        cleaned = re.sub(r"</?(?:html|body)\b[^>]*>", "", html, flags=re.IGNORECASE).strip()
    _write_text(html_path, cleaned)
    return {"status": "ok", "reason": "html/bodyタグを除去しました"}


def _run_command(cmd: list[str], timeout: int = 300) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, (result.stdout or "").strip()
        message = (result.stderr or result.stdout or "").strip()
        return False, message[:1000]
    except Exception as e:
        return False, str(e)


def _refresh_list_box(html_path: str) -> dict[str, Any]:
    ok, message = _run_command([PYTHON, "fill_list_box.py", "--html", html_path], timeout=180)
    if not ok:
        return {"status": "error", "reason": message}
    return {"status": "ok", "reason": message or "一覧ボックスを更新しました"}


def _refresh_sanitize(html_path: str) -> dict[str, Any]:
    ok, message = _run_command([PYTHON, "sanitize_article.py", "--html", html_path], timeout=180)
    if not ok:
        return {"status": "error", "reason": message}
    return {"status": "ok", "reason": message or "sanitize_article を実行しました"}


def _build_context(
    keyword: str,
    pipeline_result: dict[str, Any],
    runtime_state: dict[str, Any] | None,
    latest_log: dict[str, Any] | None,
) -> dict[str, Any]:
    paths = _artifact_paths(keyword)
    html_preview = _read_text(paths["html_path"])[:MAX_CONTEXT_CHARS]
    tag_preview = _read_text(paths["tag_structure_path"])[:MAX_CONTEXT_CHARS]
    return {
        "keyword": keyword,
        "final_status": pipeline_result.get("final_status", ""),
        "validation_error": pipeline_result.get("validation_error", ""),
        "runtime_state": {
            "status": (runtime_state or {}).get("status"),
            "current_step": (runtime_state or {}).get("current_step"),
            "last_completed_step": (runtime_state or {}).get("last_completed_step"),
            "suggested_resume_step": (runtime_state or {}).get("suggested_resume_step"),
            "final_status": (runtime_state or {}).get("final_status"),
            "validation_error": (runtime_state or {}).get("validation_error"),
        },
        "latest_log": {
            "final_status": (latest_log or {}).get("final_status"),
            "validation_error": (latest_log or {}).get("validation_error"),
        },
        "paths": paths,
        "html_preview": html_preview,
        "tag_structure_preview": tag_preview,
    }


def _known_repair_action(
    pipeline_result: dict[str, Any],
    runtime_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    final_status = pipeline_result.get("final_status", "")
    issue = pipeline_result.get("validation_error", "") or ""
    steps = pipeline_result.get("steps", {}) or {}
    last_completed_step = (runtime_state or {}).get("last_completed_step")

    if "html/bodyタグ" in issue or "html/body" in issue:
        return {
            "action": "strip_html_wrappers",
            "resume_step": _next_step(last_completed_step) or infer_resume_step(final_status, issue),
            "reason": "本文断片への html/body 混入を除去します",
            "source": "known_rule",
        }

    if "一覧ボックス" in issue or final_status in {"failed_at_fill_list_box", "list_box_output_invalid"}:
        return {
            "action": "refresh_list_box",
            "resume_step": _next_step("fill_list_box") or "fact_check",
            "reason": "一覧ボックスを再生成してリンク先IDを補完します",
            "source": "known_rule",
        }

    if "※要確認" in issue or "生成指示文" in issue or final_status == "failed_at_sanitize_article":
        return {
            "action": "refresh_sanitize",
            "resume_step": _next_step("sanitize_article") or "fill_reviews",
            "reason": "sanitize_article を再実行して未確定記述を除去します",
            "source": "known_rule",
        }

    images_step = steps.get("images") or {}
    if final_status == "failed_at_images" or images_step.get("status") == "timeout":
        return {
            "action": "set_resume_step",
            "resume_step": "images",
            "reason": "画像生成は途中成果物を再利用して images から再開します",
            "source": "known_rule",
        }

    generate_html_step = steps.get("generate_html") or {}
    if final_status == "failed_at_generate_html" or generate_html_step.get("status") == "timeout":
        return {
            "action": "set_resume_step",
            "resume_step": "generate_html",
            "reason": "本文生成のタイムアウトなので generate_html から再開できるようにします",
            "source": "known_rule",
        }

    return None


def _apply_action(keyword: str, action_data: dict[str, Any]) -> dict[str, Any]:
    paths = _artifact_paths(keyword)
    action = action_data.get("action", "noop")
    resume_step = action_data.get("resume_step")

    if action == "noop":
        return {
            "status": "skipped",
            "repaired": False,
            "resume_step": resume_step,
            "reason": action_data.get("reason", "noop"),
            "source": action_data.get("source", "unknown"),
        }

    if action == "set_resume_step":
        return {
            "status": "ok",
            "repaired": True,
            "resume_step": resume_step,
            "reason": action_data.get("reason", "再開位置のみ更新します"),
            "source": action_data.get("source", "unknown"),
        }

    if action == "strip_html_wrappers":
        result = _strip_html_wrappers(paths["html_path"])
        if result["status"] == "ok":
            validate_html_output(paths["html_path"], paths["keyword_slug"])
        return {
            "status": result["status"],
            "repaired": result["status"] == "ok",
            "resume_step": resume_step,
            "reason": result["reason"],
            "source": action_data.get("source", "unknown"),
        }

    if action == "refresh_list_box":
        result = _refresh_list_box(paths["html_path"])
        if result["status"] == "ok":
            validate_list_box(paths["html_path"])
        return {
            "status": result["status"],
            "repaired": result["status"] == "ok",
            "resume_step": resume_step,
            "reason": result["reason"],
            "source": action_data.get("source", "unknown"),
        }

    if action == "refresh_sanitize":
        result = _refresh_sanitize(paths["html_path"])
        if result["status"] == "ok":
            validate_unresolved_facts(paths["html_path"])
            validate_html_output(paths["html_path"], paths["keyword_slug"])
        return {
            "status": result["status"],
            "repaired": result["status"] == "ok",
            "resume_step": resume_step,
            "reason": result["reason"],
            "source": action_data.get("source", "unknown"),
        }

    return {
        "status": "error",
        "repaired": False,
        "resume_step": None,
        "reason": f"未対応アクションです: {action}",
        "source": action_data.get("source", "unknown"),
    }


def _save_repair_runtime_state(
    keyword: str,
    pipeline_result: dict[str, Any],
    runtime_state: dict[str, Any] | None,
    repair_result: dict[str, Any],
) -> None:
    original = dict(runtime_state or {})
    original.pop("_state_path", None)
    save_runtime_state(
        keyword,
        {
            **original,
            "status": "failed_repaired" if repair_result.get("repaired") else "failed",
            "current_step": None,
            "suggested_resume_step": repair_result.get("resume_step"),
            "final_status": pipeline_result.get("final_status", ""),
            "validation_error": pipeline_result.get("validation_error", ""),
            "last_repair": {
                "at": datetime.now().isoformat(),
                "status": repair_result.get("status"),
                "source": repair_result.get("source"),
                "action": repair_result.get("action"),
                "reason": repair_result.get("reason"),
            },
        },
    )


def _save_repair_log(keyword: str, payload: dict[str, Any]) -> None:
    os.makedirs(REPAIR_LOG_DIR, exist_ok=True)
    keyword_slug = keyword_to_slug(keyword)
    path = os.path.join(REPAIR_LOG_DIR, f"{keyword_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def attempt_auto_repair(
    keyword: str,
    pipeline_result: dict[str, Any],
    runtime_state: dict[str, Any] | None = None,
    latest_log: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if pipeline_result.get("final_status") in {"success", "completed_with_errors"}:
        return {
            "status": "skipped",
            "repaired": False,
            "resume_step": None,
            "reason": "成功ジョブなので修復不要です",
            "source": "auto_repair",
        }

    context = _build_context(keyword, pipeline_result, runtime_state, latest_log)
    action_data = _known_repair_action(pipeline_result, runtime_state)

    if action_data is None:
        action_data = suggest_repair(context)
        if action_data.get("status") != "ok":
            repair_result = {
                "status": "skipped",
                "repaired": False,
                "resume_step": None,
                "reason": action_data.get("reason", "Codex repair unavailable"),
                "source": action_data.get("source", "codex_api"),
                "action": "noop",
            }
            _save_repair_runtime_state(keyword, pipeline_result, runtime_state, repair_result)
            _save_repair_log(keyword, {"context": context, "repair_result": repair_result})
            return repair_result

    repair_result = _apply_action(keyword, action_data)
    repair_result["action"] = action_data.get("action", "noop")
    _save_repair_runtime_state(keyword, pipeline_result, runtime_state, repair_result)
    _save_repair_log(
        keyword,
        {
            "context": context,
            "action_data": action_data,
            "repair_result": repair_result,
        },
    )
    return repair_result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="失敗したジョブの自動修復")
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--result-json", help="pipeline result JSON path")
    args = parser.parse_args()

    runtime_state_path = get_runtime_state_path(args.keyword)
    runtime_state = None
    if os.path.exists(runtime_state_path):
        with open(runtime_state_path, encoding="utf-8") as f:
            runtime_state = json.load(f)
    latest_log = get_latest_log(args.keyword)

    if args.result_json:
        with open(args.result_json, encoding="utf-8") as f:
            pipeline_result = json.load(f)
    elif latest_log:
        pipeline_result = latest_log
    else:
        raise SystemExit("result JSON も latest log も見つかりません")

    result = attempt_auto_repair(args.keyword, pipeline_result, runtime_state, latest_log)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
