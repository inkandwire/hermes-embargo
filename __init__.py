"""Embargo: embargoed news stays embargoed until its release time. Registration only; the logic lives in embargo.py."""
from __future__ import annotations

import json
from pathlib import Path

from . import embargo


def _schema(name, desc, props, required):
    return {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": required}}


def _tool(fn):
    return lambda args, **kw: json.dumps(fn(args or {}, kw), ensure_ascii=False)


def register(ctx):
    try:
        embargo.init(Path(ctx.state.data_dir))
    except Exception:  # state facade unavailable (validator probe): fall back to the default data dir
        pass
    tools = [
        ("embargo_add", "Hold text until a release time. Use when the user says something is embargoed. It can only add holds, never lift one.",
         {"text": {"type": "string", "description": "The text to hold"},
          "until": {"type": "string", "description": "Release time: ISO 8601 with offset, '09:00 MYT, 12 November 2026', or +90s"},
          "label": {"type": "string", "description": "Short name (optional)"}}, ["text", "until"], embargo.tool_add),
        ("embargo_status", "List held and lifted embargoes: id, label, release time, seconds left.", {}, [], embargo.tool_status),
    ]
    for name, desc, props, req, fn in tools:
        ctx.register_tool(name=name, toolset="embargo", schema=_schema(name, desc, props, req), handler=_tool(fn))
    ctx.register_command("embargo", handler=embargo.slash, description="Manage held (embargoed) text",
                         args_hint="list | add \"<text>\" until <time> | set <id> <time> | lift <id> | clear-lifted")
    ctx.register_hook("pre_tool_call", embargo.guard)
    ctx.register_hook("post_tool_call", embargo.observe)
    ctx.register_hook("pre_llm_call", embargo.on_turn)
    ctx.register_hook("transform_llm_output", embargo.card_hook)
