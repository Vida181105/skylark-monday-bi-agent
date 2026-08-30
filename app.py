"""Streamlit chat UI for the Skylark Drones BI agent."""

from __future__ import annotations

import os

import streamlit as st
from dotenv import load_dotenv

from skylark import monday_client
from skylark.agent import BIAgent

CONFIG_KEYS = (
    "MONDAY_API_TOKEN",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "MONDAY_DEALS_BOARD_ID",
    "MONDAY_WORK_ORDERS_BOARD_ID",
)


def load_config() -> None:
    """Read config from .env locally and from st.secrets on Streamlit Cloud.

    Everything downstream reads os.environ when it is used, not when it is
    imported, so this only has to run before the first question.
    """
    load_dotenv()
    try:
        for key in CONFIG_KEYS:
            if key in st.secrets and not os.environ.get(key):
                os.environ[key] = str(st.secrets[key])
    except Exception:  # no secrets.toml locally; .env covers it
        pass


st.set_page_config(page_title="Skylark BI Agent", layout="centered")
load_config()

# Each of these returns a substantive answer against the current boards. Avoid
# phrasing them around the present ("this quarter", "this week"): the boards are
# a snapshot whose records stop in January 2026, so a current-period filter
# correctly matches nothing and reads like a broken app.
EXAMPLES = [
    "How's the pipeline looking for the energy sector?",
    "Which sectors convert best from pipeline to won deals?",
    "Where are deals getting stuck in the funnel?",
    "Which open deals are past their expected close date?",
    "Which work orders are delivered but not yet fully billed?",
    "How does open pipeline value split across owners?",
]

EXEC_PROMPT = (
    "Generate a leadership summary: pipeline health, wins and losses, sector movement, "
    "execution and billing/collection risk, and the 2-3 items that need a decision."
)


def _friendly_error(exc: Exception) -> str:
    """Say what the user can do about it, keeping the detail for the curious."""
    code = getattr(exc, "code", None)
    if code == 429:
        lead = "Gemini's free tier is rate limited right now. Wait a minute and ask again."
    elif code in (500, 502, 503, 504):
        lead = "Gemini is busy and returned a server error. Try the question again."
    elif "MONDAY_API_TOKEN" in str(exc) or code == 401:
        lead = "monday.com rejected the API token. Check it in the app's Secrets."
    else:
        lead = "Something went wrong."
    return f"{lead}\n\n<details><summary>Details</summary>\n\n`{type(exc).__name__}: {exc}`\n\n</details>"


def missing_config() -> list[str]:
    return [k for k in ("MONDAY_API_TOKEN", "GEMINI_API_KEY") if not os.environ.get(k, "").strip()]


st.title("Skylark BI Agent")
st.caption(
    "Ask about the sales pipeline or project execution. Answers are computed live from the "
    "**Deal tracker** and **Work order tracker** monday.com boards."
)

with st.sidebar:
    st.subheader("Connection")
    missing = missing_config()
    if missing:
        st.error("Missing config: " + ", ".join(missing))
        st.caption("Set these in `.env` locally, or in the app's Secrets when deployed.")
    else:
        ids = monday_client.board_ids()
        st.success("Configured")
        st.caption(f"Deals board `{ids['deals']}`\n\nWork orders board `{ids['work_orders']}`")
    if st.button("Refresh board data", use_container_width=True):
        monday_client.clear_cache()
        st.toast("Cache cleared. The next question re-reads monday.com.")

    st.subheader("Try asking")
    for q in EXAMPLES:
        if st.button(q, use_container_width=True, key=f"ex-{q}"):
            st.session_state.pending = q

    st.divider()
    if st.button("Leadership summary", type="primary", use_container_width=True):
        st.session_state.pending = EXEC_PROMPT
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.pop("agent", None)
        st.session_state.transcript = []
        st.rerun()

st.session_state.setdefault("transcript", [])

for turn in st.session_state.transcript:
    with st.chat_message(turn["role"]):
        st.markdown(turn["content"])
        for step in turn.get("steps", []):
            with st.expander(step["label"], expanded=False):
                st.json(step["detail"], expanded=False)

prompt = st.chat_input("e.g. How's pipeline looking for mining this quarter?")
if not prompt:
    prompt = st.session_state.pop("pending", None)

if prompt:
    if missing_config():
        st.error("Add the missing configuration above before asking a question.")
        st.stop()

    st.session_state.transcript.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    if "agent" not in st.session_state:
        st.session_state.agent = BIAgent()

    with st.chat_message("assistant"):
        status = st.status("Reading monday.com...", expanded=False)
        placeholder = st.empty()
        answer, steps = "", []
        try:
            for event in st.session_state.agent.ask(prompt):
                if event["type"] == "text":
                    answer += event["text"]
                    placeholder.markdown(answer)
                elif event["type"] == "tool":
                    label = f"{event['name']}({event['input'].get('dataset', '')})"
                    status.update(label=label)
                    steps.append({"label": label, "detail": event["input"]})
                elif event["type"] == "tool_result":
                    out = event["output"]
                    if steps:
                        steps[-1]["detail"] = {"query": steps[-1]["detail"], "result": out}
                    n = out.get("rows_matching_filters", out.get("row_count"))
                    if n is not None:
                        status.update(label=f"{steps[-1]['label']} - {n} rows")
        except Exception as exc:
            answer = _friendly_error(exc)
            placeholder.markdown(answer)
        # "Ran 0 board queries" reads as though the answer was invented, when it
        # means the model reused figures already retrieved in this conversation.
        if steps:
            done = f"Ran {len(steps)} board quer{'y' if len(steps) == 1 else 'ies'}"
        else:
            done = "Answered from data already retrieved in this conversation"
        status.update(label=done, state="complete")
        for step in steps:
            with st.expander(step["label"], expanded=False):
                st.json(step["detail"], expanded=False)

    st.session_state.transcript.append({"role": "assistant", "content": answer, "steps": steps})
