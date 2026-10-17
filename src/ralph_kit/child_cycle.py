"""Child cycle: one runnable child from its implementation session to a verified head.

The cycle owns the correction limit, the run budget checks, the review session,
and every handoff to human review. Agent sessions, local gates, CI, and GitHub
reach it through `ChildCycleAdapters`, so it runs the same against the real
tools and against in-memory fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .sanitize import format_findings


MAX_CORRECTIONS = 3


@dataclass(frozen=True)
class SessionRequest:
    """What one agent session must do; the session adapter turns it into a prompt."""

    role: str
    child: int
    correction: int = 0
    findings: tuple[str, ...] = ()
    gate: str = ""
    diagnostic: str = ""
    base_sha: str = ""
    head_sha: str = ""


@dataclass(frozen=True)
class SessionResult:
    outcome: str
    summary: str
    findings: list[str]
    head: str
    commits: list[str]


@dataclass(frozen=True)
class GateFailure:
    description: str
    diagnostic: str


@dataclass(frozen=True)
class CIFailure:
    """A required CI check failed because of the child's work."""

    diagnostic: str


@dataclass(frozen=True)
class CIUnavailable:
    """CI evidence could not be obtained within the infrastructure retries."""

    reason: str
    diagnostic: str


@dataclass(frozen=True)
class ChildCycleAdapters:
    run_session: Callable[[SessionRequest], SessionResult]
    # (child, base_sha, head_sha) -> None when every applicable local gate passes.
    run_local_gates: Callable[[int, str, str], GateFailure | None]
    # (child, base_sha, head_sha) -> None once head_sha is published and verified.
    verify_ci: Callable[[int, str, str], CIFailure | CIUnavailable | None]
    # (child, comment body)
    hand_to_human_review: Callable[[int, str], None]


@dataclass(frozen=True)
class Verified:
    implementation_head: str
    verified_head: str


@dataclass(frozen=True)
class HandedToHumanReview:
    reason: str


class RunBudget:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.sessions = 0

    def start(self, role: str) -> None:
        if self.sessions >= self.maximum:
            raise RunBudgetExhausted(role)
        self.sessions += 1


class RunBudgetExhausted(Exception):
    def __init__(self, role: str):
        super().__init__(f"run budget exhausted before {role}")
        self.role = role
        self.diagnostic: str | None = None


class CorrectionLimitExhausted(Exception):
    def __init__(self, role: str, findings: list[str], diagnostic: str | None):
        super().__init__(f"correction limit of {MAX_CORRECTIONS} exhausted before {role}")
        self.role = role
        self.findings = findings
        self.diagnostic = diagnostic


class AgentOutcomeStop(Exception):
    """A valid agent result that deliberately leaves the child incomplete."""

    def __init__(
        self, role: str, outcome: str, summary: str, findings: list[str] | None = None
    ):
        super().__init__(f"{role} stopped with {outcome}: {summary}")
        self.role = role
        self.outcome = outcome
        self.summary = summary
        self.findings = findings or []


class RunStop(Exception):
    """Stops the whole run without changing the child's issue state."""


class HumanReviewStop(Exception):
    def __init__(self, reason: str, body: str):
        super().__init__(reason)
        self.body = body


@dataclass
class CorrectionLimit:
    """Bounds every correction session of one child: issue, local gate, and CI."""

    budget: RunBudget
    used: int = field(default=0)

    def start(
        self,
        role: str,
        *,
        findings: list[str] | None = None,
        diagnostic: str | None = None,
    ) -> None:
        findings = findings or []
        if self.used >= MAX_CORRECTIONS:
            raise CorrectionLimitExhausted(role, findings, diagnostic)
        try:
            self.budget.start(role)
        except RunBudgetExhausted as error:
            error.diagnostic = diagnostic or format_findings(findings)
            raise
        self.used += 1


def run_child_cycle(
    child: int,
    base_sha: str,
    budget: RunBudget,
    adapters: ChildCycleAdapters,
    *,
    review: bool,
) -> Verified | HandedToHumanReview:
    """Take one child to a verified head, or hand it to human review and say why."""
    try:
        return _ChildCycle(child, base_sha, budget, adapters, review).run()
    except RunBudgetExhausted as error:
        body = (
            f"Ralph stopped before {error.role} because the run budget of "
            f"{budget.maximum} agent sessions was exhausted after {budget.sessions} sessions. "
            f"This child remains open for human review.{_diagnostic_detail(error.diagnostic)}"
        )
        reason = str(error)
    except CorrectionLimitExhausted as error:
        body = (
            f"Ralph stopped before {error.role} because the correction limit of "
            f"{MAX_CORRECTIONS} was exhausted. This child remains open for human review."
            f"{_findings_detail(error.findings)}{_diagnostic_detail(error.diagnostic)}"
        )
        reason = str(error)
    except AgentOutcomeStop as error:
        body = (
            f"Ralph stopped because {error.role} reported {error.outcome}: "
            f"{error.summary} This child remains open for human review."
            f"{_findings_detail(error.findings)}"
        )
        reason = str(error)
    except HumanReviewStop as error:
        body = error.body
        reason = str(error)
    adapters.hand_to_human_review(child, body)
    return HandedToHumanReview(reason)


