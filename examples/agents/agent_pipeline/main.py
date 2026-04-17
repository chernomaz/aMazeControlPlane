"""
Pipeline Agent — YAML-driven A2A agent
Port : 9010  (override with PORT env var)

Reads config.yaml (or PIPELINE_CONFIG env var) to map incoming A2A message
text to an ordered execution pipeline of tools, agent calls, and LLM calls.

Step type detection (from the step's sub-keys):
  tool call  — sub-key has a "params" map  → calls a registered Python fn
  agent call — sub-key has a "msg" string  → sends A2A message via Envoy
  llm call   — sub-key is literally "llm"  → calls Anthropic Messages API

Template variables in any string value:
  {input}        original incoming text
  {prev_result}  output of the previous step (or {input} for step 0)
  {<step_name>}  output of any earlier named step
"""
import asyncio
import fnmatch
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import litellm
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).parent.parent))
from a2a.protocol import (
    build_error,
    build_request,
    build_response,
    extract_reply_text,
    extract_text,
)

# ── Configuration ─────────────────────────────────────────────────────────────

_CONFIG_PATH = os.environ.get(
    "PIPELINE_CONFIG", str(Path(__file__).parent / "config.yaml")
)

with open(_CONFIG_PATH) as _f:
    _CFG: dict = yaml.safe_load(_f)

AGENT_ID: str = os.environ.get("AGENT_ID", _CFG.get("agent_id", "agent-pipeline"))
PORT: int = int(os.environ.get("PORT", _CFG.get("port", 9010)))
ENVOY_URL: str = os.environ.get("ENVOY_URL", _CFG.get("envoy_url", "http://localhost:10000"))
DEFAULT_MODEL: str = _CFG.get("llm_model", "claude-haiku-4-5-20251001")

_executor = ThreadPoolExecutor(max_workers=4)

# ── Built-in tool registry ────────────────────────────────────────────────────

_TOOLS: dict[str, Any] = {}


def _tool(name: str):
    def decorator(fn):
        _TOOLS[name] = fn
        return fn
    return decorator


@_tool("echo")
def _echo(text: str = "") -> str:
    return str(text)


@_tool("uppercase")
def _uppercase(text: str = "") -> str:
    return str(text).upper()


@_tool("lowercase")
def _lowercase(text: str = "") -> str:
    return str(text).lower()


@_tool("concat")
def _concat(parts: list | None = None, separator: str = " ") -> str:
    return separator.join(str(p) for p in (parts or []))


# ── Template rendering ────────────────────────────────────────────────────────

def _render(value: Any, ctx: dict[str, str]) -> Any:
    """Recursively substitute {key} placeholders using ctx."""
    if isinstance(value, str):
        return value.format_map(ctx)
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    return value


# ── Pipeline matching ─────────────────────────────────────────────────────────

def _find_pipeline(text: str) -> dict | None:
    for pipeline in _CFG.get("pipelines", []):
        pattern = str(pipeline.get("input", ""))
        match_type = pipeline.get("match_type", "glob")

        if match_type == "exact":
            matched = text == pattern
        elif match_type == "regex":
            matched = bool(re.search(pattern, text, re.IGNORECASE))
        else:  # glob
            matched = fnmatch.fnmatch(text.lower(), pattern.lower())

        if matched:
            return pipeline
    return None


# ── Step execution ────────────────────────────────────────────────────────────

def _exec_tool(tool_name: str, step_cfg: dict, ctx: dict) -> str:
    fn = _TOOLS.get(tool_name)
    if fn is None:
        raise ValueError(f"unknown tool: {tool_name!r}")
    params = _render(step_cfg.get("params") or {}, ctx)
    return str(fn(**params))


def _exec_agent(agent_host: str, step_cfg: dict, ctx: dict) -> str:
    msg = _render(step_cfg.get("msg", "{prev_result}"), ctx)
    payload = build_request(msg)
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            ENVOY_URL,
            json=payload,
            headers={"x-agent-id": AGENT_ID, "Host": agent_host},
        )
        resp.raise_for_status()
    return extract_reply_text(resp.json())


def _exec_llm(step_cfg: dict, ctx: dict) -> str:
    query = _render(step_cfg.get("query", "{prev_result}"), ctx)
    model = step_cfg.get("model", DEFAULT_MODEL)
    max_tokens = int(step_cfg.get("max_tokens", 512))
    system = _render(step_cfg.get("system", ""), ctx) or None

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": query})

    # litellm routes to any provider: set OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.
    response = litellm.completion(model=model, messages=messages, max_tokens=max_tokens)
    return response.choices[0].message.content


# ── Pipeline runner ───────────────────────────────────────────────────────────

def _run_pipeline(pipeline: dict, input_text: str) -> str:
    ctx: dict[str, str] = {"input": input_text, "prev_result": input_text}

    for i, step_dict in enumerate(pipeline.get("steps", [])):
        # Each step_dict has exactly one payload key plus an optional "name" key.
        name = step_dict.get("name", f"step_{i}")
        payload_items = {k: v for k, v in step_dict.items() if k != "name"}

        if not payload_items:
            raise ValueError(f"step {i} ({name!r}) has no action key")

        action_key, action_cfg = next(iter(payload_items.items()))
        action_cfg = action_cfg or {}

        if action_key == "llm":
            result = _exec_llm(action_cfg, ctx)
        elif "msg" in action_cfg:
            result = _exec_agent(action_key, action_cfg, ctx)
        else:
            result = _exec_tool(action_key, action_cfg, ctx)

        ctx[name] = result
        ctx["prev_result"] = result
        print(f"[{AGENT_ID}] step {name}: {result[:80]!r}", flush=True)

    return ctx["prev_result"]


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title=f"A2A Pipeline Agent — {AGENT_ID}")


@app.post("/")
async def receive(request: Request):
    body = await request.json()
    rpc_id = body.get("id", str(uuid.uuid4()))
    params = body.get("params", {})
    task_id = params.get("id", str(uuid.uuid4()))

    text = extract_text(body)
    if not text:
        return JSONResponse(build_error(rpc_id, -32600, "empty message"), status_code=400)

    print(f"[{AGENT_ID}] received: {text!r}", flush=True)

    pipeline = _find_pipeline(text)
    if pipeline is None:
        return JSONResponse(build_response(task_id, rpc_id, f"no pipeline matched: {text!r}"))

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(_executor, _run_pipeline, pipeline, text)
    except Exception as exc:
        print(f"[{AGENT_ID}] pipeline error: {exc}", flush=True)
        return JSONResponse(build_error(rpc_id, -32000, str(exc)), status_code=500)

    return JSONResponse(build_response(task_id, rpc_id, result))


@app.get("/health")
def health():
    return {"status": "ok", "agent_id": AGENT_ID, "pipelines": len(_CFG.get("pipelines", []))}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
