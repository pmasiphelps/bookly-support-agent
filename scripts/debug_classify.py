#!/usr/bin/env python3
"""One-off diagnostic: print the raw response classify_intent's own API
call gets back, bypassing its parsing, to debug a classification failure.

Usage:
    python3 scripts/debug_classify.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import anthropic

from app import intent_router

client = anthropic.Anthropic()

response = client.messages.create(
    model=intent_router.ROUTER_MODEL,
    max_tokens=intent_router.MAX_TOKENS,
    system=intent_router._ROUTER_SYSTEM_PROMPT,
    tools=[intent_router._TOOL_SCHEMA],
    tool_choice={"type": "tool", "name": intent_router._TOOL_NAME},
    messages=[{"role": "user", "content": "Where's my order?"}],
)

print("=== response.stop_reason ===")
print(response.stop_reason)

print("\n=== response.content (repr) ===")
print(repr(response.content))

print("\n=== per-block detail ===")
for i, block in enumerate(response.content):
    print(f"--- block {i} ---")
    print("type:", getattr(block, "type", None))
    print("name:", getattr(block, "name", None))
    print("input:", getattr(block, "input", None))
    print("full repr:", repr(block))

print("\n=== model actually used ===")
print("requested:", intent_router.ROUTER_MODEL)
print("response.model:", getattr(response, "model", None))

print("\n=== classify_intent's own result ===")
print(intent_router.classify_intent(client, [], "Where's my order?"))