def _findings_detail(findings: list[str]) -> str:
    return f"\n\nRemaining findings:\n{format_findings(findings)}" if findings else ""


def _diagnostic_detail(diagnostic: str | None) -> str:
    return f"\n\nLast diagnostic:\n```text\n{diagnostic}\n```" if diagnostic else ""


class _ChildCycle:
    def __init__(
        self,
        child: int,
        base_sha: str,
        budget: RunBudget,
        adapters: ChildCycleAdapters,
        review: bool,
    ):
        self.child = child
        self.base_sha = base_sha
        self.budget = budget
        self.adapters = adapters
        self.review = review
        self.corrections = CorrectionLimit(budget)

    def run(self) -> Verified:
        self.budget.start("implementation")
        implementation = self.session(SessionRequest("implementation", self.child))
        verified_head = self.complete(implementation)
        while True:
            verdict = self.adapters.verify_ci(self.child, self.base_sha, verified_head)
            if verdict is None:
                return Verified(implementation.head, verified_head)
            if isinstance(verdict, CIUnavailable):
                raise HumanReviewStop(
                    verdict.reason,
                    "Ralph stopped after bounded infrastructure retries without changing the "
                    "implementation or consuming another agent session. This child remains open "
                    f"for human review. Last infrastructure diagnostic: {verdict.diagnostic}",
                )
            try:
                verified_head = self.correct_ci(verdict.diagnostic)
            except RunBudgetExhausted as error:
                error.diagnostic = error.diagnostic or verdict.diagnostic
                raise

    def correct_ci(self, diagnostic: str) -> str:
        self.corrections.start("CI correction", diagnostic=diagnostic)
        correction = self.session(
            SessionRequest(
                "CI correction",
                self.child,
                correction=self.corrections.used,
                diagnostic=diagnostic,
            )
        )
        if not correction.commits:
            detail = "a product CI failure requires a committed correction"
            if correction.findings:
                detail += "\nRemaining findings:\n" + format_findings(correction.findings)
            raise HumanReviewStop(
                "CI correction did not create a new commit",
                "Ralph stopped because the CI correction did not create a new commit. "
                f"This child remains open for human review. Diagnostic: {detail}",
            )
        return self.complete(correction)

    def session(self, request: SessionRequest) -> SessionResult:
        result = self.adapters.run_session(request)
        if result.outcome in {"blocked", "failed"} or (
            result.outcome == "needs_correction" and request.role == "local gate correction"
        ):
            raise AgentOutcomeStop(
                request.role, result.outcome, result.summary, result.findings
            )
        return result

    def complete(self, result: SessionResult) -> str:
        """Complete outstanding findings, pass local gates, and review if enabled."""
        head_sha = result.head
        require_unchanged_review = result.outcome == "no_changes"
        while True:
            if result.outcome == "needs_correction":
                require_unchanged_review = False
                findings = result.findings
                self.corrections.start("issue correction", findings=findings)
                try:
                    result = self.session(
                        SessionRequest(
                            "issue correction",
                            self.child,
                            correction=self.corrections.used,
                            findings=tuple(findings),
                        )
                    )
                except AgentOutcomeStop as error:
                    if not error.findings:
                        error.findings = findings
                    raise
                head_sha = result.head
                if not result.commits:
                    raise AgentOutcomeStop(
                        "issue correction",
                        "needs_correction",
                        "Issue correction did not create a new commit.",
                        result.findings or findings,
                    )
                continue

            head_sha = self.pass_local_gates(head_sha)
            if not self.review:
                return head_sha
            self.budget.start("review")
            result = self.session(
                SessionRequest(
                    "review", self.child, base_sha=self.base_sha, head_sha=head_sha
                )
            )
            reviewed_head = result.head
            if result.outcome == "needs_correction":
                head_sha = reviewed_head
                continue
            if require_unchanged_review and (
                result.outcome != "no_changes" or result.commits
            ):
                raise RunStop(
                    "implementation no_changes requires an independent review with no corrections"
                )
            if result.commits:
                head_sha = self.pass_local_gates(reviewed_head)
                if head_sha != reviewed_head:
                    # A local gate correction changed the reviewed code. Review it again.
                    continue
            return reviewed_head

    def pass_local_gates(self, head_sha: str) -> str:
        while True:
            failure = self.adapters.run_local_gates(self.child, self.base_sha, head_sha)
            if failure is None:
                return head_sha
            self.corrections.start("local gate correction", diagnostic=failure.diagnostic)
            head_sha = self.session(
                SessionRequest(
                    "local gate correction",
                    self.child,
                    correction=self.corrections.used,
                    gate=failure.description,
                    diagnostic=failure.diagnostic,
                )
            ).head
