#!/usr/bin/env python3
"""Delegation shim — canonical implementation lives in ``hephaestus.validation.python_version``.

All business logic has been consolidated into the canonical module.
This shim exists so ``scripts/check_python_version_consistency.py`` and
existing callers that use the string-signature ``extract_pyproject_versions``
continue to work without changes.
"""

from hephaestus.validation.python_version import (
    check_ci_matrix_coverage as check_ci_matrix_coverage,
)
from hephaestus.validation.python_version import (
    check_pixi_python_ceiling as check_pixi_python_ceiling,
)
from hephaestus.validation.python_version import (
    check_project_version_consistency as check_project_version_consistency,
)
from hephaestus.validation.python_version import (
    check_python_version_consistency as check_python_version_consistency,
)
from hephaestus.validation.python_version import (
    extract_ci_matrix_python_versions as extract_ci_matrix_python_versions,
)
from hephaestus.validation.python_version import (
    extract_classifiers_python_versions as extract_classifiers_python_versions,
)
from hephaestus.validation.python_version import (
    extract_pixi_python_ceiling as extract_pixi_python_ceiling,
)
from hephaestus.validation.python_version import (
    extract_pixi_workspace_version as extract_pixi_workspace_version,
)
from hephaestus.validation.python_version import (
    extract_project_version as extract_project_version,
)
from hephaestus.validation.python_version import (  # noqa: F401
    extract_pyproject_versions_str as extract_pyproject_versions,
)
from hephaestus.validation.python_version import (
    main as main,
)
