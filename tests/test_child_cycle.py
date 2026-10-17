"""In-process child cycle tests against scripted agent sessions, gates, CI, and GitHub."""

import pathlib
import sys
import unittest


KIT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "src"))

from ralph_kit.child_cycle import (
    ChildCycleAdapters,
    CIFailure,
    CIUnavailable,
    GateFailure,
    HandedToHumanReview,
    RunBudget,
    RunStop,
    SessionResult,
    Verified,
    run_child_cycle,
)


CHILD = 7
BASE = "base"


def committed(findings=()):
    return ("committed", 1, list(findings))


def no_changes():
    return ("no_changes", 0, [])


def needs_correction(*findings, commits=0):
    return ("needs_correction", commits, list(findings))


def blocked(summary="needs a decision"):
    return ("blocked", 0, [], summary)


class FakeTools:
    """Scripted adapters: sessions per role, then gate and CI verdicts in order."""

    def __init__(self, sessions, gates=(), ci=()):
        self.head = BASE
        self.scripts = {role: list(results) for role, results in sessions.items()}
        self.gates = list(gates)
        self.ci = list(ci)
        self.requests = []
        self.gate_heads = []
        self.ci_heads = []
        self.handoffs = []
        self.adapters = ChildCycleAdapters(
            run_session=self.run_session,
            run_local_gates=self.run_local_gates,
            verify_ci=self.verify_ci,
            hand_to_human_review=lambda child, body: self.handoffs.append((child, body)),
        )

    def run_session(self, request):
        self.requests.append(request)
        outcome, commits, findings, *summary = self.scripts[request.role].pop(0)
        new = [f"{request.role}-{len(self.requests)}-{index}" for index in range(commits)]
        if new:
            self.head = new[-1]
        summary = summary[0] if summary else f"{request.role} summary"
        return SessionResult(outcome, summary, findings, self.head, new)

    def run_local_gates(self, child, base_sha, head_sha):
        self.gate_heads.append(head_sha)
        return self.gates.pop(0) if self.gates else None

    def verify_ci(self, child, base_sha, head_sha):
        self.ci_heads.append(head_sha)
        return self.ci.pop(0) if self.ci else None

    def roles(self):
        return [request.role for request in self.requests]


def cycle(tools, *, review=False, budget=20):
    run_budget = RunBudget(budget)
    result = run_child_cycle(CHILD, BASE, run_budget, tools.adapters, review=review)
    return result, run_budget


class VerifiedCycleTests(unittest.TestCase):
    def test_standard_run_verifies_the_implementation_head(self):
        tools = FakeTools({"implementation": [committed()]})

        result, budget = cycle(tools)

        self.assertEqual(result, Verified("implementation-1-0", "implementation-1-0"))
        self.assertEqual(tools.gate_heads, ["implementation-1-0"])
        self.assertEqual(tools.ci_heads, ["implementation-1-0"])
        self.assertEqual(budget.sessions, 1)
        self.assertEqual(tools.handoffs, [])

    def test_reviewed_run_reviews_the_gated_head(self):
        tools = FakeTools({"implementation": [committed()], "review": [no_changes()]})

        result, _ = cycle(tools, review=True)

        self.assertEqual(result, Verified("implementation-1-0", "implementation-1-0"))
        review = tools.requests[1]
        self.assertEqual((review.base_sha, review.head_sha), (BASE, "implementation-1-0"))

    def test_findings_are_corrected_before_gates_and_ci(self):
        tools = FakeTools({
            "implementation": [needs_correction("criterion 2 missing", commits=1)],
            "issue correction": [committed()],
        })

        result, _ = cycle(tools)

        self.assertEqual(result, Verified("implementation-1-0", "issue correction-2-0"))
        correction = tools.requests[1]
        self.assertEqual(correction.findings, ("criterion 2 missing",))
        self.assertEqual(correction.correction, 1)
        self.assertEqual(tools.gate_heads, ["issue correction-2-0"])

    def test_failed_gate_is_corrected_and_rechecked(self):
        tools = FakeTools(
            {"implementation": [committed()], "local gate correction": [committed()]},
            gates=[GateFailure("unit tests", "1 failed")],
        )

        result, _ = cycle(tools)

        self.assertEqual(result.verified_head, "local gate correction-2-0")
        self.assertEqual(tools.requests[1].gate, "unit tests")
        self.assertEqual(tools.requests[1].diagnostic, "1 failed")
        self.assertEqual(tools.gate_heads, ["implementation-1-0", "local gate correction-2-0"])

    def test_ci_failure_is_corrected_and_the_new_head_reverified(self):
        tools = FakeTools(
            {"implementation": [committed()], "CI correction": [committed()]},
            ci=[CIFailure("lint failed")],
        )

        result, _ = cycle(tools)

        self.assertEqual(result, Verified("implementation-1-0", "CI correction-2-0"))
        self.assertEqual(tools.ci_heads, ["implementation-1-0", "CI correction-2-0"])
        self.assertEqual(tools.gate_heads, ["implementation-1-0", "CI correction-2-0"])

    def test_gate_correction_after_review_commits_triggers_another_review(self):
        tools = FakeTools(
            {
                "implementation": [committed()],
                "review": [committed(), no_changes()],
                "local gate correction": [committed()],
            },
            gates=[None, GateFailure("unit tests", "1 failed")],
        )

        result, _ = cycle(tools, review=True)

        self.assertEqual(
            tools.roles(),
            ["implementation", "review", "local gate correction", "review"],
        )
        self.assertEqual(result.verified_head, "local gate correction-3-0")


