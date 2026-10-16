"""Exercise 2: interrupt, approve/reject/edit và tiếp tục bằng Command."""

from __future__ import annotations

import argparse
import uuid

from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from rich.console import Console
from rich.panel import Panel

from common.github import post_review_comment
from common.schemas import ReviewState
from exercises.exercise_1_confidence import node_analyze, node_fetch_pr, node_route

console = Console()


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("common.schemas", "PRAnalysis"),
            ("common.schemas", "ReviewComment"),
        ]
    )


def approval_payload(state: ReviewState) -> dict:
    analysis = state["analysis"]
    return {
        "kind": "approval_request",
        "pr_url": state["pr_url"],
        "confidence": analysis.confidence,
        "confidence_reasoning": analysis.confidence_reasoning,
        "summary": analysis.summary,
        "comments": [c.model_dump() for c in analysis.comments],
        "diff_preview": state["pr_diff"][:2000],
    }


def validate_response(response: dict) -> dict:
    choice = response.get("choice")
    if choice not in {"approve", "reject", "edit"}:
        raise ValueError("Choice must be approve, reject or edit")
    feedback = str(response.get("feedback") or "").strip()
    if choice == "edit" and not feedback:
        raise ValueError("Edit requires a replacement review body")
    return {"human_choice": choice, "human_feedback": feedback}


def node_human_approval(state: ReviewState) -> dict:
    return validate_response(interrupt(approval_payload(state)))


def _render_comment_body(state: ReviewState) -> str:
    if state.get("human_choice") == "edit":
        return state["human_feedback"]
    analysis = state["analysis"]
    lines = [
        f"### Day27 HITL review (confidence {analysis.confidence:.0%})",
        "",
        analysis.summary,
        "",
    ]
    for comment in analysis.comments:
        lines.append(
            f"- **[{comment.severity}]** `{comment.file}:{comment.line or '?'}` — {comment.body}"
        )
    if state.get("human_feedback"):
        lines.append(f"\nReviewer note: {state['human_feedback']}")
    if state.get("escalation_answers"):
        lines.append("\nReviewer context:")
        for question, answer in state["escalation_answers"].items():
            lines.append(f"- **{question}** {answer}")
    return "\n".join(lines)


def _post(state: ReviewState, label: str = "committed") -> str:
    # Lỗi mạng/quyền phải làm lệnh thất bại; không báo giả rằng đã đăng thành công.
    post_review_comment(state["pr_url"], _render_comment_body(state))
    console.print(f"Posted comment: {state['pr_url']}")
    return label


def node_commit(state: ReviewState) -> dict:
    if state.get("human_choice") in {"approve", "edit"}:
        return {
            "final_action": _post(state),
            "posted_comment_body": _render_comment_body(state),
        }
    return {"final_action": "rejected"}


def node_auto_approve(state: ReviewState) -> dict:
    return {
        "final_action": _post(state, "auto_approved"),
        "posted_comment_body": _render_comment_body(state),
    }


def node_escalate(state: ReviewState) -> dict:
    # Exercise 3 bổ sung Q&A; exercise 2 chưa tự đăng nhánh rủi ro cao.
    return {"final_action": "pending_escalation"}


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
    graph.add_edge("escalate", END)
    graph.add_edge("human_approval", "commit")
    graph.add_edge("commit", END)
    return graph.compile(checkpointer=MemorySaver(serde=checkpoint_serializer()))


def prompt_human(payload: dict) -> dict:
    console.print(
        Panel(payload["summary"], title=f"Approval — {payload['confidence']:.0%}")
    )
    console.print(payload["confidence_reasoning"], markup=False)
    for comment in payload.get("comments", []):
        console.print(
            f"{comment['severity']} {comment['file']}:{comment.get('line')} — {comment['body']}",
            markup=False,
        )
    console.print(payload.get("diff_preview", ""), markup=False)
    while True:
        choice = console.input("approve/reject/edit? ").strip().lower()
        feedback = (
            console.input("Replacement review body / feedback: ").strip()
            if choice != "approve"
            else ""
        )
        response = {"choice": choice, "feedback": feedback}
        try:
            validate_response(response)
            return response
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
        response = prompt_human(result["__interrupt__"][0].value)
        result = app.invoke(Command(resume=response), config)
    console.print(f"final_action = {result.get('final_action')}")


if __name__ == "__main__":
    main()
