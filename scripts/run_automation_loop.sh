#!/usr/bin/env bash
# run_automation_loop.sh
#
# Clones all HomericIntelligence repos (excluding Odysseus), then runs
# 6-phase pipeline: plan → review-plans → implement → review-PRs → address-review → drive-green
# in a loop N times for every repo.
# drive-green only runs on the final loop. Up to PARALLEL_REPOS repos are processed concurrently.
#
# Issue discovery is delegated to each phase's Python entrypoint
# (gh_list_open_issues), so issues opened mid-loop are picked up by later phases.
#
# Usage:
#   ./scripts/run_automation_loop.sh [options]
#
# Options:
#   --dry-run                 Pass --dry-run to every phase (default: off)
#   --loops N                 Number of loop iterations (default: 5)
#   --max-workers N           Parallel workers per repo per phase (default: 3)
#   --parallel-repos N        Repos processed in parallel (default: 3)
#   --phases LIST             Comma-separated subset of phases to run.
#                             Valid: plan,review-plans,implement,review-prs,address-review,drive-green
#                             Default: all six.
#                             Normal gates still apply (drive-green only on final loop).
#   --planner-model MODEL     Set HEPH_PLANNER_MODEL for child processes
#   --reviewer-model MODEL    Set HEPH_REVIEWER_MODEL (covers plan-review and PR-review)
#   --implementer-model MODEL Set HEPH_IMPLEMENTER_MODEL (covers implement, address-review,
#                             ci-driver fresh sessions; --resume sites correctly omit --model)

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve the installed entry-point binaries from the Hephaestus pixi env.
# We invoke these directly (not `python -m hephaestus.automation.*`) so that
# CWD-shadowing — when `cd $repo` puts a stray `hephaestus/` directory at
# sys.path[0] — cannot mask the real package.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEPHAESTUS_DIR="$(dirname "$SCRIPT_DIR")"
PLAN_BIN="$(cd "$HEPHAESTUS_DIR" && pixi run which hephaestus-plan-issues)"
IMPL_BIN="$(cd "$HEPHAESTUS_DIR" && pixi run which hephaestus-implement-issues)"
PYTHON="$HEPHAESTUS_DIR/.pixi/envs/default/bin/python"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DRY_RUN=0
LOOPS=5
MAX_WORKERS=3
PARALLEL_REPOS=3
PROJECTS_DIR="$HOME/Projects"
ORG="HomericIntelligence"

ALL_PHASES="plan,review-plans,implement,review-prs,address-review,drive-green"
PHASES="$ALL_PHASES"

PLANNER_MODEL=""
REVIEWER_MODEL=""
IMPLEMENTER_MODEL=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)            DRY_RUN=1; shift ;;
    --loops)              LOOPS="$2"; shift 2 ;;
    --max-workers)        MAX_WORKERS="$2"; shift 2 ;;
    --parallel-repos)     PARALLEL_REPOS="$2"; shift 2 ;;
    --phases)             PHASES="$2"; shift 2 ;;
    --planner-model)      PLANNER_MODEL="$2"; shift 2 ;;
    --reviewer-model)     REVIEWER_MODEL="$2"; shift 2 ;;
    --implementer-model)  IMPLEMENTER_MODEL="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Validate --phases against the canonical list. Typos must fail loudly.
IFS=',' read -r -a PHASE_ARRAY <<< "$PHASES"
for p in "${PHASE_ARRAY[@]}"; do
  case ",${ALL_PHASES}," in
    *",${p},"*) ;;
    *) echo "Unknown phase: $p (valid: $ALL_PHASES)" >&2; exit 1 ;;
  esac
done

