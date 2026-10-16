"""Exercise 4: audit SQLite và checkpoint bền vững qua các lần chạy."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid
from functools import lru_cache

import aiosqlite
import httpx
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from common.db import db_conn, db_path, write_audit_event
from common.llm import get_llm
from common.schemas import AuditEntry, PRAnalysis, ReviewState, risk_level_for
from exercises import exercise_1_confidence as ex1
from exercises import exercise_2_hitl as ex2
from exercises.exercise_3_escalation import (
    escalation_payload,
    handle_interrupt,
    synthesis_messages,
    validate_answers,
)

console = ex2.console
AGENT_ID = "pr-review-agent@v0.1"


@lru_cache(maxsize=1)
def current_reviewer() -> str:
    """Lấy username từ token, không yêu cầu người chấm thêm biến môi trường."""
    response = httpx.get(
        "https://api.github.com/user",
        headers={"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"]},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["login"]


async def audit(state: ReviewState, entry: AuditEntry) -> None:
    # interrupt chạy lại node từ đầu khi resume: chỉ ghi một lần sự kiện chờ.
    if entry.action in {"human_approval", "escalate"} and entry.decision == "pending":
        async with db_conn() as conn:
            async with conn.execute(
                "SELECT 1 FROM audit_events WHERE thread_id=? AND action=? AND decision=? LIMIT 1",
                (state["thread_id"], entry.action, "pending"),
            ) as cursor:
                if await cursor.fetchone():
                    return
    await write_audit_event(
        thread_id=state["thread_id"], pr_url=state["pr_url"], entry=entry
    )


def entry(
    state: ReviewState,
    action: str,
    start: float,
    decision: str = "pending",
    reason: str | None = None,
    human: bool = False,
    analysis: PRAnalysis | None = None,
) -> AuditEntry:
    analysis = analysis or state.get("analysis")
    confidence = analysis.confidence if analysis else 0.0
    return AuditEntry(
        agent_id=AGENT_ID,
        action=action,
        confidence=confidence,
        risk_level=risk_level_for(confidence) if analysis else "med",
        reviewer_id=(os.environ.get("GITHUB_USER") or current_reviewer())
        if human
        else None,
        decision=decision,
        reason=reason,
        execution_time_ms=int((time.monotonic() - start) * 1000),
    )


async def node_fetch_pr(state: ReviewState) -> dict:
    start = time.monotonic()
    result = await asyncio.to_thread(ex1.node_fetch_pr, state)
    await audit(
        state,
        entry(
            state,
            "fetch_pr",
            start,
            reason=f"Fetched {len(result['pr_files'])} files, head={result['pr_head_sha'][:7]}",
        ),
    )
    return result


async def node_analyze(state: ReviewState) -> dict:
    start = time.monotonic()
    analysis = (
        await get_llm()
        .with_structured_output(PRAnalysis)
        .ainvoke(ex1.analysis_messages(state))
    )
    await audit(
        state,
        entry(
            state,
            "analyze",
            start,
            reason=analysis.confidence_reasoning,
            analysis=analysis,
        ),
    )
    return {"analysis": analysis}


async def node_route(state: ReviewState) -> dict:
    start = time.monotonic()
    result = ex1.node_route(state)
    decision = {
        "auto_approve": "auto",
        "human_approval": "pending",
        "escalate": "escalate",
    }[result["decision"]]
    await audit(
        state, entry(state, "route", start, decision, f"Branch: {result['decision']}")
    )
    return result


async def node_human_approval(state: ReviewState) -> dict:
    start = time.monotonic()
    await audit(
        state,
        entry(state, "human_approval", start, reason="Awaiting reviewer confirmation"),
    )
    result = ex2.validate_response(interrupt(ex2.approval_payload(state)))
    await audit(
        state,
        entry(
            state,
            "human_approval",
            start,
            result["human_choice"],
            result["human_feedback"] or "Reviewer confirmed proposed review",
            human=True,
        ),
    )
    return result


async def node_commit(state: ReviewState) -> dict:
    start = time.monotonic()
    try:
        result = await asyncio.to_thread(ex2.node_commit, state)
    except Exception as exc:
        await audit(
            state,
            entry(
                state,
                "commit",
                start,
                state.get("human_choice") or "pending",
                f"Posting failed ({type(exc).__name__}); retry the existing thread",
                human=True,
            ),
        )
        raise
    await audit(
        state,
        entry(
            state,
            "commit",
            start,
            state.get("human_choice") or "reject",
            result["final_action"],
            human=True,
        ),
    )
    return result


async def node_auto_approve(state: ReviewState) -> dict:
    start = time.monotonic()
    try:
        result = await asyncio.to_thread(ex2.node_auto_approve, state)
    except Exception as exc:
        await audit(
            state,
            entry(
                state,
                "auto_approve",
                start,
                "auto",
                f"Posting failed ({type(exc).__name__})",
            ),
        )
        raise
    await audit(
        state, entry(state, "auto_approve", start, "auto", result["final_action"])
    )
    return result


async def node_escalate(state: ReviewState) -> dict:
    start = time.monotonic()
    payload = escalation_payload(state)
    await audit(
        state,
        entry(
            state,
            "escalate",
            start,
            reason="Questions: " + " | ".join(payload["questions"]),
        ),
    )
    answers = validate_answers(payload, interrupt(payload))
    await audit(
        state,
        entry(
            state,
            "escalate",
            start,
            "escalate",
            " | ".join(f"{q}: {a}" for q, a in answers.items()),
            human=True,
        ),
    )
    return {"escalation_answers": answers}


async def node_synthesize(state: ReviewState) -> dict:
    start = time.monotonic()
    refined = (
        await get_llm()
        .with_structured_output(PRAnalysis)
        .ainvoke(synthesis_messages(state))
    )
    await audit(
        state,
        entry(
            state,
            "synthesize",
            start,
            reason=refined.confidence_reasoning,
            analysis=refined,
        ),
    )
    return {"analysis": refined, "human_choice": None, "human_feedback": None}


def build_graph(checkpointer):
    graph = StateGraph(ReviewState)
    for name in (
        "fetch_pr",
        "analyze",
        "route",
        "auto_approve",
        "human_approval",
        "commit",
        "escalate",
        "synthesize",
    ):
        graph.add_node(name, globals()["node_" + name])
    graph.add_edge(START, "fetch_pr")
    graph.add_edge("fetch_pr", "analyze")
    graph.add_edge("analyze", "route")
    graph.add_conditional_edges(
        "route",
        lambda state: state["decision"],
        {name: name for name in ("auto_approve", "human_approval", "escalate")},
    )
    graph.add_edge("auto_approve", END)
    graph.add_edge("human_approval", "commit")
    graph.add_edge("commit", END)
    graph.add_edge("escalate", "synthesize")
    # Q&A không phải đồng ý đăng: review mới cần một lần xác nhận rõ ràng.
    graph.add_edge("synthesize", "human_approval")
    return graph.compile(checkpointer=checkpointer)


def snapshot_result(snapshot) -> dict:
    result = dict(snapshot.values)
    pending = [item for task in snapshot.tasks for item in task.interrupts]
    if pending:
        result["__interrupt__"] = pending
    return result


async def invoke_review(
    pr_url: str, thread_id: str, resume_value=None, *, restore_only: bool = False
) -> dict:
    async with aiosqlite.connect(db_path()) as connection:
        checkpointer = AsyncSqliteSaver(connection, serde=ex2.checkpoint_serializer())
        await checkpointer.setup()
        app = build_graph(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await app.aget_state(config)
        if snapshot.values:
            if snapshot.values["pr_url"] != pr_url:
                raise ValueError("Thread belongs to another PR")
            if restore_only or not snapshot.next:
                return snapshot_result(snapshot)
            if resume_value is None and any(task.interrupts for task in snapshot.tasks):
                return snapshot_result(snapshot)
            value = Command(resume=resume_value) if resume_value is not None else None
        else:
            if restore_only or resume_value is not None:
                raise ValueError("No saved checkpoint for this thread")
            value = {"pr_url": pr_url, "thread_id": thread_id}
        return await app.ainvoke(value, config)


async def run(pr_url: str, thread_id: str | None = None) -> dict:
    thread_id = thread_id or str(uuid.uuid4())
    console.print(f"thread_id = {thread_id}")
    result = await invoke_review(pr_url, thread_id)
    while "__interrupt__" in result:
        answer = handle_interrupt(result["__interrupt__"][0].value)
        result = await invoke_review(pr_url, thread_id, answer)
    console.print(f"final_action = {result.get('final_action')}")
    console.print(f"Replay: uv run python -m audit.replay --thread {thread_id}")
    return result


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", required=True)
    parser.add_argument(
        "--thread", help="Resume a saved thread without fetching/analyzing again"
    )
    args = parser.parse_args()
    asyncio.run(run(args.pr, args.thread))


if __name__ == "__main__":
    main()
