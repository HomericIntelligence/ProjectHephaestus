# ProjectHephaestus — Architecture Design Document

> **Sub-issue of #81** — Comprehensive design doc synthesizing the src-layout
> migration, justfile tooling, code quality, CI, and documentation design
> decisions from issues #83–#88 into a single cohesive reference.

## 1. Vision and Purpose

ProjectHephaestus is the foundational utilities and tooling repository of the
HomericIntelligence ecosystem. Named after the Greek god of craftsmanship, it
provides the shared scripts, helpers, and infrastructure that support development
across all other repositories in the ecosystem.

**Core mission:** Centralize and maintain Python utilities, helper functions, and
common abstractions used throughout the HomericIntelligence suite, with an emphasis
on modularity, reliability, and consistency.

### Ecosystem Position

| Project | Role |
|---------|------|
| ProjectOdyssey | Training and capability development |
| ProjectKeystone | Automated task DAG execution |
| ProjectScylla | Testing, measurement, and optimization |
| ProjectMnemosyne | Knowledge, skills, and memory preservation |
| ProjectHermes | Agent communication and message routing |
| ProjectArgus | Observability, monitoring, and alerting |
| ProjectProteus | Dynamic configuration and environment adaptation |
| ProjectMyrmidons | Agent swarm coordination and task distribution |
| ProjectAchaeanFleet | Multi-agent fleet orchestration |
| **ProjectHephaestus** | **Shared utilities, tooling, and foundational components** |

## 2. Repository Structure

### 2.1 src-Layout Migration (Issues #83, #84, #85, #86, #88)

> **Status: PROPOSED** — Issues #83 and #88 describe a migration from the
> current flat layout (`hephaestus/` at root) to the standard Python
> src-layout (`src/hephaestus/`). This migration is designed but not yet
> implemented on `main`.

The proposed migration would move source code from `hephaestus/` to
`src/hephaestus/` to:

1. **Prevent accidental imports** from the working directory during development,
   ensuring tests always exercise the *installed* package rather than the source
   tree.
2. **Align with ecosystem conventions** — other HomericIntelligence repositories
   (ProjectMnemosyne, ProjectScylla) use src-layout.
3. **Simplify packaging** — `pyproject.toml` would declare
   `[tool.hatch.build.targets.wheel].packages = ["src/hephaestus"]`.

#### Current directory layout (as of 2026-06-12)

```text
ProjectHephaestus/
├── hephaestus/                  # Python source code (19 subpackages) — current location
│       ├── agents/              # Agent frontmatter + loader + runtime
│       ├── automation/          # Issue planning / implementation / PR review pipeline
│       ├── benchmarks/          # Benchmark comparison utilities
│       ├── ci/                  # CI helpers (precommit, workflows, docker timing)
│       ├── cli/                 # CLI argument parsing and output formatting
│       ├── config/              # Configuration management (YAML, JSON, env vars)
│       ├── datasets/            # Dataset downloading utilities
│       ├── discovery/           # Discovery of agents, skills, and code blocks
│       ├── forensics/           # Coredump capture + gdb post-mortem runner
│       ├── github/              # GitHub automation (PR merging, fleet sync, tidy)
│       ├── io/                  # I/O utilities (read, write, safe_write)
│       ├── logging/             # Logging utilities (ContextLogger, setup_logging)
│       ├── markdown/            # Markdown linting and link fixing
│       ├── nats/                # NATS JetStream subscriber
│       ├── resilience/          # Circuit breaker + retry primitives
│       ├── system/              # System information collection
│       ├── utils/               # General utility functions
│       ├── validation/          # README, schema, and structural validation
│       └── version/             # Version management (hatch-vcs)
├── scripts/                     # Automation and maintenance scripts
├── skills/                      # Claude Code skill definitions (23 entries)
├── tests/
│   ├── unit/                    # Unit tests (mirrors src/hephaestus/ structure)
│   └── integration/             # Integration tests
├── docs/
│   └── architecture/            # This document
├── justfile                     # Ecosystem-standard task runner
├── pixi.toml                    # Pixi environment configuration
├── pyproject.toml               # Python package configuration
├── .pre-commit-config.yaml      # Pre-commit hooks
└── .github/workflows/           # CI/CD workflows
```

#### Proposed path migration (Issues #83, #84, #86)

When implemented, all internal path references would be updated:

