"""Giao diện Streamlit cho agent HITL. Chạy: uv run streamlit run app.py."""

from __future__ import annotations

import asyncio
import uuid

import streamlit as st
from dotenv import load_dotenv

from audit.replay import recent_threads
from common.github import parse_pr_url
from common.schemas import PRAnalysis
from exercises.exercise_4_audit import invoke_review

load_dotenv()
st.set_page_config(page_title="HITL PR Review", layout="wide")
st.title("HITL PR Review Agent")
for key, default in {
    "thread_id": None,
    "pr_url": "",
    "interrupt_payload": None,
    "final": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


async def run_graph(
    pr_url: str, thread_id: str, resume_value=None, *, restore_only=False
):
    return await invoke_review(
        pr_url, thread_id, resume_value, restore_only=restore_only
    )


def save_result(result: dict) -> None:
    if result.get("__interrupt__"):
        st.session_state.interrupt_payload = result["__interrupt__"][0].value
        st.session_state.final = None
    else:
        st.session_state.interrupt_payload = None
        st.session_state.final = result
    st.query_params["thread"] = st.session_state.thread_id


def render_approval_card(payload: dict) -> dict | None:
    st.subheader(f"Approval requested — confidence {payload['confidence']:.0%}")
    st.caption(payload["confidence_reasoning"])
    st.markdown(payload["summary"])
    for comment in payload.get("comments", []):
        st.markdown(
            f"- **[{comment['severity']}]** `{comment['file']}:{comment.get('line') or '?'}` — {comment['body']}"
        )
    with st.expander("Diff"):
        st.code(payload.get("diff_preview", ""), language="diff")
    feedback = st.text_area(
        "Feedback / replacement review body for Edit",
        key="approval_feedback_" + st.session_state.thread_id,
    )
    col1, col2, col3 = st.columns(3)
    if col1.button("Approve", type="primary"):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("Reject"):
        return {"choice": "reject", "feedback": feedback}
    if col3.button("Edit"):
        if feedback.strip():
            return {"choice": "edit", "feedback": feedback}
        st.error("Enter the replacement review body before choosing Edit.")
    return None


def render_escalation_card(payload: dict) -> dict | None:
    st.subheader(f"Strong escalation — confidence {payload['confidence']:.0%}")
    st.caption(payload["confidence_reasoning"])
    if payload.get("risk_factors"):
        st.error("Risks: " + ", ".join(payload["risk_factors"]))
    st.markdown(payload["summary"])
    with st.form("escalation_" + st.session_state.thread_id):
        answers = {
            question: st.text_input(
                question, key=f"answer_{st.session_state.thread_id}_{index}"
            )
            for index, question in enumerate(payload["questions"])
        }
        submitted = st.form_submit_button("Submit answers")
    if submitted:
        if all(answer.strip() for answer in answers.values()):
            return answers
        st.error("Answer every question before continuing.")
    return None


with st.sidebar:
    st.header("Recent sessions")
    try:
        sessions = asyncio.run(recent_threads())
    except Exception as exc:
        st.error(f"Could not load sessions ({type(exc).__name__}).")
        sessions = []
    for session in sessions:
        label = f"{session['thread_id'][:8]} · {session['worst_risk']} · {session['events']} events"
        if st.button(label, key=session["thread_id"]):
            st.session_state.thread_id = session["thread_id"]
            st.session_state.pr_url = session["pr_url"]
            try:
                save_result(
                    asyncio.run(
                        run_graph(
                            session["pr_url"], session["thread_id"], restore_only=True
                        )
                    )
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Could not restore checkpoint ({type(exc).__name__}).")
        st.caption(f"{session['pr_url']} · {session['last_event']}")

# URL giữ thread khi trình duyệt reload (session_state riêng sẽ được tạo lại).
requested_thread = st.query_params.get("thread")
if requested_thread and st.session_state.thread_id is None:
    saved = next(
        (session for session in sessions if session["thread_id"] == requested_thread),
        None,
    )
    if saved:
        st.session_state.thread_id = saved["thread_id"]
        st.session_state.pr_url = saved["pr_url"]
        try:
            save_result(
                asyncio.run(
                    run_graph(saved["pr_url"], saved["thread_id"], restore_only=True)
                )
            )
        except Exception as exc:
            st.error(f"Could not restore checkpoint ({type(exc).__name__}).")

with st.form("start"):
    pr_url = st.text_input(
        "PR URL",
        value=st.session_state.pr_url,
        placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
    )
    submitted = st.form_submit_button("Run review")

if submitted:
    try:
        parse_pr_url(pr_url)
        st.session_state.pr_url = pr_url
        st.session_state.thread_id = str(uuid.uuid4())
        st.session_state.interrupt_payload = None
        st.session_state.final = None
        st.query_params["thread"] = st.session_state.thread_id
        with st.spinner("Fetching PR + asking the LLM..."):
            save_result(asyncio.run(run_graph(pr_url, st.session_state.thread_id)))
        st.rerun()
    except Exception as exc:
        st.error(
            f"Review failed ({type(exc).__name__}). Check credentials/network, then retry the saved thread."
        )

payload = st.session_state.interrupt_payload
if payload is not None:
    answer = (
        render_approval_card(payload)
        if payload["kind"] == "approval_request"
        else render_escalation_card(payload)
    )
    if answer is not None:
        try:
            with st.spinner("Resuming..."):
                save_result(
                    asyncio.run(
                        run_graph(
                            st.session_state.pr_url, st.session_state.thread_id, answer
                        )
                    )
                )
            st.rerun()
        except Exception as exc:
            st.error(
                f"Resume failed ({type(exc).__name__}). The checkpoint is retained."
            )

if st.session_state.thread_id:
    st.caption(f"thread_id = {st.session_state.thread_id}")
    if st.button("Reload / retry saved session"):
        try:
            save_result(
                asyncio.run(
                    run_graph(st.session_state.pr_url, st.session_state.thread_id)
                )
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Retry failed ({type(exc).__name__}).")

if st.session_state.final is not None:
    final = st.session_state.final
    action = final.get("final_action", "pending")
    if action in {"auto_approved", "committed"}:
        st.success(f"{action} — comment posted")
        st.link_button(
            "View review on GitHub", st.session_state.pr_url + "#discussion_bucket"
        )
    elif action == "rejected":
        st.warning("Rejected — no comment posted")
    else:
        st.info(f"final_action = {action}")
    if final.get("analysis"):
        analysis = PRAnalysis.model_validate(final["analysis"])
        st.metric("Confidence", f"{analysis.confidence:.0%}")
        st.markdown(analysis.summary)
    st.code(
        f"uv run python -m audit.replay --thread {st.session_state.thread_id}",
        language="bash",
    )
