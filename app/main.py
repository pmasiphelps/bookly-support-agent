"""FastAPI backend for the Bookly support chat demo."""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv

load_dotenv()  # must run before `agent` (and its Snowflake/Anthropic clients) import

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import Scope

from . import agent

app = FastAPI(title="Bookly Support Agent")


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles with Cache-Control: no-store, so a browser can't keep
    serving a stale app.js after it changes on disk."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", NoCacheStaticFiles(directory="app/static"), name="static")


class ChatRequest(BaseModel):
    session_id: str
    message: str
    # Set when the frontend sends a CLARIFYING_SUGGESTIONS quick-reply
    # button's value -- routes straight to that category via
    # agent.run_turn_for_category. None for an ordinary typed message.
    category: str | None = None


class Suggestion(BaseModel):
    label: str
    value: str
    category: str


class ToolCallLog(BaseModel):
    """One real tool call made while producing this reply -- what the
    frontend's "dev: show tool calls" toggle renders, so it's visible that
    answers come from an actual tool call and not a guess."""

    name: str
    input: dict[str, Any]
    result: Any


class ChatResponse(BaseModel):
    reply: str
    # Populated only when the scope gate comes back "unclear".
    suggestions: list[Suggestion] | None = None
    # Always present; empty for turns that never called a tool.
    tool_calls: list[ToolCallLog] = []


@app.get("/")
def index():
    return FileResponse("app/static/index.html", headers={"Cache-Control": "no-store"})


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session = agent.get_session(req.session_id)
    if req.category:
        result = agent.run_turn_for_category(session, req.message, req.category)
    else:
        result = agent.run_turn(session, req.message)
    return ChatResponse(reply=result.reply, suggestions=result.suggestions, tool_calls=result.tool_calls)


@app.get("/health")
def health():
    return {"status": "ok"}