| Component | Current | After Migration |
|-----------|---------|------------------|
| Source code | `hephaestus/` | `src/hephaestus/` |
| Test discovery | `tests/` → `hephaestus/` | `tests/` → `src/hephaestus/` |
| Ruff targets | `hephaestus/ tests/` | `src/hephaestus/ tests/` |
| Mypy paths | `hephaestus/` | `src/hephaestus/` |
| Pre-commit hooks | `files: ^hephaestus/` | `files: ^src/hephaestus/` |
| Skill docs | `hephaestus/` references | `src/hephaestus/` references |
| Coverage source | `source = ["hephaestus"]` | `source = ["src/hephaestus"]` |

## 3. Build System and Tooling

### 3.1 Justfile (Issues #83, #85)

The `justfile` provides standardized ecosystem recipes. Issue #85 proposes
refactoring to use configurable path variables to reduce duplication and
support future path changes:

```just
# Configurable path variables (proposed in #85)
src_dirs := "hephaestus"       # would become "src/hephaestus" after migration
test_dir := "tests"
script_dir := "scripts"

# Standard ecosystem recipes
test:                    # Run unit tests
test-integration:        # Run integration tests
lint:                    # Run ruff linter
format:                  # Run ruff formatter
typecheck:               # Run mypy
check:                   # lint + format-check + typecheck
pre-commit:              # Run pre-commit hooks
audit:                   # Run repo-analyze audit
build:                   # Build wheel + sdist
clean:                   # Remove build artifacts
bootstrap:               # One-command dev setup (pixi install + dev-install + pre-commit)
```

### 3.2 Pixi Environment Management

`pixi.toml` manages the development environment. The developer environment is
**Linux-64 only** (`platforms = ["linux-64"]`). The published wheel supports
Linux, macOS, and Windows.

### 3.3 Version Management

The project uses **hatch-vcs dynamic versioning** — the package version is
derived from the latest `vX.Y.Z` git tag, not stored in any file. There is no
`[project].version` field in `pyproject.toml`.

## 4. Automation Pipeline Architecture

The `hephaestus.automation` subpackage drives a 3-stage issue/PR pipeline:

```text
plan → implement → drive-green
```

Each stage runs an external coding agent (Claude Code or Codex), selected via
the `--agent` CLI flag or auto-detected.

### 4.1 Pipeline Stages

| Stage | Module | Console Script | Purpose |
|-------|--------|----------------|---------|
| Plan | `planner` | `hephaestus-plan-issues` | Produce implementation plan for an issue |
| ↳ plan review | `plan_reviewer` (in-loop) | (internal) | Strict R0/R1/R2 review of the plan |
| Implement | `implementer` | `hephaestus-implement-issues` | Carry out plan in isolated git worktree |
| ↳ impl review | `_review_utils` (in-loop) | (internal) | Review the resulting diff |
| ↳ PR review | `pr_reviewer` (in-loop) | (internal) | Inline review of the open PR |
| ↳ address review | `address_review` (in-loop) | (internal) | Resolve review threads |
| Drive green | `ci_driver` | `hephaestus-merge-prs` | Poll CI, fix failures, enable auto-merge |

### 4.2 Agent Runtime

`hephaestus.agents.runtime` abstracts over Claude Code and Codex:

- `resolve_agent(agent)` — resolves and **validates authentication** of the
  selected backend (raises `RuntimeError` if not installed or not authenticated).
- `is_codex(agent_str)` — branches between providers.
- `run_codex_text/session/resume` — invoke Codex.
- Claude is invoked via `claude_invoke.invoke_claude_with_session`.

Per-agent timeouts are centralized in `claude_timeouts`, all tunable via
`HEPH_*` environment variables.

### 4.3 Prompt Safety

All untrusted GitHub content (issue bodies, PR diffs, reviewer comments, plan
text) is wrapped with `_fence_untrusted()` using random nonces, preventing
hostile content from forging verdict lines or injecting instructions that bypass
the review loop.

### 4.4 State Labels

The pipeline uses three `state:*` labels as the single source of truth:

| Label | Meaning |
|-------|---------|
| `state:needs-plan` | Issue is new; planner should run next loop |
| `state:plan-no-go` | Reviewer's latest verdict was NOGO |
| `state:plan-go` | Plan approved; implementer may proceed |

