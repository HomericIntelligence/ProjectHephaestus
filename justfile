# ProjectHephaestus justfile
# One-command developer experience for the HomericIntelligence ecosystem

# Configurable paths
src_dirs := "hephaestus scripts tests"
test_dir := "tests/unit"

# List available recipes
default:
    @just --list

# === Setup ===

# Install dependencies and configure pre-commit hooks
bootstrap:
    pixi install
    pixi run pre-commit install

# === Test ===

# Run unit tests (pass extra args: just test -v, just test -k test_slugify)
test *ARGS:
    pixi run pytest {{ test_dir }} {{ ARGS }}

# === Lint ===

# Check code with ruff linter
lint:
    pixi run ruff check {{ src_dirs }}

# Format code with ruff formatter
format:
    pixi run ruff format {{ src_dirs }}

# Run mypy type checking
typecheck:
    pixi run mypy hephaestus/

# Run pre-commit hooks on all files
pre-commit:
    pixi run pre-commit run --all-files

# === Security ===

# Run pip-audit dependency audit
audit:
    pixi run --environment lint pip-audit

# === CI ===

# Run lint, typecheck, and tests (full CI check)
check:
    just lint
    just typecheck
    just test
