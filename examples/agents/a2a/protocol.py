"""A2A Protocol helpers — JSON-RPC 2.0 over HTTP."""
import uuid
from typing import Optional
from pydantic import BaseModel


class TextPart(BaseModel):
    type: str = "text"
    text: str


class Message(BaseModel):
    role: str
    parts: list[TextPart]


class TaskSendParams(BaseModel):
    id: str
    message: Message


class JSONRPCRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: TaskSendParams
    id: str


class TaskStatus(BaseModel):
    state: str


class Artifact(BaseModel):
    parts: list[TextPart]


class TaskResult(BaseModel):
    id: str
    status: TaskStatus
    artifacts: list[Artifact] = []


class JSONRPCResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: str
    result: Optional[TaskResult] = None
    error: Optional[dict] = None


# ── builders ──────────────────────────────────────────────────────────────────

def build_request(text: str, task_id: str | None = None) -> dict:
    return JSONRPCRequest(
        id=str(uuid.uuid4()),
        method="tasks/send",
        params=TaskSendParams(
            id=task_id or str(uuid.uuid4()),
            message=Message(role="user", parts=[TextPart(text=text)]),
        ),
    ).model_dump()


def build_response(task_id: str, rpc_id: str, text: str) -> dict:
    return JSONRPCResponse(
        id=rpc_id,
        result=TaskResult(
            id=task_id,
            status=TaskStatus(state="completed"),
            artifacts=[Artifact(parts=[TextPart(text=text)])],
        ),
    ).model_dump()


def build_error(rpc_id: str, code: int, message: str) -> dict:
    return JSONRPCResponse(
        id=rpc_id,
        error={"code": code, "message": message},
    ).model_dump()


# ── parsers ───────────────────────────────────────────────────────────────────

def extract_text(body: dict) -> str:
    try:
        parts = body["params"]["message"]["parts"]
        return parts[0]["text"] if parts else ""
    except (KeyError, IndexError):
        return ""


def extract_reply_text(response: dict) -> str:
    try:
        artifacts = response["result"]["artifacts"]
        return artifacts[0]["parts"][0]["text"] if artifacts else ""
    except (KeyError, IndexError):
        return ""
