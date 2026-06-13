"""Verify that all four reviewer classes satisfy ReviewerProtocol."""

from hephaestus.automation._interfaces import ReviewerProtocol


class _ConcreteReviewer:
    def run(self) -> dict:
        return {}


class _MissingReviewer:
    pass


def test_protocol_satisfied_by_stub() -> None:
    assert isinstance(_ConcreteReviewer(), ReviewerProtocol)


def test_protocol_violated_by_stub() -> None:
    assert not isinstance(_MissingReviewer(), ReviewerProtocol)


def test_pr_reviewer_satisfies_protocol() -> None:
    """PRReviewer inherits BaseReviewer and defines run() — must satisfy."""
    from hephaestus.automation.pr_reviewer import PRReviewer

    assert issubclass(PRReviewer, ReviewerProtocol)


def test_address_reviewer_satisfies_protocol() -> None:
    from hephaestus.automation.address_review import AddressReviewer

    assert issubclass(AddressReviewer, ReviewerProtocol)


def test_audit_reviewer_satisfies_protocol() -> None:
    from hephaestus.automation.audit_reviewer import AuditReviewer

    assert issubclass(AuditReviewer, ReviewerProtocol)


def test_plan_reviewer_satisfies_protocol() -> None:
    from hephaestus.automation.plan_reviewer import PlanReviewer

    assert issubclass(PlanReviewer, ReviewerProtocol)
