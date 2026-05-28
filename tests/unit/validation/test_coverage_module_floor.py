"""Tests for per-module coverage floor enforcement."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hephaestus.validation.coverage import parse_module_coverage


@pytest.fixture()
def sample_coverage_xml(tmp_path: Path) -> Path:
    """Create a sample Cobertura coverage XML with two modules."""
    coverage_file = tmp_path / "coverage.xml"
    # Create XML with two <class> entries
    root = ET.Element("coverage")
    root.set("line-rate", "0.85")
    root.set("branch-rate", "0.80")

    # Module above floor (95% branch rate)
    class1 = ET.SubElement(root, "class")
    class1.set("filename", "hephaestus/utils/helpers.py")
    class1.set("line-rate", "0.96")
    class1.set("branch-rate", "0.95")

    # Module below floor (60% branch rate)
    class2 = ET.SubElement(root, "class")
    class2.set("filename", "hephaestus/validation/schema.py")
    class2.set("line-rate", "0.94")
    class2.set("branch-rate", "0.60")

    tree = ET.ElementTree(root)
    tree.write(coverage_file, encoding="utf-8", xml_declaration=True)
    return coverage_file


class TestParseModuleCoverage:
    """Tests for parse_module_coverage()."""

    def test_parses_module_coverage(self, sample_coverage_xml: Path) -> None:
        """Parses per-module branch and line rates from Cobertura XML."""
        modules = parse_module_coverage(sample_coverage_xml)
        assert len(modules) == 2
        assert modules["hephaestus/utils/helpers.py"] == (96.0, 95.0)
        assert modules["hephaestus/validation/schema.py"] == (94.0, 60.0)

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        """Missing coverage file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            parse_module_coverage(tmp_path / "nonexistent.xml")

    def test_missing_defusedxml_raises(self, sample_coverage_xml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing defusedxml raises RuntimeError."""
        import sys

        # Mock defusedxml as unavailable
        import builtins

        real_import = builtins.__import__

        def mock_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "defusedxml.ElementTree":
                raise ModuleNotFoundError(f"No module named '{name}'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        with pytest.raises(RuntimeError, match="defusedxml not installed"):
            parse_module_coverage(sample_coverage_xml)

    def test_invalid_xml_raises(self, tmp_path: Path) -> None:
        """Invalid XML raises RuntimeError."""
        bad_xml = tmp_path / "bad.xml"
        bad_xml.write_text("<invalid xml")
        with pytest.raises(RuntimeError, match="Error parsing coverage file"):
            parse_module_coverage(bad_xml)

    def test_handles_missing_rates(self, tmp_path: Path) -> None:
        """Handles classes with missing rate attributes gracefully."""
        coverage_file = tmp_path / "coverage.xml"
        root = ET.Element("coverage")
        # Class with no branch-rate attribute
        class1 = ET.SubElement(root, "class")
        class1.set("filename", "hephaestus/test_module.py")
        class1.set("line-rate", "0.85")
        # No branch-rate set

        tree = ET.ElementTree(root)
        tree.write(coverage_file, encoding="utf-8", xml_declaration=True)

        modules = parse_module_coverage(coverage_file)
        assert modules["hephaestus/test_module.py"] == (85.0, 0.0)

    def test_returns_empty_dict_for_no_classes(self, tmp_path: Path) -> None:
        """Coverage file with no <class> elements returns empty dict."""
        coverage_file = tmp_path / "coverage.xml"
        root = ET.Element("coverage")
        root.set("line-rate", "0.85")

        tree = ET.ElementTree(root)
        tree.write(coverage_file, encoding="utf-8", xml_declaration=True)

        modules = parse_module_coverage(coverage_file)
        assert modules == {}
