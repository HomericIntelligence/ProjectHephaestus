# Justfile for ProjectHephaestus
# Standard ecosystem recipes wrapping pixi commands

default:
    @just --list

# Install dependencies and set up the development environment
bootstrap:
    pixi install
    pixi run pre-commit install

# Run unit tests
test *ARGS:
    pixi run pytest tests/unit {{ ARGS }}

# Run integration tests
test-integration *ARGS:
    pixi run pytest tests/integration --override-ini="addopts=" -v --strict-markers {{ ARGS }}

# Run linter
lint:
    pixi run ruff check src/hephaestus/ scripts/ tests/

# Run formatter
format:
    pixi run ruff format src/hephaestus/ scripts/ tests/

# Run type checking
typecheck:
    pixi run mypy src/hephaestus/

# Run dependency vulnerability scan
audit:
    pixi run --environment lint pip-audit

# Run all pre-commit hooks
pre-commit:
    pixi run pre-commit run --all-files

# Remove build artifacts and caches
clean:
    rm -rf build/ dist/ *.egg-info .coverage htmlcov/ .pytest_cache/ .mypy_cache/
