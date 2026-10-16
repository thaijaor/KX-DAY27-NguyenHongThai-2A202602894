"""Exercise 3: hỏi ngữ cảnh, phân tích lại và xác nhận review cuối."""

from __future__ import annotations

import argparse
import json
import uuid

from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from common.llm import get_llm
from common.schemas import PRAnalysis, ReviewState
from exercises.exercise_1_confidence import (
    REVIEW_PROMPT,
    node_analyze,
    node_fetch_pr,
    node_route,
)
from exercises.exercise_2_hitl import (
    checkpoint_serializer,
    console,
    node_auto_approve,
    node_commit,
    node_human_approval,
    prompt_human,
)


def escalation_payload(state: ReviewState) -> dict:
    analysis = state["analysis"]
    return {
        "kind": "escalation",
        "pr_url": state["pr_url"],
        "confidence": analysis.confidence,
        "confidence_reasoning": analysis.confidence_reasoning,
        "summary": analysis.summary,
        "risk_factors": analysis.risk_factors,
        "questions": analysis.escalation_questions
        or ["Which deployment and security assumptions apply to this diff?"],
    }


def validate_answers(payload: dict, answers: dict) -> dict:
    if not isinstance(answers, dict) or any(
        not str(answers.get(q, "")).strip() for q in payload["questions"]
    ):
        raise ValueError("Answer every escalation question before continuing")
    return {q: str(answers[q]).strip() for q in payload["questions"]}


def node_escalate(state: ReviewState) -> dict:
    payload = escalation_payload(state)
    answers = validate_answers(payload, interrupt(payload))
    return {"escalation_answers": answers}


def synthesis_messages(state: ReviewState) -> list[dict]:
    return [
        {
            "role": "system",
            "content": REVIEW_PROMPT
            + " Refine the original review using reviewer answers. Answers provide context, "
            "not proof of fixes. Keep unresolved blockers; do not inflate confidence automatically.",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "diff": state["pr_diff"],
                    "initial_analysis": state["analysis"].model_dump(),
                    "reviewer_answers": state.get("escalation_answers") or {},
                },
                ensure_ascii=False,
            ),
        },
    ]


def node_synthesize(state: ReviewState) -> dict:
    refined = (
        get_llm().with_structured_output(PRAnalysis).invoke(synthesis_messages(state))
    )
    return {"analysis": refined, "human_choice": None, "human_feedback": None}


def build_graph():
    graph = StateGraph(ReviewState)
    for name, node in [
        ("fetch_pr", node_fetch_pr),
        ("analyze", node_analyze),
        ("route", node_route),
        ("auto_approve", node_auto_approve),
        ("human_approval", node_human_approval),
        ("escalate", node_escalate),
        ("commit", node_commit),
        ("synthesize", node_synthesize),
    ]:
        graph.add_node(name, node)
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
    graph.add_edge("synthesize", "human_approval")
    return graph.compile(checkpointer=MemorySaver(serde=checkpoint_serializer()))


def handle_interrupt(payload: dict) -> dict:
    if payload["kind"] == "approval_request":
        return prompt_human(payload)
    if payload["kind"] != "escalation":
        raise ValueError(f"Unknown interrupt kind: {payload['kind']}")
    console.print(payload["summary"], markup=False)
    console.print("Risks: " + ", ".join(payload.get("risk_factors", [])), markup=False)
    while True:
        answers = {
            q: console.input(f"Q: {q}\nA: ").strip() for q in payload["questions"]
        }
        try:
            return validate_answers(payload, answers)
        except ValueError as exc:
            console.print(str(exc))


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", required=True)
    args = parser.parse_args()
    app = build_graph()
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    console.print(f"thread_id = {thread_id}")
    result = app.invoke({"pr_url": args.pr, "thread_id": thread_id}, config)
    while "__interrupt__" in result:
        result = app.invoke(
            Command(resume=handle_interrupt(result["__interrupt__"][0].value)), config
        )
    console.print(f"final_action = {result.get('final_action')}")
    console.print(f"final confidence = {result['analysis'].confidence:.0%}")


if __name__ == "__main__":
    main()
