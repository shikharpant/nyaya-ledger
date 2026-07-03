"""Small OpenAI-compatible client for the local OMLX endpoint."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = "qwopus3.6-27b-v2-mtp"


def _dotenv_values(path: str = ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    dotenv = os.path.abspath(path)
    if not os.path.exists(dotenv):
        return values
    with open(dotenv, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class OmlxError(RuntimeError):
    """Base class for OMLX client failures."""

    reason = "llm_unavailable"


class OmlxAuthMissing(OmlxError):
    reason = "llm_auth_missing"


class OmlxAuthError(OmlxError):
    reason = "llm_auth_invalid"


class OmlxModelUnavailable(OmlxError):
    reason = "llm_model_unavailable"


class OmlxSchemaError(OmlxError):
    reason = "llm_schema_invalid"


@dataclass(frozen=True)
class OmlxConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key_env: str = "OMLX_API_KEY"
    timeout: int = 60

    @classmethod
    def from_env(
        cls,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key_env: str = "OMLX_API_KEY",
        timeout: int = 60,
    ) -> "OmlxConfig":
        dotenv = _dotenv_values()
        return cls(
            base_url=base_url or os.getenv("OMLX_BASE_URL") or dotenv.get("OMLX_BASE_URL", DEFAULT_BASE_URL),
            model=model or os.getenv("OMLX_MODEL") or dotenv.get("OMLX_MODEL", DEFAULT_MODEL),
            api_key_env=api_key_env,
            timeout=timeout,
        )

    @property
    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "") or _dotenv_values().get(self.api_key_env, "")


def _post_json(url: str, payload: dict[str, Any], config: OmlxConfig) -> dict[str, Any]:
    api_key = config.api_key
    if not api_key:
        raise OmlxAuthMissing(f"{config.api_key_env} is not set")
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {401, 403}:
            raise OmlxAuthError(body) from exc
        if exc.code == 404 or "Model" in body and "not found" in body:
            raise OmlxModelUnavailable(body) from exc
        raise OmlxError(body) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OmlxError(str(exc)) from exc


def _get_json(url: str, config: OmlxConfig) -> dict[str, Any]:
    api_key = config.api_key
    if not api_key:
        raise OmlxAuthMissing(f"{config.api_key_env} is not set")
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {401, 403}:
            raise OmlxAuthError(body) from exc
        raise OmlxError(body) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OmlxError(str(exc)) from exc


def chat_json(
    prompt: str,
    *,
    system: str = "You are a strict JSON API. Output exactly one compact JSON object and no prose.",
    config: OmlxConfig | None = None,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Call OMLX and parse a single JSON object from the response content."""

    cfg = config or OmlxConfig.from_env()
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "/no_think " + prompt.lstrip()},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    response = _post_json(cfg.base_url.rstrip("/") + "/chat/completions", payload, cfg)
    choices = response.get("choices") or []
    content = ""
    if choices:
        content = str((choices[0].get("message") or {}).get("content") or "")
    try:
        parsed = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise OmlxSchemaError(content[:500]) from exc
    if not isinstance(parsed, dict):
        raise OmlxSchemaError("OMLX response was not a JSON object")
    return parsed


def smoke_test(config: OmlxConfig | None = None) -> dict[str, Any]:
    cfg = config or OmlxConfig.from_env()
    models = _get_json(cfg.base_url.rstrip("/") + "/models", cfg)
    model_ids = [str(item.get("id")) for item in models.get("data", []) if item.get("id")]
    completion = chat_json(
        'Return exactly this object with no other text: {"ok":true,"operation":"SPLICE"}',
        config=cfg,
        max_tokens=96,
    )
    return {
        "ok": completion.get("ok") is True and completion.get("operation") == "SPLICE",
        "base_url": cfg.base_url,
        "model": cfg.model,
        "models": model_ids,
        "completion": completion,
    }


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "OmlxAuthError",
    "OmlxAuthMissing",
    "OmlxConfig",
    "OmlxError",
    "OmlxModelUnavailable",
    "OmlxSchemaError",
    "chat_json",
    "smoke_test",
]
