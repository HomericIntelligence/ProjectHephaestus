"""Verify that all four reviewer classes satisfy ReviewerProtocol."""

from pathlib import Path

from hephaestus.automation._interfaces import ReviewerProtocol, StateStore
from hephaestus.automation.arming_state import ArmingStateStore


class _ConcreteReviewer:
    """Stub reviewer that satisfies the protocol."""

    def run(self) -> dict:
        """Return an empty result mapping."""
        return {}


class _MissingReviewer:
    """Stub that does not implement run()."""


def test_protocol_satisfied_by_stub() -> None:
    """A class defining run() satisfies ReviewerProtocol."""
    assert isinstance(_ConcreteReviewer(), ReviewerProtocol)


def test_protocol_violated_by_stub() -> None:
    """A class missing run() does not satisfy ReviewerProtocol."""
    assert not isinstance(_MissingReviewer(), ReviewerProtocol)


def test_pr_reviewer_satisfies_protocol() -> None:
    """PRReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.pr_reviewer import PRReviewer

    assert issubclass(PRReviewer, ReviewerProtocol)


def test_address_reviewer_satisfies_protocol() -> None:
    """AddressReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.address_review import AddressReviewer

    assert issubclass(AddressReviewer, ReviewerProtocol)


def test_audit_reviewer_satisfies_protocol() -> None:
    """AuditReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.audit_reviewer import AuditReviewer

    assert issubclass(AuditReviewer, ReviewerProtocol)


def test_plan_reviewer_satisfies_protocol() -> None:
    """PlanReviewer satisfies ReviewerProtocol structurally."""
    from hephaestus.automation.plan_reviewer import PlanReviewer

    assert issubclass(PlanReviewer, ReviewerProtocol)


def test_arming_state_store_satisfies_state_store_protocol() -> None:
    """ArmingStateStore satisfies StateStore structurally (path/load/save)."""
    assert issubclass(ArmingStateStore, StateStore)


def test_arming_state_store_roundtrip_via_canonical_loader(tmp_path: Path) -> None:
    """save/load/clear round-trips an arming record after the helper swap."""
    store = ArmingStateStore(lambda: tmp_path)
    store.save(42, {"head_sha": "abc", "armed": True})
    assert store.load(42) == {"head_sha": "abc", "armed": True}
    store.clear(42)
    assert store.load(42) is None


def test_arming_state_store_malformed_record_returns_none(tmp_path: Path) -> None:
    """A malformed record file is logged and ignored, returning None."""
    store = ArmingStateStore(lambda: tmp_path)
    (tmp_path / "drive-green-armed-7.json").write_text("{not json")
    assert store.load(7) is None
