#!/usr/bin/env python3
"""Delegation shim — canonical implementation lives in ``hephaestus.validation.python_version``.

All business logic has been consolidated into the canonical module.
This shim exists so ``scripts/check_python_version_consistency.py`` and
existing callers that use the string-signature ``extract_pyproject_versions``
continue to work without changes.
"""

from hephaestus.validation.python_version import (  # noqa: F401
    check_ci_matrix_coverage,
    check_pixi_python_ceiling,
    check_project_version_consistency,
    check_python_version_consistency,
    extract_ci_matrix_python_versions,
    extract_classifiers_python_versions,
    extract_pixi_python_ceiling,
    extract_pixi_workspace_version,
    extract_project_version,
    main,
)
