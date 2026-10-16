"""Kiểm tra hành vi offline: graph, HITL, checkpoint, audit và UI."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langgraph.types import Command

from audit.replay import recent_threads
from common.db import replay_events
from common.github import PullRequest
from common.schemas import PRAnalysis, risk_level_for
from exercises import exercise_1_confidence as ex1
from exercises import exercise_2_hitl as ex2
from exercises import exercise_3_escalation as ex3
from exercises import exercise_4_audit as ex4

PR_URL = "https://github.com/VinUni-AI20k/PR-Demo/pull/1"
PR = PullRequest(
    PR_URL,
    "VinUni-AI20k",
    "PR-Demo",
    1,
    "Test change",
    "instructor",
    "main",
    "feature",
    "abcdef123",
    "+test diff",
    ["test.py"],
)


def analysis(confidence=0.65):
    return PRAnalysis(
        summary="Test review",
        confidence=confidence,
        confidence_reasoning="Needs context",
        risk_factors=["migration"],
        escalation_questions=["What is the migration plan?"],
    )


class FakeLLM:
    def __init__(self, *outputs):
        self.outputs = iter(outputs)

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return next(self.outputs)

    async def ainvoke(self, messages):
        return next(self.outputs)


class RoutingTests(unittest.TestCase):
    def test_threshold_boundaries(self):
        for confidence, decision, risk in [
            (0, "escalate", "high"),
            (0.579, "escalate", "high"),
            (0.58, "human_approval", "med"),
            (0.729, "human_approval", "med"),
            (0.73, "auto_approve", "low"),
            (1, "auto_approve", "low"),
        ]:
            with self.subTest(confidence=confidence):
                self.assertEqual(
                    ex1.node_route({"analysis": analysis(confidence)})["decision"],
                    decision,
                )
                self.assertEqual(risk_level_for(confidence), risk)

    def test_exercise1_all_branches_no_post(self):
        for confidence, action in [
            (0.9, "auto_approved"),
            (0.65, "pending_human_approval"),
            (0.4, "pending_escalation"),
        ]:
            with (
                patch.object(ex1, "fetch_pr", return_value=PR),
                patch.object(
                    ex1, "get_llm", return_value=FakeLLM(analysis(confidence))
                ),
            ):
                self.assertEqual(
                    ex1.build_graph().invoke({"pr_url": PR_URL})["final_action"], action
                )

    def test_exercise2_resume_decisions_and_no_early_post(self):
        for choice in ("approve", "reject", "edit"):
            with (
                self.subTest(choice=choice),
                patch.object(ex1, "fetch_pr", return_value=PR),
                patch.object(ex1, "get_llm", return_value=FakeLLM(analysis())),
                patch.object(ex2, "post_review_comment") as post,
            ):
                graph = ex2.build_graph()
                config = {"configurable": {"thread_id": choice}}
                paused = graph.invoke({"pr_url": PR_URL}, config)
                self.assertEqual(
                    paused["__interrupt__"][0].value["kind"], "approval_request"
                )
                post.assert_not_called()
                result = graph.invoke(
                    Command(
                        resume={"choice": choice, "feedback": "Replacement review"}
                    ),
                    config,
                )
                self.assertEqual(
                    result["final_action"],
                    "rejected" if choice == "reject" else "committed",
                )
                self.assertEqual(post.call_count, 0 if choice == "reject" else 1)
                if choice == "edit":
                    self.assertEqual(post.call_args.args[1], "Replacement review")

    def test_invalid_choice_and_empty_edit(self):
        for response in ({"choice": "anything"}, {"choice": "edit", "feedback": " "}):
            with self.assertRaises(ValueError):
                ex2.validate_response(response)

    def test_exercise3_escalation_then_final_confirmation(self):
        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(ex1, "get_llm", return_value=FakeLLM(analysis(0.4))),
            patch.object(ex3, "get_llm", return_value=FakeLLM(analysis(0.68))),
            patch.object(ex2, "post_review_comment") as post,
        ):
            graph = ex3.build_graph()
            config = {"configurable": {"thread_id": "ex3"}}
            result = graph.invoke({"pr_url": PR_URL}, config)
            self.assertEqual(result["__interrupt__"][0].value["kind"], "escalation")
            result = graph.invoke(
                Command(
                    resume={
                        "What is the migration plan?": "Not specified; keep blocker"
                    }
                ),
                config,
            )
            self.assertEqual(
                result["__interrupt__"][0].value["kind"], "approval_request"
            )
            self.assertEqual(result["analysis"].confidence, 0.68)
            post.assert_not_called()
            result = graph.invoke(Command(resume={"choice": "reject"}), config)
            self.assertEqual(result["final_action"], "rejected")
            post.assert_not_called()


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "HITL_DB_PATH": str(Path(self.temp.name) / "audit.db"),
                "GITHUB_USER": "test-reviewer",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def call(self, *args, **kwargs):
        # Mỗi lần gọi dùng kết nối SQLite và event loop mới, như các lần rerun UI.
        return asyncio.run(ex4.invoke_review(PR_URL, "durable-thread", *args, **kwargs))

    def test_reopen_resume_audit_and_no_duplicate_pending(self):
        with (
            patch.object(ex1, "fetch_pr", return_value=PR) as fetch,
            patch.object(ex4, "get_llm", return_value=FakeLLM(analysis())) as llm,
            patch.object(ex2, "post_review_comment") as post,
        ):
            first = self.call()
            self.assertIn("__interrupt__", first)
            self.assertIn("__interrupt__", self.call(restore_only=True))
            final = self.call({"choice": "approve"})
            self.assertEqual(final["final_action"], "committed")
            self.assertEqual(self.call()["final_action"], "committed")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(llm.call_count, 1)
            post.assert_called_once()
        events = asyncio.run(replay_events("durable-thread"))
        self.assertEqual(
            [row["action"] for row in events],
            [
                "fetch_pr",
                "analyze",
                "route",
                "human_approval",
                "human_approval",
                "commit",
            ],
        )
        self.assertEqual(events[0]["risk_level"], "med")
        self.assertIsNone(events[3]["reviewer_id"])
        self.assertEqual(events[4]["reviewer_id"], "test-reviewer")
        self.assertEqual(events[4]["decision"], "approve")
        self.assertTrue(all(row["execution_time_ms"] >= 0 for row in events))

    def test_escalation_audit_and_worst_risk(self):
        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(
                ex4, "get_llm", return_value=FakeLLM(analysis(0.4), analysis(0.7))
            ),
            patch.object(ex2, "post_review_comment") as post,
        ):
            self.call()
            self.call({"What is the migration plan?": "Keep migration blocker"})
            self.call({"choice": "reject"})
            post.assert_not_called()
        events = asyncio.run(replay_events("durable-thread"))
        self.assertEqual(
            [row["action"] for row in events],
            [
                "fetch_pr",
                "analyze",
                "route",
                "escalate",
                "escalate",
                "synthesize",
                "human_approval",
                "human_approval",
                "commit",
            ],
        )
        self.assertEqual(events[5]["confidence"], 0.7)
        self.assertEqual(asyncio.run(recent_threads())[0]["worst_risk"], "high")

    def test_auto_audit_and_no_human(self):
        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(ex4, "get_llm", return_value=FakeLLM(analysis(0.95))),
            patch.object(ex2, "post_review_comment") as post,
        ):
            self.assertEqual(self.call()["final_action"], "auto_approved")
            post.assert_called_once()
        events = asyncio.run(replay_events("durable-thread"))
        self.assertTrue(all(row["reviewer_id"] is None for row in events))
        self.assertEqual(events[-1]["decision"], "auto")
        self.assertEqual(events[-1]["risk_level"], "low")

    def test_post_failure_is_not_success_and_retry_keeps_analysis(self):
        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(ex4, "get_llm", return_value=FakeLLM(analysis())),
            patch.object(
                ex2, "post_review_comment", side_effect=RuntimeError("offline")
            ) as post,
        ):
            self.call()
            with self.assertRaises(RuntimeError):
                self.call({"choice": "approve"})
            self.assertNotIn("final_action", self.call(restore_only=True))
            post.side_effect = None
            self.assertEqual(self.call()["final_action"], "committed")

    def test_wrong_pr_cannot_reuse_thread(self):
        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(ex4, "get_llm", return_value=FakeLLM(analysis())),
        ):
            self.call()
        with self.assertRaises(ValueError):
            asyncio.run(
                ex4.invoke_review(PR_URL + "0", "durable-thread", restore_only=True)
            )


class StreamlitTests(unittest.TestCase):
    setUp = DurableTests.setUp
    tearDown = DurableTests.tearDown

    def test_ui_approval_and_rejection(self):
        from streamlit.testing.v1 import AppTest

        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(ex4, "get_llm", return_value=FakeLLM(analysis())),
            patch.object(ex2, "post_review_comment") as post,
        ):
            app = AppTest.from_file(
                str(Path(__file__).resolve().parents[1] / "app.py")
            ).run()
            app.text_input[0].set_value(PR_URL)
            next(
                button for button in app.button if button.label == "Run review"
            ).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(
                any("Approval requested" in item.value for item in app.subheader)
            )
            next(
                button for button in app.button if button.label == "Reject"
            ).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("Rejected" in item.value for item in app.warning))
            post.assert_not_called()

    def test_ui_edit_publishes_replacement(self):
        from streamlit.testing.v1 import AppTest

        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(ex4, "get_llm", return_value=FakeLLM(analysis())),
            patch.object(ex2, "post_review_comment") as post,
        ):
            app = AppTest.from_file(
                str(Path(__file__).resolve().parents[1] / "app.py")
            ).run()
            app.text_input[0].set_value(PR_URL)
            next(
                button for button in app.button if button.label == "Run review"
            ).click().run()
            next(
                button for button in app.button if button.label == "Edit"
            ).click().run()
            self.assertTrue(app.error)
            post.assert_not_called()
            app.text_area[0].set_value("Edited review from UI test")
            next(
                button for button in app.button if button.label == "Edit"
            ).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.success)
            self.assertEqual(post.call_args.args[1], "Edited review from UI test")

    def test_ui_escalation_restore_and_confirm(self):
        from streamlit.testing.v1 import AppTest

        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(
                ex4, "get_llm", return_value=FakeLLM(analysis(0.4), analysis(0.7))
            ),
            patch.object(ex2, "post_review_comment") as post,
        ):
            source = str(Path(__file__).resolve().parents[1] / "app.py")
            app = AppTest.from_file(source).run()
            app.text_input[0].set_value(PR_URL)
            next(
                button for button in app.button if button.label == "Run review"
            ).click().run()
            self.assertTrue(
                any("Strong escalation" in item.value for item in app.subheader)
            )
            thread_id = app.session_state["thread_id"]
            # Một session_state mới như reload trình duyệt; thread lấy từ URL.
            restored = AppTest.from_file(source)
            restored.query_params["thread"] = thread_id
            restored.run()
            self.assertEqual(restored.session_state["thread_id"], thread_id)
            self.assertFalse(restored.exception)
            next(
                button for button in restored.button if button.label == "Submit answers"
            ).click().run()
            self.assertTrue(restored.error)
            next(
                field
                for field in restored.text_input
                if field.label == "What is the migration plan?"
            ).set_value("Keep migration blocker")
            next(
                button for button in restored.button if button.label == "Submit answers"
            ).click().run()
            self.assertTrue(
                any("Approval requested" in item.value for item in restored.subheader)
            )
            post.assert_not_called()
            next(
                button for button in restored.button if button.label == "Approve"
            ).click().run()
            self.assertFalse(restored.exception)
            self.assertTrue(restored.success)
            post.assert_called_once()

    def test_ui_auto_success(self):
        from streamlit.testing.v1 import AppTest

        with (
            patch.object(ex1, "fetch_pr", return_value=PR),
            patch.object(ex4, "get_llm", return_value=FakeLLM(analysis(0.95))),
            patch.object(ex2, "post_review_comment") as post,
        ):
            app = AppTest.from_file(
                str(Path(__file__).resolve().parents[1] / "app.py")
            ).run()
            app.text_input[0].set_value(PR_URL)
            next(
                button for button in app.button if button.label == "Run review"
            ).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("auto_approved" in item.value for item in app.success))
            post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