phase_enabled() {
  case ",${PHASES}," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

# Forward model selections via env vars to all child processes.
# claude_models.py honours these and falls back to its own defaults if unset.
[[ -n "$PLANNER_MODEL" ]]     && export HEPH_PLANNER_MODEL="$PLANNER_MODEL"
[[ -n "$REVIEWER_MODEL" ]]    && export HEPH_REVIEWER_MODEL="$REVIEWER_MODEL"
[[ -n "$IMPLEMENTER_MODEL" ]] && export HEPH_IMPLEMENTER_MODEL="$IMPLEMENTER_MODEL"

DRY_RUN_FLAGS=""
if [[ "$DRY_RUN" -eq 1 ]]; then
  DRY_RUN_FLAGS="--dry-run"
fi

# ---------------------------------------------------------------------------
# Repos to process (all non-archived, excluding Odysseus)
# ---------------------------------------------------------------------------
mapfile -t REPOS < <(
  gh repo list "$ORG" \
    --json name,isArchived \
    --limit 50 \
    --jq '[.[] | select(.isArchived == false and .name != "Odysseus") | .name] | sort[]'
)

if [[ ${#REPOS[@]} -eq 0 ]]; then
  echo "ERROR: No repos returned from gh repo list — possible GitHub API rate limit." >&2
  echo "Check: gh api rate_limit" >&2
  exit 1
fi

echo "Repos to process: ${REPOS[*]}"
echo "Loops: $LOOPS | Max workers: $MAX_WORKERS | Parallel repos: $PARALLEL_REPOS | Dry run: $DRY_RUN"
echo "Phases: $PHASES"
echo "Models: planner=${HEPH_PLANNER_MODEL:-<default>} reviewer=${HEPH_REVIEWER_MODEL:-<default>} implementer=${HEPH_IMPLEMENTER_MODEL:-<default>}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---------------------------------------------------------------------------
# Step 1: Clone any repos that don't exist locally
# ---------------------------------------------------------------------------
echo ""
echo "▶ Cloning missing repos..."
for repo in "${REPOS[@]}"; do
  dir="$PROJECTS_DIR/$repo"
  if [[ ! -d "$dir/.git" ]]; then
    echo "  Cloning $ORG/$repo -> $dir"
    gh repo clone "$ORG/$repo" "$dir"
  else
    echo "  Already cloned: $repo"
  fi
done

# ---------------------------------------------------------------------------
# process_repo: 6-phase pipeline for a single repo.
# Runs in a subshell so multiple repos can execute in parallel.
# Each phase entrypoint auto-discovers open issues via gh_list_open_issues().
# ---------------------------------------------------------------------------
process_repo() {
  local repo="$1"
  local loop="$2"
  local dir="$PROJECTS_DIR/$repo"

  echo ""
  echo "── $repo ──────────────────────────────────────────────────────"

  # Rebase main before starting work
  echo "  [$repo] Rebasing main..."
  git -C "$dir" fetch origin --quiet
  git -C "$dir" rebase origin/main --quiet 2>/dev/null \
    || git -C "$dir" reset --hard origin/main --quiet 2>/dev/null \
    || echo "  [$repo] Warning: could not rebase, continuing anyway"

  # On loop 3+, suppress follow-up issue filing to avoid noise
  local FOLLOW_UP_FLAG=""
  if [[ "$loop" -ge 3 ]]; then
    FOLLOW_UP_FLAG="--no-follow-up"
  fi

  # --- Phase 1: Plan ---
  if phase_enabled plan; then
    echo "  [$repo] Planning issues..."
    (
      cd "$dir"
      "$PLAN_BIN" \
        -v \
        $DRY_RUN_FLAGS \
        || echo "  [$repo] Warning: plan-issues exited non-zero (loop $loop)"
    )
  fi

  # --- Phase 2: Review Plans ---
  if phase_enabled review-plans; then
    echo "  [$repo] Reviewing plans..."
    (
      cd "$dir"
      "$PYTHON" "$SCRIPT_DIR/review_plans.py" \
        --max-workers "$MAX_WORKERS" \
        -v \
        $DRY_RUN_FLAGS \
        || echo "  [$repo] Warning: review-plans exited non-zero (loop $loop)"
    )
  fi

  # --- Phase 3: Implement ---
  if phase_enabled implement; then
    echo "  [$repo] Implementing issues..."
    (
      cd "$dir"
      "$IMPL_BIN" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        -v \
        $FOLLOW_UP_FLAG \
        $DRY_RUN_FLAGS \
        || echo "  [$repo] Warning: implement-issues exited non-zero (loop $loop)"
    )
  fi

  # --- Phase 4: Review PRs (inline comments) ---
  if phase_enabled review-prs; then
    echo "  [$repo] Reviewing PRs..."
    (
      cd "$dir"
      "$PYTHON" "$SCRIPT_DIR/review_issues.py" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        -v \
        $DRY_RUN_FLAGS \
        || echo "  [$repo] Warning: review-issues exited non-zero (loop $loop)"
    )
  fi

  # --- Phase 5: Address Review Comments ---
  if phase_enabled address-review; then
    echo "  [$repo] Addressing review comments..."
    (
      cd "$dir"
      "$PYTHON" "$SCRIPT_DIR/address_review.py" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        -v \
        $DRY_RUN_FLAGS \
        || echo "  [$repo] Warning: address-review exited non-zero (loop $loop)"
    )
  fi

  # --- Phase 6: Drive PRs to Green CI (final loop only) ---
  if phase_enabled drive-green && [[ "$loop" -eq "$LOOPS" ]]; then
    echo "  [$repo] Driving PRs to green CI..."
    (
      cd "$dir"
      # Defence-in-depth: ci_driver also checks these envs and refuses to run
      # unless HEPH_LOOP_INDEX == HEPH_TOTAL_LOOPS or --force-run is given.
      HEPH_LOOP_INDEX="$loop" HEPH_TOTAL_LOOPS="$LOOPS" \
      "$PYTHON" "$SCRIPT_DIR/drive_prs_green.py" \
        --max-workers "$MAX_WORKERS" \
        --no-ui \
        -v \
        $DRY_RUN_FLAGS \
        || echo "  [$repo] Warning: drive-prs-green exited non-zero (loop $loop)"
    )
  fi
}

# ---------------------------------------------------------------------------
# Step 2: Main loop — PARALLEL_REPOS repos processed concurrently
# ---------------------------------------------------------------------------
for (( loop=1; loop<=LOOPS; loop++ )); do
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "▶ LOOP $loop / $LOOPS"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  active_pids=()
  for repo in "${REPOS[@]}"; do
    process_repo "$repo" "$loop" &
    active_pids+=($!)

    if [[ ${#active_pids[@]} -ge "$PARALLEL_REPOS" ]]; then
      wait "${active_pids[0]}"
      active_pids=("${active_pids[@]:1}")
    fi
  done

  # Drain any remaining background jobs
  for pid in "${active_pids[@]}"; do
    wait "$pid"
  done

  echo ""
  echo "  Loop $loop complete."
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✓ All $LOOPS loops complete across ${#REPOS[@]} repos."
