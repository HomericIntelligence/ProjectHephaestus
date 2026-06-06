"""Unit tests for hephaestus.validation.repo_analyze_skills."""

from __future__ import annotations

from pathlib import Path

from hephaestus.validation.repo_analyze_skills import main
from hephaestus.validation.skill_catalog import (
    _discover_skill_names,
    check_skill_frontmatter,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / "skills"
EXPECTED_VARIANTS = {
    "repo-analyze",
    "repo-analyze-full",
    "repo-analyze-quick",
    "repo-analyze-quick-full",
    "repo-analyze-strict",
    "repo-analyze-strict-full",
}


def test_check_mode_passes_on_clean_tree() -> None:
    """--check returns 0 against committed SKILL.md files."""
    assert main(["--check"]) == 0


def test_generator_is_idempotent() -> None:
    """Running --write twice produces no diff on the second run."""
    assert main(["--write"]) == 0
    assert main(["--check"]) == 0


def test_check_mode_fails_on_partial_tampering(capsys) -> None:
    """If a partial drifts from the rendered SKILL.md, --check exits 1."""
    partial = SKILLS_DIR / "_repo_analyze_common" / "principles.md"
    backup = partial.read_text()
    try:
        partial.write_text(backup + "<!-- tamper -->\n")
        assert main(["--check"]) == 1
        err = capsys.readouterr().err
        assert "Drift detected in" in err
    finally:
        partial.write_text(backup)


def test_all_six_variants_render() -> None:
    """variants.yaml produces all 6 SKILL.md files at the expected paths."""
    for name in EXPECTED_VARIANTS:
        assert (SKILLS_DIR / name / "SKILL.md").is_file(), name


def test_rendered_frontmatter_name_matches_directory() -> None:
    """Guards against the check_skill_frontmatter regression at skill_catalog.py:166-170."""
    for name in EXPECTED_VARIANTS:
        skill_md = (SKILLS_DIR / name / "SKILL.md").read_text()
        assert f"\nname: {name}\n" in skill_md, name


def test_rendered_skills_pass_existing_catalog_validation() -> None:
    """Generated skills introduce no check_skill_frontmatter errors."""
    errors = check_skill_frontmatter(SKILLS_DIR)
    for name in EXPECTED_VARIANTS:
        assert name not in errors, f"{name}: {errors.get(name)}"


def test_private_common_dir_not_discovered_as_skill() -> None:
    """_repo_analyze_common/ has no SKILL.md and must be excluded from discovery."""
    names = _discover_skill_names(SKILLS_DIR)
    assert "_repo_analyze_common" not in names
    assert EXPECTED_VARIANTS.issubset(names)


def test_rendered_skill_md_files_use_lf_line_endings() -> None:
    """Generator must emit LF, never CRLF, to keep diffs stable across platforms."""
    for name in EXPECTED_VARIANTS:
        raw = (SKILLS_DIR / name / "SKILL.md").read_bytes()
        assert b"\r\n" not in raw, f"{name}: CRLF detected"
