"""LLM model registry — read/write LiteLLM model_list in config/litellm.yaml.

The file format mirrors LiteLLM's own `litellm --config` schema:

    model_list:
      - model_name: gpt-4o
        litellm_params:
          model: openai/gpt-4o
          api_key: os.environ/OPENAI_API_KEY
          api_base: https://api.openai.com/v1
      - model_name: claude-opus
        litellm_params:
          model: anthropic/claude-opus-4
          api_key: os.environ/ANTHROPIC_API_KEY

Each `litellm_params.model` is `{provider}/{model}`; we split on the first
slash. If the file is missing → `[]`. Malformed entries are skipped, not
500'd, so a single typo in the YAML doesn't break the GUI.

POST /llms appends a new entry. We round-trip the dict via yaml.safe_load
+ yaml.safe_dump, then atomically replace the file (tempfile in same dir
+ os.replace). Existing entries are preserved by virtue of writing the
whole list back, but YAML comments/ordering in hand-edited files are NOT
preserved — that's an accepted tradeoff per the task spec ("don't use
yaml.safe_dump line-appending").
"""
from __future__ import annotations

import logging
import os
import pathlib
import tempfile
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

router = APIRouter()

CONFIG_DIR = pathlib.Path(os.environ.get("CONFIG_DIR", "/app/config"))


class CreateLLMRequest(BaseModel):
    provider: Literal["openai", "anthropic", "gemini", "mistral", "cohere", "custom"]
    model: str = Field(min_length=1, max_length=256)
    api_key_ref: str = Field(min_length=1, max_length=256)
    base_url: Optional[str] = None

    @field_validator("base_url")
    @classmethod
    def _base_url_is_http(cls, v: Optional[str]) -> Optional[str]:
        # base_url flows into LiteLLM's `api_base`. Reject anything that isn't
        # an http(s) URL with a real host so this endpoint can't be used to
        # redirect a model's traffic to file://, internal-metadata services,
        # or admin sockets.
        if v is None or v == "":
            return None
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be http(s)://host[:port][/path]")
        return v


class LLMEntryResponse(BaseModel):
    provider: str
    model: str
    base_url: Optional[str] = None


@router.get("/llms")
async def list_llms() -> list[dict[str, Any]]:
    path = CONFIG_DIR / "litellm.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        logger.warning("llms: litellm.yaml unreadable: %s", e)
        return []

    model_list = data.get("model_list") or []
    if not isinstance(model_list, list):
        return []

    out: list[dict[str, Any]] = []
    for item in model_list:
        if not isinstance(item, dict):
            continue
        params = item.get("litellm_params") or {}
        if not isinstance(params, dict):
            continue
        full_model = str(params.get("model", ""))
        if "/" in full_model:
            provider, model = full_model.split("/", 1)
        else:
            # Fall back: no provider prefix — treat the model_name's prefix
            # if present, else mark provider unknown.
            provider, model = "unknown", full_model

        entry: dict[str, Any] = {"provider": provider, "model": model}
        api_base = params.get("api_base")
        if api_base:
            entry["base_url"] = api_base
        out.append(entry)

    return out


@router.post("/llms", status_code=201, response_model=LLMEntryResponse)
async def create_llm(req: CreateLLMRequest) -> LLMEntryResponse:
    """Append one model entry to config/litellm.yaml.

    Edge cases:
      - file missing               → create with one-element model_list
      - file empty / no model_list → initialise model_list and append
      - duplicate model_name       → 409
      - parse error on existing    → 500 (don't silently overwrite)
    """
    path = CONFIG_DIR / "litellm.yaml"

    # --- load existing (or start fresh) ----------------------------------
    if path.exists():
        try:
            raw = path.read_text()
        except OSError as e:
            logger.error("llms: cannot read litellm.yaml: %s", e)
            raise HTTPException(status_code=500, detail="litellm-yaml-read-error") from e
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            logger.error("llms: litellm.yaml parse error: %s", e)
            raise HTTPException(status_code=500, detail=f"litellm-yaml-parse-error: {e}") from e
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="litellm-yaml-not-mapping")
    else:
        data = {}

    model_list = data.get("model_list")
    if not isinstance(model_list, list):
        model_list = []

    # --- duplicate check on model_name -----------------------------------
    for item in model_list:
        if isinstance(item, dict) and item.get("model_name") == req.model:
            raise HTTPException(status_code=409, detail="model-name-exists")

    # --- build new entry --------------------------------------------------
    litellm_params: dict[str, Any] = {
        "model": f"{req.provider}/{req.model}",
        "api_key": f"os.environ/{req.api_key_ref}",
    }
    if req.base_url:
        litellm_params["api_base"] = req.base_url

    new_entry: dict[str, Any] = {
        "model_name": req.model,
        "litellm_params": litellm_params,
    }
    model_list.append(new_entry)
    data["model_list"] = model_list

    # --- atomic write: tempfile in same dir + os.replace -----------------
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile in the same dir guarantees os.replace is atomic
        # across the rename (same filesystem).
        fd, tmp_path = tempfile.mkstemp(
            prefix=".litellm.yaml.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
            os.replace(tmp_path, path)
        except Exception:
            # best-effort cleanup of the temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.error("llms: cannot write litellm.yaml: %s", e)
        raise HTTPException(status_code=500, detail="litellm-yaml-write-error") from e

    logger.info("llms: registered model_name=%s provider=%s", req.model, req.provider)
    return LLMEntryResponse(
        provider=req.provider, model=req.model, base_url=req.base_url
    )
