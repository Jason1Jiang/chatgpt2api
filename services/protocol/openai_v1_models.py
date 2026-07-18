from __future__ import annotations

from typing import Any

from services.account_service import account_service
from services.gemini_web_backend import GEMINI_WEB_IMAGE_MODEL
from services.openai_backend_api import OpenAIBackendAPI
from utils.helper import CODEX_IMAGE_MODEL


def list_models() -> dict[str, Any]:
    backend = OpenAIBackendAPI()
    try:
        result = backend.list_models()
    finally:
        backend.close()
    data = result.get("data")
    if not isinstance(data, list):
        return result
    dynamic_models: set[str] = set()
    accounts = account_service.list_accounts()
    web_image_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
           and str(account.get("provider") or "").strip().lower() != "gemini_web"
    ]
    gemini_web_accounts = [
        account
        for account in accounts
        if isinstance(account, dict)
           and str(account.get("provider") or "").strip().lower() == "gemini_web"
           and str(account.get("status") or "").strip() == "正常"
    ]
    if not gemini_web_accounts:
        data[:] = [
            item
            for item in data
            if not (
                isinstance(item, dict)
                and str(item.get("id") or "").strip() == GEMINI_WEB_IMAGE_MODEL
            )
        ]
    seen = {str(item.get("id") or "").strip() for item in data if isinstance(item, dict)}
    codex_types = {
        normalized
        for account in accounts
        if isinstance(account, dict)
           and account_service._normalize_source_type(account.get("source_type")) == "codex"
           and (normalized := account_service._normalize_account_type(account.get("type")))
    }

    if web_image_accounts:
        dynamic_models.add("gpt-image-2")
    if gemini_web_accounts:
        dynamic_models.add(GEMINI_WEB_IMAGE_MODEL)
    if codex_types & {"Plus", "Team", "Pro"}:
        dynamic_models.add(CODEX_IMAGE_MODEL)
    if "Plus" in codex_types:
        dynamic_models.add(f"plus-{CODEX_IMAGE_MODEL}")
    if "Team" in codex_types:
        dynamic_models.add(f"team-{CODEX_IMAGE_MODEL}")
    if "Pro" in codex_types:
        dynamic_models.add(f"pro-{CODEX_IMAGE_MODEL}")

    for model in sorted(dynamic_models):
        if model not in seen:
            data.append({
                "id": model,
                "object": "model",
                "created": 0,
                "owned_by": "chatgpt2api",
                "permission": [],
                "root": model,
                "parent": None,
            })
    return result
