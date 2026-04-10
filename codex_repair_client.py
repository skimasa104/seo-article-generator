#!/usr/local/bin/python3.12
"""
OpenAI Responses API を使って、未知エラーの修復方針を提案させる。
最初の版では、安全な限定アクションだけを返させる。
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_REPAIR_MODEL = os.environ.get("OPENAI_REPAIR_MODEL", "gpt-5-mini")


def _extract_output_text(data: dict[str, Any]) -> str:
    output = data.get("output") or []
    texts: list[str] = []
    for item in output:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    return "\n".join(texts).strip()


def suggest_repair(context: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {
            "status": "skipped",
            "reason": "OPENAI_API_KEY is not set",
            "source": "codex_api",
        }

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "noop",
                    "set_resume_step",
                    "strip_html_wrappers",
                    "refresh_list_box",
                    "refresh_sanitize",
                ],
            },
            "resume_step": {
                "type": ["string", "null"],
                "enum": [
                    "search",
                    "scrape",
                    "tag_structure",
                    "generate_html",
                    "fill_list_box",
                    "fact_check",
                    "sanitize_article",
                    "fill_reviews",
                    "fill_final_cta",
                    "fill_maps",
                    "screenshots",
                    "images",
                    "wordpress",
                    None,
                ],
            },
            "reason": {"type": "string"},
        },
        "required": ["action", "resume_step", "reason"],
    }

    system_prompt = (
        "あなたはSEO記事生成パイプラインの自動修復エージェントです。"
        "危険なコード編集は行わず、許可された限定アクションの中から最も安全な修復方針を1つだけ返してください。"
        "不明なら noop を返してください。"
    )
    user_prompt = (
        "以下の失敗コンテキストを読んで、もっとも安全な修復アクションを1つ選んでください。\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )

    body = {
        "model": DEFAULT_OPENAI_REPAIR_MODEL,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "repair_action",
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": 300,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers=headers,
            json=body,
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        text = _extract_output_text(payload)
        if not text:
            return {
                "status": "error",
                "reason": "OpenAI response text is empty",
                "source": "codex_api",
            }
        data = json.loads(text)
        data["status"] = "ok"
        data["source"] = "codex_api"
        return data
    except Exception as e:
        return {
            "status": "error",
            "reason": str(e),
            "source": "codex_api",
        }
