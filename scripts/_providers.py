"""Pluggable model backends for the extraction prompt.

A model is named by a spec string:  [provider:]model[@endpoint]

    haiku                    -> Anthropic `claude` CLI, model haiku
    claude:opus              -> `claude` CLI, model opus
    claude                   -> `claude` CLI, default model
    ollama:llama3.1:8b       -> OpenAI-compatible HTTP at the ollama default endpoint
    ollama:qwen2.5@http://box:11434/v1
    mlx:<model-id>           -> OpenAI-compatible HTTP at the mlx/oMLX default endpoint
    openai:<model>@http://host:port/v1   -> any OpenAI-compatible server

This keeps cloud (claude) and local (Ollama, oMLX / mlx_lm.server, vLLM, ...) models
on one comparable footing so the benchmark can pit them against each other.

Every backend returns the SAME dict shape:
    {text, error, cost_usd, wall_s, api_s, input_tokens, output_tokens, context_window}
`text` is the raw model reply (possibly fenced / prose-wrapped — the caller coerces
it to JSON). Local backends report cost_usd = None (free / unpriced).

stdlib only: the OpenAI-compatible path uses urllib, no requests/httpx.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

# Provider alias -> default OpenAI-compatible base URL (override with @endpoint).
DEFAULT_ENDPOINTS = {
    "ollama": "http://localhost:11434/v1",
    "mlx": "http://127.0.0.1:8080/v1",   # mlx_lm.server default
    "omlx": "http://127.0.0.1:8000/v1",  # oMLX default (key-gated; see _api_key)
    "vllm": "http://localhost:8000/v1",
}


def _api_key(provider: str) -> str | None:
    """Bearer token for a key-gated OpenAI-compatible backend, from the environment
    only. Checked in order: <PROVIDER>_API_KEY (e.g. OMLX_API_KEY), then
    OPENAI_API_KEY. Never hardcoded, never logged. Servers needing no key work
    without one (no header is sent)."""
    for var in (f"{provider.upper()}_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(var)
        if v:
            return v
    return None


def parse_spec(spec: str) -> tuple[str, str | None, str | None]:
    """('provider', 'model'|None, 'endpoint'|None). Bare name => claude."""
    endpoint = None
    if "@" in spec:
        spec, endpoint = spec.split("@", 1)
    if ":" in spec:
        provider, model = spec.split(":", 1)
    else:
        provider, model = "claude", spec
    provider = provider.strip().lower()  # 'Claude:Opus' / 'OLLAMA:…' route the same
    if provider == "claude" and model.lower() in ("", "claude", "default"):
        model = None
    return provider, model, endpoint


def _err(msg: str, wall: float) -> dict:
    return {"text": None, "error": msg, "cost_usd": None, "wall_s": round(wall, 1),
            "api_s": None, "input_tokens": None, "output_tokens": None,
            "context_window": None}


def run(spec: str, instruction: str, payload: str, timeout: int = 1200) -> dict:
    provider, model, endpoint = parse_spec(spec)
    if provider == "claude":
        return _run_claude(model, instruction, payload, timeout)
    return _run_openai(provider, model, endpoint, instruction, payload, timeout)


def _run_claude(model: str | None, instruction: str, payload: str, timeout: int) -> dict:
    cmd = ["claude", "-p", instruction, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, input=payload, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _err(f"claude timeout after {timeout}s", time.monotonic() - t0)
    wall = time.monotonic() - t0
    if proc.returncode != 0:
        return _err((proc.stderr or proc.stdout).strip()[:300] or f"exit {proc.returncode}", wall)
    try:
        outer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _err("claude output not JSON", wall)
    if not isinstance(outer, dict):
        return _err("claude output not a JSON object", wall)

    usage = outer.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    mu = next(iter((outer.get("modelUsage") or {}).values()), {})
    mu = mu if isinstance(mu, dict) else {}
    res = {
        "text": outer.get("result"),
        "error": None,
        "cost_usd": outer.get("total_cost_usd"),
        "wall_s": round(wall, 1),
        "api_s": round((outer.get("duration_ms") or 0) / 1000, 1),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "context_window": mu.get("contextWindow"),
    }
    if outer.get("is_error"):
        detail = outer.get("api_error_status") or outer.get("result")
        res["error"] = (str(detail)[:300] if detail else
                        "claude reported is_error with no detail "
                        "(empty api_error_status/result)")
        res["text"] = None
    return res


def _run_openai(provider: str, model: str | None, endpoint: str | None,
                instruction: str, payload: str, timeout: int) -> dict:
    endpoint = endpoint or DEFAULT_ENDPOINTS.get(provider)
    if not endpoint:
        return _err(f"no endpoint for provider '{provider}' "
                    f"(use {provider}:model@http://host:port/v1)", 0.0)
    if not model:
        return _err(f"provider '{provider}' needs a model name", 0.0)
    url = endpoint.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": payload},
        ],
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = _api_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
        hint = (f" — set {provider.upper()}_API_KEY (or OPENAI_API_KEY)"
                if e.code == 401 else "")
        return _err(f"{provider} HTTP {e.code} ({url}): {detail}{hint}",
                    time.monotonic() - t0)
    except (urllib.error.URLError, TimeoutError) as e:
        return _err(f"{provider} request failed ({url}): {e}", time.monotonic() - t0)
    except json.JSONDecodeError:
        return _err(f"{provider} returned non-JSON ({url})", time.monotonic() - t0)
    wall = time.monotonic() - t0
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return _err(f"{provider} response missing choices: {str(data)[:200]}", wall)
    try:
        text = choice["message"]["content"]
    except (KeyError, TypeError):
        return _err(f"{provider} response missing message content: {str(choice)[:200]}", wall)
    usage = data.get("usage") or {}
    return {
        "text": text,
        "error": None,
        "cost_usd": None,  # local / unpriced
        "wall_s": round(wall, 1),
        "api_s": round(wall, 1),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "context_window": None,
    }
