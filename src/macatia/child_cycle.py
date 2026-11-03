"""Child cycle: one runnable child from its implementation session to a verified head.

The cycle owns the correction limit, the child session limit, the review session,
and every handoff to human review. Agent sessions, local gates, CI, and GitHub
reach it through `ChildCycleAdapters`, so it runs the same against the real
tools and against in-memory fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from .sanitize import format_findings


MAX_CORRECTIONS = 3
# The visible text of every handoff comment starts with this, so a stopped run can link
# the latest one, including those posted before the handoff marker existed.
HANDOFF_PREFIX = "Macatia stopped"


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
    # (child, gate base, head_sha) -> None when every local gate that applies to the
    # paths changed since the gate base passes.
    run_local_gates: Callable[[int, str, str], GateFailure | None]
    # (child, gate base, head_sha) -> None once head_sha is published and every CI
    # check required by the paths changed since the gate base is green.
    verify_ci: Callable[[int, str, str], CIFailure | CIUnavailable | None]
    # (child, comment body)
    hand_to_human_review: Callable[[int, str], None]


@dataclass(frozen=True)
class Verified:
    implementation_head: str
    verified_head: str


class HandoffCause(Enum):
    """Why a child cycle handed its child to human review, when the child itself is not the cause."""

    CHILD_SESSION_LIMIT = "child session limit"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class HandedToHumanReview:
    reason: str
    # None when the child's work stopped the cycle.
    cause: HandoffCause | None = None


class SessionTotal:
    """The agent sessions one run started across its children, reported but never capped."""

    def __init__(self) -> None:
        self.count = 0


class ChildSessionLimit:
    """Bounds the agent sessions one child receives in one run; each run gives a fresh count."""

    def __init__(self, maximum: int, total: SessionTotal | None = None):
        self.maximum = maximum
        self.sessions = 0
        self.total = total if total is not None else SessionTotal()

    def start(self, role: str) -> None:
        if self.sessions >= self.maximum:
            raise ChildSessionLimitExhausted(role)
        self.sessions += 1
        self.total.count += 1


class ChildSessionLimitExhausted(Exception):
    def __init__(self, role: str):
        super().__init__(f"child session limit exhausted before {role}")
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


class ChildWorkRejected(Exception):
    """Raised by an adapter when the child's own work breaks Macatia's contract."""


class HumanReviewStop(Exception):
    def __init__(self, reason: str, body: str, cause: HandoffCause | None = None):
        super().__init__(reason)
        self.body = body
        self.cause = cause


@dataclass
class CorrectionLimit:
    """Bounds every correction session of one child: issue, local gate, and CI."""

    session_limit: ChildSessionLimit
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
            self.session_limit.start(role)
        except ChildSessionLimitExhausted as error:
            error.diagnostic = diagnostic or format_findings(findings)
            raise
        self.used += 1


def run_child_cycle(
    child: int,
    base_sha: str,
    session_limit: ChildSessionLimit,
    adapters: ChildCycleAdapters,
    *,
    review: bool,
    gate_base: str | None = None,
) -> Verified | HandedToHumanReview:
    """Take one child to a verified head, or hand it to human review and say why.

    Local gates and required CI checks are selected from the paths changed since
    `gate_base`, which defaults to the child base `base_sha`.
    """
    cause = None
    try:
        return _ChildCycle(child, base_sha, session_limit, adapters, review, gate_base).run()
    except ChildSessionLimitExhausted as error:
        cause = HandoffCause.CHILD_SESSION_LIMIT
        mode = "reviewed" if review else "standard"
        body = (
            f"Macatia stopped before {error.role} because the child session limit of "
            f"{session_limit.maximum} ({mode} run) was exhausted. "
            f"This child remains open for human review.{_diagnostic_detail(error.diagnostic)}"
        )
        reason = str(error)
    except CorrectionLimitExhausted as error:
        body = (
            f"Macatia stopped before {error.role} because the correction limit of "
            f"{MAX_CORRECTIONS} was exhausted. This child remains open for human review."
            f"{_findings_detail(error.findings)}{_diagnostic_detail(error.diagnostic)}"
        )
        reason = str(error)
    except AgentOutcomeStop as error:
        body = (
            f"Macatia stopped because {error.role} reported {error.outcome}: "
            f"{error.summary} This child remains open for human review."
            f"{_findings_detail(error.findings)}"
        )
        reason = str(error)
    except HumanReviewStop as error:
        body = error.body
        reason = str(error)
        cause = error.cause
    except ChildWorkRejected as error:
        reason = str(error)
        body = f"Macatia stopped because {reason}. This child remains open for human review."
    adapters.hand_to_human_review(child, body)
    return HandedToHumanReview(reason, cause)


def _findings_detail(findings: list[str]) -> str:
    return f"\n\nRemaining findings:\n{format_findings(findings)}" if findings else ""


def _diagnostic_detail(diagnostic: str | None) -> str:
    return f"\n\nLast diagnostic:\n```text\n{diagnostic}\n```" if diagnostic else ""


class _ChildCycle:
    def __init__(
        self,
        child: int,
        base_sha: str,
        session_limit: ChildSessionLimit,
        adapters: ChildCycleAdapters,
        review: bool,
        gate_base: str | None = None,
    ):
        self.child = child
        self.base_sha = base_sha
        self.gate_base = gate_base or base_sha
        self.session_limit = session_limit
        self.adapters = adapters
        self.review = review
        self.corrections = CorrectionLimit(session_limit)

    def run(self) -> Verified:
        self.session_limit.start("implementation")
        implementation = self.session(SessionRequest("implementation", self.child))
        verified_head = self.complete(implementation)
        while True:
            verdict = self.adapters.verify_ci(self.child, self.gate_base, verified_head)
            if verdict is None:
                return Verified(implementation.head, verified_head)
            if isinstance(verdict, CIUnavailable):
                raise HumanReviewStop(
                    verdict.reason,
                    "Macatia stopped after bounded infrastructure retries without changing the "
                    "implementation or consuming another agent session. This child remains open "
                    f"for human review. Last infrastructure diagnostic: {verdict.diagnostic}",
                    HandoffCause.INFRASTRUCTURE,
                )
            try:
                verified_head = self.correct_ci(verdict.diagnostic)
            except ChildSessionLimitExhausted as error:
                error.diagnostic = error.diagnostic or verdict.diagnostic
                raise

    def correct_ci(self, diagnostic: str) -> str:
        self.corrections.start("CI correction", diagnostic=diagnostic)
        correction = self.correct(
            SessionRequest(
                "CI correction",
                self.child,
                correction=self.corrections.used,
                diagnostic=diagnostic,
            )
        )
        return self.complete(correction)

    def correct(self, request: SessionRequest, findings: list[str] | None = None) -> SessionResult:
        """Run a correction session, which must create a new commit."""
        result = self.session(request)
        if not result.commits:
            role = request.role
            raise HumanReviewStop(
                f"{role[0].upper()}{role[1:]} did not create a new commit",
                f"Macatia stopped because the {role} did not create a new commit; a "
                "correction session must commit its repair. This child remains open "
                "for human review."
                f"{_findings_detail(result.findings or findings or [])}"
                f"{_diagnostic_detail(request.diagnostic)}",
            )
        return result

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
        while True:
            if result.outcome == "needs_correction":
                findings = result.findings
                self.corrections.start("issue correction", findings=findings)
                try:
                    result = self.correct(
                        SessionRequest(
                            "issue correction",
                            self.child,
                            correction=self.corrections.used,
                            findings=tuple(findings),
                        ),
                        findings,
                    )
                except AgentOutcomeStop as error:
                    if not error.findings:
                        error.findings = findings
                    raise
                head_sha = result.head
                continue

            head_sha = self.pass_local_gates(head_sha)
            if not self.review:
                return head_sha
            self.session_limit.start("review")
            result = self.session(
                SessionRequest(
                    "review", self.child, base_sha=self.base_sha, head_sha=head_sha
                )
            )
            reviewed_head = result.head
            if result.outcome == "needs_correction":
                head_sha = reviewed_head
                continue
            if result.commits:
                head_sha = self.pass_local_gates(reviewed_head)
                if head_sha != reviewed_head:
                    # A local gate correction changed the reviewed code. Review it again.
                    continue
            return reviewed_head

    def pass_local_gates(self, head_sha: str) -> str:
        while True:
            failure = self.adapters.run_local_gates(self.child, self.gate_base, head_sha)
            if failure is None:
                return head_sha
            self.corrections.start("local gate correction", diagnostic=failure.diagnostic)
            head_sha = self.correct(
                SessionRequest(
                    "local gate correction",
                    self.child,
                    correction=self.corrections.used,
                    gate=failure.description,
                    diagnostic=failure.diagnostic,
                )
            ).head
