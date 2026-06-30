"""Declared abstraction contracts for the automation pipeline.

Import directly from this module — not via ``hephaestus.automation`` —
to avoid defeating the lazy-export design of the package __init__.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ReviewerProtocol(Protocol):
    """Structural contract satisfied by all four reviewer classes.

    Verified: PRReviewer.run (pr_reviewer.py:396),
              AddressReviewer.run (address_review.py:350),
              AuditReviewer.run (audit_reviewer.py:197),
              PlanReviewer.run (plan_reviewer.py:99).
    """

    def run(self) -> Any: ...


@runtime_checkable
class StateStore(Protocol):
    """Structural contract for per-issue on-disk automation state stores.

    Captures the operations shared by the concrete state stores so callers
    can depend on the abstraction (DIP) rather than a concrete class.

    Conforming today:
        ArmingStateStore (arming_state.py) — path/load/save/clear over
        ``drive-green-armed-<n>.json`` dict records.

    The canonical on-disk layout and serialization for Pydantic-backed
    state is provided by ``_review_utils.save_state_file`` /
    ``load_state_file``; this Protocol documents the per-issue accessor
    surface those helpers back.
    """

    def path(self, issue_number: int) -> Path:
        """Return the on-disk path for ``issue_number``'s record."""
        ...

    def load(self, issue_number: int) -> Any:
        """Return the parsed record for ``issue_number`` or ``None``."""
        ...

    def save(self, issue_number: int, record: Any) -> None:
        """Persist ``record`` for ``issue_number``."""
        ...
