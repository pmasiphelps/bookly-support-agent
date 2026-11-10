"""FastAPI backend for the Bookly support chat demo."""

from __future__ import annotations

from typing import Any

from .config import load_environment

load_environment()  # must run before `agent` and its clients import

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import Scope

from . import agent
from .studio import deploy as studio_deploy
from .studio.api import router as studio_router
from .studio import watchtower as studio_watchtower
from .studio.evals import SEED_CASES
from .studio.runtime import execute_turn, production_config_for
from .studio.store import get_store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # First boot: code defaults become v1 in production + starter tests.
    def bootstrap() -> None:
        studio_deploy.ensure_initialized(SEED_CASES)
        studio_watchtower.ensure_seeded()

    bootstrap()
    get_store().on_recreate = bootstrap
    yield


app = FastAPI(title="Bookly Support Agent", lifespan=lifespan)
app.include_router(studio_router)


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


@app.get("/studio")
def studio():
    return FileResponse("app/static/studio/index.html", headers={"Cache-Control": "no-store"})


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    # A new conversation is pinned to the deployed version (or the canary,
    # for its slice of traffic); every turn is logged for Monitor.
    session = agent.get_session(req.session_id, config_factory=production_config_for)
    result, _trace = execute_turn(session, req.message, env="production", category=req.category)
    return ChatResponse(reply=result.reply, suggestions=result.suggestions, tool_calls=result.tool_calls)


class FeedbackRequest(BaseModel):
    session_id: str
    rating: int  # 1 = thumbs up, -1 = thumbs down


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    if req.rating not in (1, -1):
        raise HTTPException(400, "rating must be 1 or -1")
    if not get_store().set_csat(req.session_id, req.rating):
        raise HTTPException(404, "No conversation to rate yet")
    return {"ok": True}


@app.get("/health")
def health():
    return {"status": "ok"}
