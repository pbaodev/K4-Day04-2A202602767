"""Streamlit live chat for the IT Helpdesk Agent.

Reuses run_model_tool_loop from chat.py so the UI and the CLI share one agent loop.
Every turn is appended to the same transcript file used by the CLI.

Run from starter_v0/:  streamlit run app.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from chat import (
    ARTIFACTS_DIR,
    ROOT,
    now_iso,
    run_model_tool_loop,
    safe_slug,
    trim_history,
    write_transcript,
)
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version

PROVIDERS = ["groq", "openrouter", "openai", "anthropic", "gemini", "9router"]
SYSTEM_PROMPT_PATH = ARTIFACTS_DIR / "system_prompt.md"
TOOLS_PATH = ARTIFACTS_DIR / "tools.yaml"


def new_transcript(version: str, provider_name: str, model: str | None) -> tuple[dict[str, Any], Path]:
    artifact_version = build_artifact_version(version, SYSTEM_PROMPT_PATH, TOOLS_PATH)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    transcript_id = "_".join([safe_slug(version), safe_slug(provider_name), "ui", timestamp])
    transcript = {
        "transcript_id": transcript_id,
        **artifact_version_dict(artifact_version),
        "provider": provider_name,
        "model": model,
        "interface": "streamlit",
        "system_prompt": str(SYSTEM_PROMPT_PATH),
        "tools": str(TOOLS_PATH),
        "history_window": 5,
        "max_tool_rounds": 4,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "turns": [],
    }
    return transcript, ROOT / "transcripts" / f"{transcript_id}.transcript.json"


def render_tool_event(event: dict[str, Any]) -> None:
    """Show one tool call: name, args and result — errors highlighted."""
    result = event.get("result", {})
    is_error = isinstance(result, dict) and "error" in result
    icon = "🚫" if is_error else "🔧"
    with st.expander(f"{icon} {event.get('tool')}", expanded=is_error):
        st.caption("Arguments")
        st.code(json.dumps(event.get("args", {}), ensure_ascii=False, indent=2), language="json")
        st.caption("Result")
        if is_error:
            st.error(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            st.code(json.dumps(result, ensure_ascii=False, indent=2), language="json")


def render_turn(turn: dict[str, Any]) -> None:
    with st.chat_message("user"):
        st.write(turn["user"])
    with st.chat_message("assistant"):
        if turn["status"] == "provider_error":
            st.error(turn.get("error", "provider error"))
            return
        for round_record in turn.get("rounds", []):
            calls = round_record.get("tool_calls", [])
            if calls:
                st.caption(f"Round {round_record['round']} — {len(calls)} tool call(s)")
            for event in round_record.get("tool_results", []):
                render_tool_event(event)
        st.markdown(reply_text(turn.get("assistant_text") or ""))
        st.caption(f"status: `{turn['status']}`")


def reply_text(assistant_text: str) -> str:
    """Prefer the `reply` field when the model answers with the JSON output format."""
    try:
        payload = json.loads(assistant_text)
    except (json.JSONDecodeError, TypeError):
        return assistant_text
    if isinstance(payload, dict) and isinstance(payload.get("reply"), str):
        return payload["reply"]
    return assistant_text


st.set_page_config(page_title="IT Helpdesk Agent", page_icon="🛠️", layout="wide")
st.title("🛠️ IT Helpdesk Agent — live chat")

with st.sidebar:
    st.header("Run settings")
    version = st.text_input("Version label", value="v3")
    provider_name = st.selectbox("Provider", PROVIDERS, index=0)
    model = st.text_input("Model (blank = provider default)", value="") or None
    if st.button("New transcript", use_container_width=True):
        for key in ("transcript", "transcript_path", "history", "provider_obj", "provider_key"):
            st.session_state.pop(key, None)
        st.rerun()

if "transcript" not in st.session_state:
    transcript, path = new_transcript(version, provider_name, model)
    st.session_state.transcript = transcript
    st.session_state.transcript_path = path
    st.session_state.history = []

transcript = st.session_state.transcript
transcript_path = st.session_state.transcript_path

with st.sidebar:
    st.header("Artifact version")
    st.code(transcript["artifact_version"], language="text")
    st.caption(f"prompt_hash: `{transcript['prompt_hash'][:12]}`")
    st.caption(f"tools_hash: `{transcript['tools_hash'][:12]}`")
    st.caption(f"transcript: `{transcript_path.relative_to(ROOT)}`")
    st.caption(f"turns logged: {len(transcript['turns'])}")

for turn in transcript["turns"]:
    render_turn(turn)

user_text = st.chat_input("Ví dụ: VPN production có đang gặp sự cố không?")
if user_text:
    declarations = load_tool_declarations(TOOLS_PATH)
    openai_tools = to_openai_tools(declarations)
    provider_key = (provider_name, model)
    if st.session_state.get("provider_key") != provider_key:
        st.session_state.provider_obj = make_provider(provider_name)
        st.session_state.provider_key = provider_key
    provider = st.session_state.provider_obj

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")},
        *trim_history(st.session_state.history, transcript["history_window"]),
        {"role": "user", "content": user_text},
    ]
    turn_record: dict[str, Any] = {
        "turn_index": len(transcript["turns"]) + 1,
        "started_at": now_iso(),
        "user": user_text,
        "status": "started",
        "assistant_text": None,
        "rounds": [],
        "tool_events": [],
    }
    try:
        with st.spinner("Agent đang chạy tool loop..."):
            result = run_model_tool_loop(
                provider=provider,
                messages=messages,
                tools=openai_tools,
                model=model,
                max_tool_rounds=transcript["max_tool_rounds"],
            )
        turn_record.update(result)
        st.session_state.history.append({"role": "user", "content": user_text})
        st.session_state.history.append({"role": "assistant", "content": result["assistant_text"]})
    except Exception as exc:  # provider/network failure must not kill the app
        turn_record.update({"status": "provider_error", "error": f"{type(exc).__name__}: {exc}"})

    turn_record["ended_at"] = now_iso()
    transcript["turns"].append(turn_record)
    write_transcript(transcript_path, transcript)
    st.rerun()
