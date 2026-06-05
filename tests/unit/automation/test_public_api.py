"""Pin the public re-export surface of hephaestus.automation.

Regression guard for issue #799: the package must re-export every reviewer
class and option model at the root, so consumers do not need to reach into
private-looking submodules.
"""

from __future__ import annotations

import importlib

import hephaestus.automation as automation

EXPECTED_PUBLIC_SYMBOLS = {
    "AddressReviewOptions": "hephaestus.automation.models",
    "AddressReviewer": "hephaestus.automation.address_review",
    "CIDriver": "hephaestus.automation.ci_driver",
    "CIDriverOptions": "hephaestus.automation.models",
    "DependencyResolver": "hephaestus.automation.dependency_resolver",
    "ImplementerOptions": "hephaestus.automation.models",
    "IssueImplementer": "hephaestus.automation.implementer",
    "IssueInfo": "hephaestus.automation.models",
    "PRReviewer": "hephaestus.automation.pr_reviewer",
    "PlanReviewer": "hephaestus.automation.plan_reviewer",
    "PlanReviewerOptions": "hephaestus.automation.models",
    "Planner": "hephaestus.automation.planner",
    "PlannerOptions": "hephaestus.automation.models",
    "ReviewerOptions": "hephaestus.automation.models",
}


def test_all_matches_expected() -> None:
    """Assert __all__ exactly matches the expected public API."""
    assert set(automation.__all__) == set(EXPECTED_PUBLIC_SYMBOLS)


def test_every_all_entry_resolves_via_package_root() -> None:
    """Assert every export is accessible and points to the correct source module."""
    for name, source_module in EXPECTED_PUBLIC_SYMBOLS.items():
        value = getattr(automation, name)
        source = importlib.import_module(source_module)
        assert value is getattr(source, name), (
            f"hephaestus.automation.{name} is not the same object as "
            f"{source_module}.{name}"
        )


def test_dir_includes_all_entries() -> None:
    """Assert dir() introspection includes all public exports."""
    listing = dir(automation)
    for name in EXPECTED_PUBLIC_SYMBOLS:
        assert name in listing, f"{name!r} missing from dir(hephaestus.automation)"
