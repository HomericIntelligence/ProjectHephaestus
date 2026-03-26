# Backwards Compatibility Policy

## Current Status (v0.x)

ProjectHephaestus is in **pre-1.0 development**. During the v0.x series:

- **Minor versions** (0.x.0) may include breaking changes to the public API
- **Patch versions** (0.x.y) are backwards-compatible bug fixes and security patches
- All breaking changes are documented in [CHANGELOG.md](CHANGELOG.md)

## Public API Surface

The public API consists of symbols exported by `hephaestus.__all__`:

- `ContextLogger`
- `ensure_directory`
- `get_logger`
- `get_system_info`
- `load_config`
- `retry_with_backoff`
- `setup_logging`
- `slugify`

Additional symbols accessible via `hephaestus.<name>` (listed in `_LAZY_IMPORTS`)
are considered **stable but secondary** — they will not be removed without a
deprecation notice in at least one minor release.

Subpackage internals (functions not re-exported via `__init__.py`) may change
without notice between minor versions.

## Planned v1.0

When ProjectHephaestus reaches v1.0:

- The public API surface will be frozen under [Semantic Versioning](https://semver.org/)
- Breaking changes will only occur in major version bumps
- Deprecation warnings will precede removal by at least one minor release cycle

## Python Version Support

ProjectHephaestus supports Python 3.10 and later. Dropping support for a Python
version is considered a breaking change and will be noted in the changelog.
