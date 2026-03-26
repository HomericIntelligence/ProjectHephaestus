# ProjectHephaestus task runner
# Convention: justfile delegates to pixi tasks

default:
    @just --list

# === Test ===

# Run tests (accepts optional args, e.g. `just test tests/unit/`)
test *ARGS:
    pixi run pytest {{ ARGS }}

# === Code Quality ===

# Run linter
lint *ARGS:
    pixi run lint {{ ARGS }}

# Run formatter
format:
    pixi run format

# Run type checker
typecheck:
    pixi run mypy hephaestus/

# === Security ===

# Run dependency audit
audit:
    pixi run audit

# === Checks ===

# Run pre-commit hooks on all files
pre-commit:
    pixi run pre-commit run --all-files