class HumanReviewTests(unittest.TestCase):
    def assert_handed_off(self, tools, result, reason, *body_parts):
        self.assertEqual(result, HandedToHumanReview(reason))
        self.assertEqual(len(tools.handoffs), 1)
        child, body = tools.handoffs[0]
        self.assertEqual(child, CHILD)
        self.assertIn("This child remains open for human review.", body)
        for part in body_parts:
            self.assertIn(part, body)

    def test_blocked_implementation_is_handed_off(self):
        tools = FakeTools({"implementation": [blocked("needs a product decision")]})

        result, _ = cycle(tools)

        self.assert_handed_off(
            tools, result,
            "implementation stopped with blocked: needs a product decision",
            "implementation reported blocked",
        )
        self.assertEqual(tools.gate_heads, [])

    def test_corrections_of_every_kind_share_the_correction_limit(self):
        tools = FakeTools(
            {
                "implementation": [needs_correction("missing test", commits=1)],
                "issue correction": [committed()],
                "local gate correction": [committed()],
                "CI correction": [committed()],
            },
            gates=[GateFailure("unit tests", "1 failed")],
            ci=[CIFailure("lint failed"), CIFailure("lint failed again")],
        )

        result, _ = cycle(tools)

        self.assert_handed_off(
            tools, result,
            "correction limit of 3 exhausted before CI correction",
            "lint failed again",
        )
        self.assertEqual(
            tools.roles(),
            ["implementation", "issue correction", "local gate correction", "CI correction"],
        )

    def test_correction_limit_handoff_keeps_the_remaining_findings(self):
        tools = FakeTools({
            "implementation": [needs_correction("gap 1", commits=1)],
            "issue correction": [
                needs_correction("gap 2", commits=1),
                needs_correction("gap 3", commits=1),
                needs_correction("gap 4", commits=1),
            ],
        })

        result, _ = cycle(tools)

        self.assert_handed_off(
            tools, result,
            "correction limit of 3 exhausted before issue correction",
            "- gap 4",
        )

    def test_run_budget_stops_before_the_review_session(self):
        tools = FakeTools({"implementation": [committed()]})

        result, budget = cycle(tools, review=True, budget=1)

        self.assert_handed_off(
            tools, result,
            "run budget exhausted before review",
            "run budget of 1 agent sessions was exhausted after 1 sessions",
        )
        self.assertEqual(budget.sessions, 1)

    def test_run_budget_stop_during_ci_correction_keeps_the_ci_diagnostic(self):
        tools = FakeTools(
            {"implementation": [committed()]}, ci=[CIFailure("lint failed")]
        )

        result, _ = cycle(tools, budget=1)

        self.assert_handed_off(
            tools, result, "run budget exhausted before CI correction", "lint failed"
        )

    def test_unavailable_ci_is_handed_off_without_a_session(self):
        tools = FakeTools(
            {"implementation": [committed()]},
            ci=[CIUnavailable("infrastructure retries exhausted", "checks never started")],
        )

        result, budget = cycle(tools)

        self.assert_handed_off(
            tools, result, "infrastructure retries exhausted", "checks never started"
        )
        self.assertEqual(budget.sessions, 1)

    def test_gate_correction_reporting_needs_correction_is_handed_off(self):
        tools = FakeTools(
            {
                "implementation": [committed()],
                "local gate correction": [needs_correction("cannot fix", commits=0)],
            },
            gates=[GateFailure("unit tests", "1 failed")],
        )

        result, _ = cycle(tools)

        self.assert_handed_off(
            tools, result,
            "local gate correction stopped with needs_correction: "
            "local gate correction summary",
            "- cannot fix",
        )


class RunStopTests(unittest.TestCase):
    def test_review_must_not_change_an_unchanged_implementation(self):
        tools = FakeTools({"implementation": [no_changes()], "review": [committed()]})

        with self.assertRaises(RunStop):
            cycle(tools, review=True)

        self.assertEqual(tools.handoffs, [])


if __name__ == "__main__":
    unittest.main()
