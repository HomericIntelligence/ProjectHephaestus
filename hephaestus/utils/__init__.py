"""Utility functions for ProjectHephaestus."""

# Import from helpers
from .helpers import (
    flatten_dict,
    get_proj_root,
    get_repo_root,
    human_readable_size,
    install_package,
    local_branch_exists,
    resolve_repo_root,
    run_subprocess,
    slugify,
)

# Import from retry
from .retry import (
    is_network_error,
    retry_on_network_error,
    retry_with_backoff,
    retry_with_jitter,
)

# Import from terminal
from .terminal import (
    install_signal_handlers,
    restore_terminal,
    terminal_guard,
)

__all__ = [
    "flatten_dict",
    "get_proj_root",
    "get_repo_root",
    "human_readable_size",
    "install_package",
    "install_signal_handlers",
    "is_network_error",
    "local_branch_exists",
    "resolve_repo_root",
    "restore_terminal",
    "retry_on_network_error",
    "retry_with_backoff",
    "retry_with_jitter",
    "run_subprocess",
    "slugify",
    "terminal_guard",
]
