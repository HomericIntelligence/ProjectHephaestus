"""Unit tests for hephaestus.automation.pr_discovery.PRDiscovery."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.models import CIDriverOptions
from hephaestus.automation.pr_discovery import PRDiscovery

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_options(**kwargs: Any) -> CIDriverOptions:
    defaults: dict[str, Any] = {
        "issues": [],
        "max_workers": 1,
        "dry_run": False,
        "enable_ui": False,
        "enable_advise": False,
        "include_all_authors": False,
        "include_bot_prs": True,
        "prs": [],
    }
    defaults.update(kwargs)
    return CIDriverOptions(**defaults)


@pytest.fixture
def shared_box() -> dict[str, Any]:
    """Mutable box wrapping the shared_pr_issues dict for setter/getter lambdas."""
    return {"v": {}}


@pytest.fixture
def discovery(shared_box: dict[str, Any], tmp_path: Path) -> PRDiscovery:
    """PRDiscovery instance with a mutable-box shared_pr_issues provider."""

    def _setter(d: dict[int, list[int]]) -> None:
        shared_box["v"].clear()
        shared_box["v"].update(d)

    with patch("hephaestus.automation.pr_discovery.get_repo_root", return_value=tmp_path):
        d = PRDiscovery(
            options=_make_options(),
            shared_pr_issues_setter=_setter,
            shared_pr_issues_getter=lambda: shared_box["v"],
        )
    return d


# ---------------------------------------------------------------------------
# _resolve_viewer_login
# ---------------------------------------------------------------------------


class TestResolveViewerLogin:
    """Tests for viewer-login lazy caching."""

    def test_caches_on_first_call(self, discovery: PRDiscovery) -> None:
        """Second call should NOT hit gh api again."""
        mock_result = MagicMock(stdout="testuser")
        with patch(
            "hephaestus.automation.pr_discovery._gh_call", return_value=mock_result
        ) as mock_gh:
            login1 = discovery._resolve_viewer_login()
            login2 = discovery._resolve_viewer_login()

        assert login1 == "testuser"
        assert login2 == "testuser"
        mock_gh.assert_called_once()

    def test_raises_on_gh_failure(self, discovery: PRDiscovery) -> None:
        """CalledProcessError propagates as RuntimeError with guidance text."""
        with patch(
            "hephaestus.automation.pr_discovery._gh_call",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            with pytest.raises(RuntimeError, match="gh auth login"):
                discovery._resolve_viewer_login()

    def test_raises_on_empty_login(self, discovery: PRDiscovery) -> None:
        """Empty stdout also raises RuntimeError (not silently returns blank)."""
        mock_result = MagicMock(stdout="")
        with patch("hephaestus.automation.pr_discovery._gh_call", return_value=mock_result):
            with pytest.raises(RuntimeError, match="empty response"):
                discovery._resolve_viewer_login()

    def test_viewer_login_attribute_settable(self, discovery: PRDiscovery) -> None:
        """Tests can pre-set _viewer_login to avoid live gh calls."""
        discovery._viewer_login = "precached"
        with patch("hephaestus.automation.pr_discovery._gh_call") as mock_gh:
            login = discovery._resolve_viewer_login()
        assert login == "precached"
        mock_gh.assert_not_called()


# ---------------------------------------------------------------------------
# _is_bot_pr_mode
# ---------------------------------------------------------------------------


class TestIsBotPrMode:
    """Tests for synthetic-issue bot-PR detection."""

    def test_returns_true_when_issue_equals_pr(self, discovery: PRDiscovery) -> None:
        assert discovery._is_bot_pr_mode(42, 42) is True

    def test_returns_false_when_different(self, discovery: PRDiscovery) -> None:
        assert discovery._is_bot_pr_mode(1, 42) is False


# ---------------------------------------------------------------------------
# _discover_bot_prs
# ---------------------------------------------------------------------------


class TestDiscoverBotPrs:
    """Tests for bot-PR enumeration via REST /pulls."""

    def _make_pulls_response(self, pulls: list[dict[str, Any]]) -> MagicMock:
        m = MagicMock()
        m.stdout = json.dumps(pulls)
        return m

    def test_returns_empty_on_gh_failure(self, discovery: PRDiscovery) -> None:
        with (
            patch(
                "hephaestus.automation.pr_discovery.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ),
        ):
            result = discovery._discover_bot_prs()
        assert result == {}

    def test_returns_empty_when_no_bot_prs(self, discovery: PRDiscovery) -> None:
        pulls = [{"number": 1, "user": {"type": "User", "login": "alice"}}]
        with (
            patch(
                "hephaestus.automation.pr_discovery.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=self._make_pulls_response(pulls),
            ),
        ):
            result = discovery._discover_bot_prs()
        assert result == {}

    def test_includes_bot_prs(self, discovery: PRDiscovery) -> None:
        discovery.options.include_all_authors = True
        pulls = [
            {"number": 10, "user": {"type": "Bot", "login": "dependabot[bot]"}},
            {"number": 11, "user": {"type": "User", "login": "alice"}},
        ]
        with (
            patch(
                "hephaestus.automation.pr_discovery.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=self._make_pulls_response(pulls),
            ),
        ):
            result = discovery._discover_bot_prs()
        assert result == {10: 10}

    def test_skips_bots_not_owned_by_viewer_under_author_filter(
        self, discovery: PRDiscovery
    ) -> None:
        """Under the default author filter, bot PRs not owned by viewer are excluded."""
        discovery.options.include_all_authors = False
        discovery._viewer_login = "mybot[bot]"
        pulls = [
            {"number": 10, "user": {"type": "Bot", "login": "dependabot[bot]"}},
            {"number": 11, "user": {"type": "Bot", "login": "mybot[bot]"}},
        ]
        with (
            patch(
                "hephaestus.automation.pr_discovery.get_repo_info",
                return_value=("owner", "repo"),
            ),
            patch(
                "hephaestus.automation.pr_discovery._gh_call",
                return_value=self._make_pulls_response(pulls),
            ),
        ):
            result = discovery._discover_bot_prs()
        # Only the viewer-owned bot PR is included
        assert result == {11: 11}

    def test_returns_empty_on_repo_info_failure(self, discovery: PRDiscovery) -> None:
        with patch(
            "hephaestus.automation.pr_discovery.get_repo_info",
            side_effect=RuntimeError("no remote"),
        ):
            result = discovery._discover_bot_prs()
        assert result == {}


# ---------------------------------------------------------------------------
# _discover_prs — shared_pr_issues population and dedup
# ---------------------------------------------------------------------------


class TestDiscoverPrs:
    """Tests for main PR discovery orchestration."""

    def test_single_issue_populates_shared_mapping(
        self, discovery: PRDiscovery, shared_box: dict[str, Any]
    ) -> None:
        with patch.object(discovery, "_find_pr_for_issue", return_value=99):
            result = discovery._discover_prs([42])

        assert result == {42: 99}
        assert shared_box["v"] == {99: [42]}

    def test_dedup_by_pr_number_keeps_lowest_issue(
        self, discovery: PRDiscovery, shared_box: dict[str, Any]
    ) -> None:
        """Two issues resolving to same PR → only the lower issue is the canonical key."""

        def _find_pr(issue: int) -> int | None:
            return 99

        with patch.object(discovery, "_find_pr_for_issue", side_effect=_find_pr):
            result = discovery._discover_prs([50, 10])

        assert result == {10: 99}
        assert shared_box["v"][99] == [10, 50]

    def test_missing_pr_skipped(self, discovery: PRDiscovery) -> None:
        with patch.object(discovery, "_find_pr_for_issue", return_value=None):
            result = discovery._discover_prs([42])
        assert result == {}

    def test_direct_pr_mode_adds_pr_key(
        self, discovery: PRDiscovery, shared_box: dict[str, Any], tmp_path: Path
    ) -> None:
        """options.prs bypasses find_pr_for_issue and uses pr_number as key."""
        discovery.options.prs = [77]
        with (
            patch.object(discovery, "_find_pr_for_issue", return_value=None),
            patch.object(discovery, "_validate_pr_open", return_value=True),
        ):
            result = discovery._discover_prs([])

        assert result == {77: 77}
        assert 77 in shared_box["v"]

    def test_direct_pr_closed_is_skipped(self, discovery: PRDiscovery) -> None:
        discovery.options.prs = [77]
        with (
            patch.object(discovery, "_find_pr_for_issue", return_value=None),
            patch.object(discovery, "_validate_pr_open", return_value=False),
        ):
            result = discovery._discover_prs([])
        assert result == {}


# ---------------------------------------------------------------------------
# _validate_pr_open
# ---------------------------------------------------------------------------


class TestValidatePrOpen:
    """Tests for single-PR state validation."""

    def test_returns_true_for_open_pr(self, discovery: PRDiscovery) -> None:
        mock_result = MagicMock(stdout=json.dumps({"number": 5, "state": "OPEN"}))
        with patch("hephaestus.automation.pr_discovery._gh_call", return_value=mock_result):
            assert discovery._validate_pr_open(5) is True

    def test_returns_false_for_merged_pr(self, discovery: PRDiscovery) -> None:
        mock_result = MagicMock(stdout=json.dumps({"number": 5, "state": "MERGED"}))
        with patch("hephaestus.automation.pr_discovery._gh_call", return_value=mock_result):
            assert discovery._validate_pr_open(5) is False

    def test_returns_false_on_gh_error(self, discovery: PRDiscovery) -> None:
        with patch(
            "hephaestus.automation.pr_discovery._gh_call",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            assert discovery._validate_pr_open(5) is False
