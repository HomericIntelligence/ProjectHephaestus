"""Declared abstraction contracts for the automation pipeline.

Import directly from this module — not via ``hephaestus.automation`` —
to avoid defeating the lazy-export design of the package __init__.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable


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
class PRDiscoveryProtocol(Protocol):
    """Structural contract for PR discovery (PRDiscovery, used by CIDriver).

    Full public surface of PRDiscovery (pr_discovery.py): resolve_viewer_login:58,
    discover_bot_prs:96, discover_failing_prs:183, is_bot_pr_mode:260,
    list_open_prs_remaining:279, pr_merge_state:375.
    """

    def resolve_viewer_login(self) -> str: ...
    def discover_bot_prs(self) -> dict[int, int]: ...
    def discover_failing_prs(self, *args: Any, **kwargs: Any) -> Any: ...
    def is_bot_pr_mode(self, issue_number: int, pr_number: int) -> bool: ...
    def list_open_prs_remaining(self) -> list[dict[str, Any]]: ...
    def pr_merge_state(self, pr_number: Any) -> tuple[str, str]: ...


@runtime_checkable
class StatusTrackerProtocol(Protocol):
    """Structural contract for the slot tracker (StatusTracker).

    Full public surface of StatusTracker (status_tracker.py): acquire_slot:31,
    release_slot:54, update_slot:68, get_status:82, get_active_count:92,
    wait_for_available:102, wait_all_complete:118, clear:134.
    """

    def acquire_slot(self, timeout: float | None = ...) -> int | None: ...
    def release_slot(self, slot_id: int) -> None: ...
    def update_slot(self, slot_id: int, status: str) -> None: ...
    def get_status(self) -> list[str | None]: ...
    def get_active_count(self) -> int: ...
    def wait_for_available(self, timeout: float | None = ...) -> bool: ...
    def wait_all_complete(self, timeout: float | None = ...) -> bool: ...
    def clear(self) -> None: ...


@runtime_checkable
class WorktreeManagerProtocol(Protocol):
    """Structural contract for git worktree lifecycle (WorktreeManager).

    Full public surface of WorktreeManager (worktree_manager.py): base_branch:107
    (property), refresh_base_branch:117, create_worktree:171, remove_worktree:552,
    get_worktree:605, cleanup_all:618, prune_worktrees:665, list_worktrees:699,
    ensure_branch_deleted:746.
    """

    @property
    def base_branch(self) -> str: ...
    def refresh_base_branch(self) -> str: ...
    def create_worktree(self, *args: Any, **kwargs: Any) -> Any: ...
    def remove_worktree(self, issue_number: int, force: bool = ...) -> None: ...
    def get_worktree(self, issue_number: int) -> Path | None: ...
    def cleanup_all(self, force: bool = ...) -> None: ...
    def prune_worktrees(self) -> None: ...
    def list_worktrees(self, *, raise_on_error: bool = ...) -> list[dict[str, str]]: ...
    def ensure_branch_deleted(self, branch_name: str) -> None: ...


@runtime_checkable
class PlannerStateProtocol(Protocol):
    """Structural contract for the planner-side state store.

    Full public surface of PlannerStateManager (planner_state.py): filter:79,
    get_cached_labels:167, prefetch_comments:178, get_cached_comments:209,
    has_existing_plan:224.
    """

    def filter(self) -> list[int]: ...
    def get_cached_labels(self, issue_number: int) -> list[str] | None: ...
    def prefetch_comments(self, issue_numbers: list[int]) -> None: ...
    def get_cached_comments(self, issue_number: int) -> list[dict[str, Any]] | None: ...
    def has_existing_plan(self, issue_number: int) -> bool: ...


@runtime_checkable
class ImplementerStateProtocol(Protocol):
    """Structural contract for the implementer-side state store.

    Full public surface of ImplementationStateManager (implementer_state.py):
    lock:54 (property), get_or_create:63, get:70, save:75, load_all:79.
    """

    @property
    def lock(self) -> Any: ...
    def get_or_create(self, issue_number: int) -> Any: ...
    def get(self, issue_number: int) -> Any | None: ...
    def save(self, state: Any) -> None: ...
    def load_all(self) -> None: ...


# The two state managers expose DISJOINT public surfaces (verified:
# planner=filter/get_cached_*; implementer=lock/get/save/load_all), so a single
# structural Protocol cannot match both. StateStoreProtocol is a real union
# TypeAlias — usable as a type hint (``def f(s: StateStoreProtocol)``) and in
# ``isinstance(x, (PlannerStateProtocol, ImplementerStateProtocol))``.
StateStoreProtocol: TypeAlias = "PlannerStateProtocol | ImplementerStateProtocol"
