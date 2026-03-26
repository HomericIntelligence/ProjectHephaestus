# ProjectHephaestus — shared utilities for HomericIntelligence ecosystem

# === Default ===
default:
    @just --list

# === Development ===
test *ARGS:
    pixi run pytest tests/unit {{ARGS}}

test-integration *ARGS:
    pixi run pytest tests/integration --override-ini="addopts=" -v --strict-markers {{ARGS}}

lint:
    pixi run ruff check src/hephaestus scripts tests

format:
    pixi run ruff format src/hephaestus scripts tests

typecheck:
    pixi run mypy src/hephaestus/

# === Quality ===
check: lint typecheck test

pre-commit:
    pixi run pre-commit run --all-files

audit:
    pixi run --environment lint pip-audit

# === Build ===
build:
    pixi run python -m build

clean:
    rm -rf build/ dist/ *.egg-info .coverage coverage.xml htmlcov/ .mypy_cache/ .pytest_cache/
