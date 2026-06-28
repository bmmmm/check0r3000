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
it to JSON). Only the `claude` CLI reports a real cost_usd; every OpenAI-compatible
backend reports cost_usd = None (the API returns no price), so a billed `openai:` spec
shows as unpriced too — read cost as claude-only.

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


def _env_files() -> list[str]:
    """`.env` files consulted as a key fallback, most-specific first. Override the
    first candidate with CHECK0R_ENV_FILE."""
    files = []
    override = os.environ.get("CHECK0R_ENV_FILE")
    if override:
        files.append(os.path.expanduser(override))
    files.append(os.path.join(os.getcwd(), ".env"))
    files.append(os.path.expanduser("~/.env"))
    return files


def _parse_env_file(path: str) -> dict[str, str]:
    """Minimal stdlib .env reader: KEY=VALUE lines, with `export ` prefix and a
    matching pair of surrounding quotes stripped. Returns {} if unreadable. Values
    are kept in-process only and never logged."""
    out: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError:
        return out
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            # Unquoted value: drop a trailing inline comment (` #…`) like python-dotenv
            # does, so `KEY=abc  # note` yields 'abc', not a silently mangled key that
            # 401s. Only ` #` (space-hash) splits; a '#' inside the value survives.
            hash_at = val.find(" #")
            if hash_at != -1:
                val = val[:hash_at].rstrip()
        if key and val:
            out[key] = val
    return out


def _api_key(provider: str) -> str | None:
    """Bearer token for a key-gated OpenAI-compatible backend. The provider-specific
    name (<PROVIDER>_API_KEY, e.g. OMLX_API_KEY) is fully resolved — environment THEN
    `.env` (project, then ~/.env) — before falling back to the generic OPENAI_API_KEY.
    Resolving per-name across both sources matters: a globally-exported OPENAI_API_KEY
    must NOT shadow a provider-specific key the user put in `.env` and then get sent as
    a Bearer to whatever (possibly remote) endpoint the spec names. Never hardcoded,
    never logged. Servers needing no key work without one (no header is sent)."""
    maps = None  # parse the .env files lazily — only if the environment misses
    for var in (f"{provider.upper()}_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(var)
        if v:
            return v
        if maps is None:
            maps = [_parse_env_file(p) for p in _env_files()]
        for m in maps:          # project .env beats ~/.env
            if m.get(var):
                return m[var]
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
    model = model.strip()                # stray spaces must not reach the argv/request
    if endpoint is not None:
        endpoint = endpoint.strip() or None
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
    # Guard the CONTAINER too, not just the inner value: a non-dict modelUsage has no
    # .values(), and a string duration_ms breaks the division — either would escape
    # run() unhandled and crash the caller, defeating the uniform-dict contract _err
    # upholds. Coerce both defensively.
    mv = outer.get("modelUsage")
    mu = next(iter(mv.values()), {}) if isinstance(mv, dict) else {}
    mu = mu if isinstance(mu, dict) else {}
    dur = outer.get("duration_ms")
    res = {
        "text": outer.get("result"),
        "error": None,
        "cost_usd": outer.get("total_cost_usd"),
        "wall_s": round(wall, 1),
        "api_s": round(dur / 1000, 1) if isinstance(dur, (int, float)) else None,
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


def prewarm(spec: str, timeout: int = 1200) -> dict:
    """Load a local model into the server's RAM ahead of real use by firing a
    minimal (max_tokens=1) completion. The FIRST such call pays the one-time
    cold-load — minutes for a large local model — so the next real extraction
    runs warm instead of eating that latency in the critical path. For the
    `claude` provider this is a no-op (no resident model to warm). Returns
    {ok: bool, wall_s: float, error: str|None}. Kept self-contained (not folded
    into _run_openai) so warming never disturbs the proven extraction path."""
    provider, model, endpoint = parse_spec(spec)
    if provider == "claude":
        return {"ok": True, "wall_s": 0.0, "error": None}
    endpoint = endpoint or DEFAULT_ENDPOINTS.get(provider)
    if not endpoint:
        return {"ok": False, "wall_s": 0.0,
                "error": f"no endpoint for provider '{provider}'"}
    if not model:
        return {"ok": False, "wall_s": 0.0,
                "error": f"provider '{provider}' needs a model name"}
    url = endpoint.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "temperature": 0,
        "max_tokens": 1,
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
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
        hint = (f" — set {provider.upper()}_API_KEY (or OPENAI_API_KEY)"
                if e.code == 401 else "")
        return {"ok": False, "wall_s": round(time.monotonic() - t0, 1),
                "error": f"{provider} prewarm HTTP {e.code} ({url}): {detail}{hint}"}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "wall_s": round(time.monotonic() - t0, 1),
                "error": f"{provider} prewarm failed ({url}): {e}"}
    return {"ok": True, "wall_s": round(time.monotonic() - t0, 1), "error": None}


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
        # The OpenAI /chat/completions response carries no price, so cost is UNKNOWN
        # for every backend on this path — local (free) AND a billed cloud `openai:`
        # spec alike. Reported as None (shown as "-" = unpriced/unknown, never "$0").
        # Only the `claude` CLI surfaces a real total_cost_usd.
        "error": None,
        "cost_usd": None,
        "wall_s": round(wall, 1),
        "api_s": round(wall, 1),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "context_window": None,
    }