### 4.5 Worktree Management

Implementation runs in isolated git worktrees, enabling parallel work on
multiple issues without branch conflicts. The worktree manager handles:

- Creating worktrees from the current trunk
- Reusing existing worktrees for resumed work
- Cleaning up worktrees after completion
- Preserving dirty worktrees when reuse is safe

## 5. Plugin Architecture

ProjectHephaestus ships as both a Claude Code plugin and a Codex plugin,
providing 23 reusable skills to any repository in the ecosystem.

### 5.1 Skill Catalog

Skills use kebab-case naming per the Claude Code plugin format. Each skill has
a `SKILL.md` with YAML frontmatter declaring allowed tools, hooks, and
permissions.

**Workflow skills:** `skill-advisor`, `advise`, `learn`, `brainstorm`,
`test-driven-development`, `systematic-debugging`, `verification`,
`finish-branch`, `code-review`.

**Audit skills:** `repo-analyze` and its `-quick`, `-strict`, `-full`,
`-quick-full`, `-strict-full` variants. `review-pr-strict`.

**Worktree skills:** `git-worktrees`, `worktree-cleanup`, `tidy`.

**Orchestration:** `myrmidon-swarm` for hierarchical multi-agent fan-out.

### 5.2 Plugin Installation

```bash
# Claude Code
claude plugin install HomericIntelligence/ProjectHephaestus

# Codex
codex plugin marketplace add HomericIntelligence/ProjectHephaestus --ref main
codex plugin add hephaestus@project-hephaestus
```

## 6. CI/CD Architecture

### 6.1 Required CI Gates

Every PR must pass these gates (enforced in `.github/workflows/_required.yml`):

1. **pr-policy** — signed commits, `Closes #N` line, auto-merge armed
2. **lint** — ruff check, ruff format, mypy
3. **unit-tests** — pytest with 80% coverage gate
4. **integration-tests** — integration test suite
5. **shell-tests** — BATS shell tests
6. **pre-commit** — all pre-commit hooks pass
7. **secrets-scan** — gitleaks
8. **dependency-scan** — pip-audit

### 6.2 Release Pipeline

```text
workflow_dispatch (Auto Tag Release)
  └─ computes next vX.Y.Z
  └─ GPG-signed git tag + push
       └─ triggers Release workflow
            ├─ test job (pytest)
            ├─ type-check job (mypy)
            └─ build-and-publish job
                 ├─ verify tag == package version
                 ├─ build wheel + sdist
                 ├─ publish to PyPI (trusted publishing)
                 └─ create GitHub Release with auto-generated notes
```

### 6.3 Quality Standards (Definition of Done)

A PR is done when it satisfies:

- Branch named `<issue-number>-<description>`
- PR body contains `Closes #<issue-number>`
- Every commit cryptographically signed (`git commit -S`)
- Auto-merge enabled with `--squash`
- Conventional commit messages
- All lint, test, coverage, and security gates pass
- No new warnings introduced
- All review threads resolved

## 7. Subpackage Stability Tiers

### Stable (covered by deprecation policy)

`hephaestus.cli`, `hephaestus.config`, `hephaestus.io`, `hephaestus.logging`,
`hephaestus.system`, `hephaestus.utils`, `hephaestus.version`.

### Provisional (may change without notice)

`hephaestus.agents`, `hephaestus.automation`, `hephaestus.benchmarks`,
`hephaestus.ci`, `hephaestus.datasets`, `hephaestus.discovery`,
`hephaestus.forensics`, `hephaestus.github`, `hephaestus.markdown`,
`hephaestus.nats`, `hephaestus.resilience`, `hephaestus.validation`.

## 8. Security Architecture

- **No hardcoded secrets** — credentials read from environment variables
- **Pickle safety** — `load_data`/`save_data` block pickle by default
- **Subprocess safety** — list-form commands only, never `shell=True`
- **HTTPS downloads** — all dataset downloads use HTTPS
- **Signed commits** — every commit must be GPG/SSH signed
- **Signed tags** — release tags are GPG-signed annotated tags
- **GPG key rotation** — documented procedure for release-signing key rotation

## 9. Audit Remediation Design (Issue #88)

The `repo-analyze-strict` audit identified findings across multiple dimensions.
Issue #88 proposes remediation covering:

### 9.1 Structural

- src-layout migration (proposed in #83)
- Justfile with ecosystem recipes (added in #83)
- Configurable path variables (proposed in #85)

### 9.2 Code Quality

- Fixed `io/utils` return types for type safety
- Structured logging adoption (replacing f-string anti-patterns)
- Complexity enforcement via pre-commit hooks

### 9.3 CI/CD

- Multi-platform CI matrix expansion (Ubuntu + macOS + Windows)
- Coverage gate enforcement at 80%
- Dependency vulnerability scanning (pip-audit)

### 9.4 Documentation

- `COMPATIBILITY.md` for stability tier documentation
- `SECURITY.md` with vulnerability reporting process
- `docs/MIGRATION.md` for version upgrade guidance
- This architecture document (comprehensive design reference)

## 10. Testing Architecture (Issue #81)

### 10.1 Justfile Recipe-Pixi Sync (Issue #81)

> **Status: PROPOSED** — Issue #81 proposes adding BATS shell tests to guard
> against drift between `justfile` recipes and `pixi.toml` task definitions.

The proposed BATS tests at `tests/shell/justfile/test_justfile.bats` would
verify:

- Every `just` recipe that wraps a `pixi run` command has a matching task in
  `pixi.toml`
- Recipe descriptions match their `pixi.toml` counterparts
- No orphaned recipes exist that reference removed pixi tasks

### 10.2 Test Structure

```text
tests/
├── unit/                    # Unit tests (mirrors src/hephaestus/ structure)
│   ├── agents/
│   ├── automation/
│   ├── config/
│   └── ...
├── integration/             # Integration tests
│   ├── test_orchestration_smoke.py
│   └── test_cli_entry_points.py
└── shell/                   # BATS shell tests
    └── justfile/
        └── test_justfile.bats
```

### 10.3 Coverage Requirements

- Minimum 80% line coverage (enforced by CI)
- All new `main()` entry points require smoke tests
- Regression tests required for all bug fixes

## 11. Cross-Cutting Concerns

### 11.1 Configuration Management

`hephaestus.config` provides layered configuration:

1. Default values in code
2. YAML/JSON config files
3. Environment variable overlay via `merge_with_env`
4. Double-underscore (`__`) nesting delimiter

### 11.2 Logging

`hephaestus.logging` provides `ContextLogger` with structured JSON output,
context binding, and correlation ID propagation across subprocess boundaries.

### 11.3 Resilience

`hephaestus.resilience` provides circuit breaker and retry primitives.
Currently implemented but not yet wired into all production paths (tracked
in #469).

### 11.4 GitHub Integration

`hephaestus.github` provides:

- Rate limit detection and backoff (`wait_until`, `detect_rate_limit`)
- PR merge automation
- Fleet-wide sync operations
- Branch tidying with conflict resolution

## 12. Design Decision Log

| Decision | Issues | Status | Rationale |
|----------|--------|--------|-----------|
| src-layout migration | #83, #84, #85, #86, #88 | Proposed | Prevents accidental imports; aligns with ecosystem conventions |
| Justfile addition | #83 | Done | Standardized ecosystem task runner |
| Justfile configurable variables | #85 | Proposed | Reduces path duplication; supports future changes |
| BATS recipe-pixi sync tests | #81 | Proposed | Guards against justfile/pixi.toml drift |
| GPG-signed release tags | #88 | Done | Matches repo-wide signed-commits policy |
| hatch-vcs versioning | — | Done | Single source of truth via git tags; no version files to maintain |
| 3-stage pipeline (collapsed from 6-phase) | #677, #679 | Done | Simpler state machine; fewer handoffs; easier to debug |
| State labels as source of truth | #704 | Done | Labels-first gate; avoids comment-parsing fragility |
| Prompt fencing with random nonces | — | Done | Prevents hostile issue bodies from injecting instructions |
| Worktree isolation for implementation | — | Done | Enables parallel work; prevents branch conflicts |
| 80% coverage gate | — | Done | Balances thoroughness with development velocity |
| Signed commits required | — | Done | Cryptographic provenance for every change |

---

*This document synthesizes design decisions from issues #81, #83–#88 and the
existing documentation in `docs/`, `CLAUDE.md`, `AGENTS.md`, `README.md`,
`COMPATIBILITY.md`, `CONTRIBUTING.md`, and `SECURITY.md`.*

*Last updated: 2026-06-12*
