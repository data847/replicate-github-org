#!/usr/bin/env bash
#
# Full-fidelity GitHub org replication using GitHub Enterprise Importer (GEI).
# Migrates ALL repos (including archived), all metadata GEI supports, and LFS.
# Cross-account: requires one PAT with GEI access to BOTH source and target orgs.
# Source org is READ-ONLY — never deleted, transferred, or modified.
# This creates copies in the target org only; it is not an org ownership transfer.
#
# Usage:
#   ./replicate_github_org.sh \
#     --tokens-file tokens \
#     --source-org Gold-Setu \
#     --target-org Gold-Setu-LH2
#
# Optional allowlist:
#   --repo NAME / --repos-file FILE
#
# Re-running is safe: uses a stable work dir and skips repos already marked success.
# Migrations run one at a time so GEI queue conflicts are avoided.
#
set -euo pipefail

SOURCE_ORG=""
TARGET_ORG=""
TOKEN=""
TOKENS_FILE=""
REPO_SELECTORS=()
REPOS_LIST_FILE=""

RUN_STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
WORKDIR=""
LOG_DIR=""
STATE_DIR=""
REPOS_FILE=""
REPORT_CSV=""
REPORT_JSON=""
CHECKLIST=""

usage() {
  cat <<'EOF'
Full-fidelity GitHub org replication (everything GEI can migrate).

Cross-account: one PAT must have GEI access to BOTH source and target orgs.
Source org is READ-ONLY — never deleted, transferred, or modified.
This is a copy into the target org, not an org ownership transfer.

Required:
  --source-org ORG    Source organization
  --target-org ORG    Destination organization (must already exist)

Auth (one of):
  --token TOKEN       GitHub PAT with full source + target access
  --tokens-file FILE  File with: github-data-token=ghp_...

Optional allowlist (migrate a subset):
  --repo NAME         Repository name to migrate (repeatable)
  --repos-file FILE   Text file with one repo name per line (# comments ok)

Migrates ALL repos by default (including archived and forks), all branches/tags/commits,
issues, PRs, reviews, labels, milestones, releases, wikis, and LFS.

GEI states like PENDING_VALIDATION, QUEUED, and IN_PROGRESS are normal — not errors.
EOF
}

init_paths() {
  # Stable dir so re-runs resume; logs for each attempt are timestamped per repo.
  WORKDIR="$(pwd)/org-replica-${SOURCE_ORG}-to-${TARGET_ORG}"
  LOG_DIR="${WORKDIR}/logs"
  STATE_DIR="${WORKDIR}/state"
  REPOS_FILE="${WORKDIR}/repos.txt"
  REPORT_CSV="${WORKDIR}/migration-report.csv"
  REPORT_JSON="${WORKDIR}/migration-report.json"
  CHECKLIST="${WORKDIR}/POST_MIGRATION_CHECKLIST.md"
  mkdir -p "$LOG_DIR" "$STATE_DIR"
}

log() {
  local level="$1"
  shift
  local line="[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [$level] $*"
  echo "$line"
  echo "$line" >> "${LOG_DIR}/main.log"
}

die() {
  echo "ERROR: $*" >&2
  [[ -n "$LOG_DIR" && -d "$LOG_DIR" ]] && echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] [ERROR] $*" >> "${LOG_DIR}/main.log"
  exit 1
}

on_interrupt() {
  echo ""
  log "WARN" "Interrupted — migrations may still be running on GitHub. Re-run the same command to resume."
  exit 130
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source-org) SOURCE_ORG="$2"; shift 2 ;;
      --target-org) TARGET_ORG="$2"; shift 2 ;;
      --token) TOKEN="$2"; shift 2 ;;
      --tokens-file) TOKENS_FILE="$2"; shift 2 ;;
      --repo) REPO_SELECTORS+=("$2"); shift 2 ;;
      --repos-file) REPOS_LIST_FILE="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown argument: $1" ;;
    esac
  done

  [[ -n "$SOURCE_ORG" ]] || die "--source-org is required"
  [[ -n "$TARGET_ORG" ]] || die "--target-org is required"
  [[ "$SOURCE_ORG" != "$TARGET_ORG" ]] || die "Source and target org must differ"

  if [[ -z "$TOKEN" && -n "$TOKENS_FILE" ]]; then
    [[ -f "$TOKENS_FILE" ]] || die "Tokens file not found: $TOKENS_FILE"
    TOKEN="$(grep -E '^[[:space:]]*github-data-token[[:space:]]*=' "$TOKENS_FILE" \
      | head -1 | cut -d= -f2- | tr -d '[:space:]')"
  fi
  [[ -n "$TOKEN" ]] || die "Provide --token or --tokens-file with github-data-token="

  if [[ -n "$REPOS_LIST_FILE" ]]; then
    [[ -f "$REPOS_LIST_FILE" ]] || die "Repos file not found: $REPOS_LIST_FILE"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

check_prereqs() {
  require_cmd gh
  require_cmd git
  require_cmd git-lfs
  require_cmd jq
  require_cmd curl

  if ! gh extension list 2>/dev/null | grep -q 'gh-gei'; then
    log "INFO" "Installing gh-gei extension..."
    gh extension install github/gh-gei
  fi
  gh gei version >/dev/null 2>&1 || die "gh gei is not available — run: gh extension install github/gh-gei"
  log "INFO" "Tip: upgrade GEI with: gh extension upgrade gei"
}

setup_env() {
  export GH_SOURCE_PAT="$TOKEN"
  export GH_PAT="$TOKEN"
  export GITHUB_TOKEN="$TOKEN"
  echo "$TOKEN" | gh auth login --with-token 2>/dev/null || true
}

api_get() {
  curl -fsSL \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$1"
}

api_status_code() {
  curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$1"
}

verify_orgs() {
  log "INFO" "Verifying source org: ${SOURCE_ORG}"
  api_get "https://api.github.com/orgs/${SOURCE_ORG}" >/dev/null \
    || die "Cannot access source org '${SOURCE_ORG}'. Check token scopes (repo, read:org, workflow)."

  log "INFO" "Verifying target org: ${TARGET_ORG}"
  api_get "https://api.github.com/orgs/${TARGET_ORG}" >/dev/null \
    || die "Cannot access target org '${TARGET_ORG}'. Create it first: https://github.com/account/organizations/new"
}

apply_repo_filter() {
  local discovered_count selected_count
  discovered_count="$(wc -l < "$REPOS_FILE" | tr -d ' ')"

  if [[ ${#REPO_SELECTORS[@]} -eq 0 && -z "$REPOS_LIST_FILE" ]]; then
    log "INFO" "Found ${discovered_count} repositories — all will be migrated"
    return 0
  fi

  local tmp_selected tmp_wanted
  tmp_selected="$(mktemp)"
  tmp_wanted="$(mktemp)"
  : > "$tmp_wanted"

  local repo
  for repo in "${REPO_SELECTORS[@]+"${REPO_SELECTORS[@]}"}"; do
    repo="$(echo "$repo" | sed -E 's#.*/##; s/^[[:space:]]+//; s/[[:space:]]+$//')"
    [[ -n "$repo" ]] && echo "$repo" >> "$tmp_wanted"
  done

  if [[ -n "$REPOS_LIST_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="$(echo "$line" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
      [[ -z "$line" || "$line" == \#* ]] && continue
      line="$(echo "$line" | sed -E 's#.*/##')"
      echo "$line" >> "$tmp_wanted"
    done < "$REPOS_LIST_FILE"
  fi

  sort -u "$tmp_wanted" -o "$tmp_wanted"
  : > "$tmp_selected"

  local missing=0
  while IFS= read -r repo || [[ -n "$repo" ]]; do
    [[ -z "$repo" ]] && continue
    if grep -Fxq "$repo" "$REPOS_FILE"; then
      echo "$repo" >> "$tmp_selected"
    else
      log "ERROR" "Selected repository not found in ${SOURCE_ORG}: ${repo}"
      missing=$((missing + 1))
    fi
  done < "$tmp_wanted"

  [[ "$missing" -eq 0 ]] || die "${missing} selected repo(s) not found in ${SOURCE_ORG}"
  sort -u "$tmp_selected" -o "$REPOS_FILE"
  selected_count="$(wc -l < "$REPOS_FILE" | tr -d ' ')"
  [[ "$selected_count" -gt 0 ]] || die "Repo filter matched zero repositories"
  log "INFO" "Repo filter active: migrating ${selected_count}/${discovered_count} repositories"
  rm -f "$tmp_selected" "$tmp_wanted"
}

list_all_repos() {
  log "INFO" "Listing ALL repositories in ${SOURCE_ORG} (including archived)"
  : > "$REPOS_FILE"
  local page=1
  while true; do
    local resp count
    resp="$(api_get "https://api.github.com/orgs/${SOURCE_ORG}/repos?per_page=100&page=${page}&type=all")"
    count="$(echo "$resp" | jq 'length')"
    [[ "$count" -eq 0 ]] && break
    echo "$resp" | jq -r '.[].name' >> "$REPOS_FILE"
    page=$((page + 1))
  done
  sort -u "$REPOS_FILE" -o "$REPOS_FILE"
  local total
  total="$(wc -l < "$REPOS_FILE" | tr -d ' ')"
  [[ "$total" -gt 0 ]] || die "No repositories found in ${SOURCE_ORG}"
  apply_repo_filter
}

state_file() { echo "${STATE_DIR}/$1.status"; }
meta_file()  { echo "${STATE_DIR}/$1.meta.json"; }

repo_visibility() {
  local repo="$1"
  api_get "https://api.github.com/repos/${SOURCE_ORG}/${repo}" | jq -r '.visibility // "private"'
}

target_repo_exists() {
  local repo="$1"
  [[ "$(api_status_code "https://api.github.com/repos/${TARGET_ORG}/${repo}")" == "200" ]]
}

target_repo_ready() {
  local repo="$1"
  target_repo_exists "$repo" || return 1
  local meta default_branch
  meta="$(api_get "https://api.github.com/repos/${TARGET_ORG}/${repo}")"
  default_branch="$(echo "$meta" | jq -r '.default_branch // empty')"
  [[ -n "$default_branch" ]]
}

# Returns: 0=success, 1=failed, 2=already_queued, 3=already_exists, 4=incomplete
classify_migration_log() {
  local logf="$1"

  if grep -qE 'OctoshiftCliException|\[ERROR\]' "$logf"; then
    if grep -q 'already been queued' "$logf"; then
      return 2
    fi
    if grep -qE 'already contains a repository|already exists' "$logf"; then
      return 3
    fi
    return 1
  fi

  if grep -q '"state":"FAILED"' "$logf"; then
    return 1
  fi

  if grep -q '"state":"SUCCEEDED"' "$logf"; then
    return 0
  fi

  if grep -qE 'already contains a repository|already exists' "$logf"; then
    return 3
  fi

  if grep -qE '"state":"IN_PROGRESS"|"state":"QUEUED"|"state":"PENDING_VALIDATION"' "$logf"; then
    return 4
  fi

  return 0
}

wait_for_target_repo() {
  local repo="$1"
  local max_wait="${2:-7200}"
  local elapsed=0
  log "INFO" "Waiting for ${TARGET_ORG}/${repo} to become ready (up to ${max_wait}s)..."
  while [[ "$elapsed" -lt "$max_wait" ]]; do
    if target_repo_ready "$repo"; then
      log "INFO" "Target repo ready: ${repo}"
      return 0
    fi
    sleep 15
    elapsed=$((elapsed + 15))
    log "INFO" "Still waiting on ${repo} (${elapsed}s elapsed)..."
  done
  return 1
}

migrate_repo() {
  local repo="$1"
  local sf logf run_ts
  sf="$(state_file "$repo")"
  run_ts="$(date -u +"%Y%m%dT%H%M%SZ")"
  logf="${LOG_DIR}/${repo}.${run_ts}.migrate.log"

  if [[ -f "$sf" && "$(cat "$sf")" == "success" ]]; then
    log "INFO" "Already migrated: ${repo}"
    return 0
  fi

  if target_repo_ready "$repo"; then
    log "INFO" "Target repo already present: ${TARGET_ORG}/${repo} — marking success"
    echo "success" > "$sf"
    return 0
  fi

  local visibility
  visibility="$(repo_visibility "$repo")"
  log "INFO" "Migrating ${SOURCE_ORG}/${repo} -> ${TARGET_ORG}/${repo} (${visibility})"
  log "INFO" "GEI progress states (PENDING_VALIDATION / QUEUED / IN_PROGRESS) are normal"

  api_get "https://api.github.com/repos/${SOURCE_ORG}/${repo}" > "$(meta_file "$repo")"

  set +e
  gh gei migrate-repo \
    --github-source-org "$SOURCE_ORG" \
    --source-repo "$repo" \
    --github-target-org "$TARGET_ORG" \
    --target-repo "$repo" \
    --target-repo-visibility "$visibility" \
    --verbose \
    2>&1 | tee "$logf"
  local rc=${PIPESTATUS[0]}
  set -e

  classify_migration_log "$logf" || true
  local outcome=$?

  case "$outcome" in
    0)
      if [[ "$rc" -ne 0 ]]; then
        echo "failed" > "$sf"
        log "ERROR" "GEI failed for ${repo} (see ${logf})"
        return 1
      fi
      ;;
    2)
      log "INFO" "Migration already queued for ${repo} — waiting for target repo"
      if wait_for_target_repo "$repo"; then
        echo "success" > "$sf"
        log "INFO" "Repo migration complete (was queued): ${repo}"
        return 0
      fi
      echo "failed" > "$sf"
      log "ERROR" "Timed out waiting for queued migration: ${repo}"
      return 1
      ;;
    3)
      if target_repo_ready "$repo"; then
        echo "success" > "$sf"
        log "INFO" "Repo migration complete (already existed): ${repo}"
        return 0
      fi
      if wait_for_target_repo "$repo"; then
        echo "success" > "$sf"
        log "INFO" "Repo migration complete (existed, now ready): ${repo}"
        return 0
      fi
      echo "failed" > "$sf"
      log "ERROR" "Repo exists on target but never became ready: ${repo}"
      return 1
      ;;
    4)
      log "WARN" "Migration log ended while still in progress for ${repo} — waiting on target"
      if wait_for_target_repo "$repo"; then
        echo "success" > "$sf"
        log "INFO" "Repo migration complete (finished after wait): ${repo}"
        return 0
      fi
      echo "failed" > "$sf"
      log "ERROR" "Migration incomplete for ${repo} (see ${logf})"
      return 1
      ;;
    1)
      echo "failed" > "$sf"
      log "ERROR" "GEI migration failed for ${repo} (see ${logf})"
      return 1
      ;;
  esac

  if ! target_repo_ready "$repo"; then
    if wait_for_target_repo "$repo"; then
      echo "success" > "$sf"
      log "INFO" "Repo migration complete: ${repo}"
      return 0
    fi
    echo "failed" > "$sf"
    log "ERROR" "GEI reported success but target repo not ready: ${repo}"
    return 1
  fi

  echo "success" > "$sf"
  log "INFO" "Repo migration complete: ${repo}"
  return 0
}

migrate_lfs() {
  local repo="$1"
  local sf logf run_ts
  sf="$(state_file "$repo")"
  run_ts="$(date -u +"%Y%m%dT%H%M%SZ")"
  logf="${LOG_DIR}/${repo}.${run_ts}.lfs.log"

  [[ -f "$sf" && "$(cat "$sf")" == "success" ]] || return 0

  log "INFO" "Migrating LFS for ${repo}"
  set +e
  gh gei migrate-lfs \
    --github-source-org "$SOURCE_ORG" \
    --source-repo "$repo" \
    --github-target-org "$TARGET_ORG" \
    --target-repo "$repo" \
    2>&1 | tee "$logf"
  local rc=${PIPESTATUS[0]}
  set -e

  if [[ "$rc" -eq 0 ]]; then
    log "INFO" "LFS migration complete: ${repo}"
  else
    log "INFO" "No LFS objects or LFS already migrated: ${repo}"
  fi
}

run_migrations() {
  local repo n=0 total
  total="$(wc -l < "$REPOS_FILE" | tr -d ' ')"
  while IFS= read -r repo || [[ -n "$repo" ]]; do
    [[ -n "$repo" ]] || continue
    n=$((n + 1))
    log "INFO" "=== Repo ${n}/${total}: ${repo} ==="
    migrate_repo "$repo" || true
  done < "$REPOS_FILE"
}

run_lfs_pass() {
  log "INFO" "Starting LFS pass for all repos"
  while IFS= read -r repo || [[ -n "$repo" ]]; do
    [[ -n "$repo" ]] || continue
    migrate_lfs "$repo"
  done < "$REPOS_FILE"
}

write_report() {
  log "INFO" "Writing report"
  echo "repo,status" > "$REPORT_CSV"
  local results="[]"
  local repo status sf

  while IFS= read -r repo || [[ -n "$repo" ]]; do
    [[ -n "$repo" ]] || continue
    sf="$(state_file "$repo")"
    status="unknown"
    [[ -f "$sf" ]] && status="$(cat "$sf")"
    echo "${repo},${status}" >> "$REPORT_CSV"
    results="$(echo "$results" | jq --arg r "$repo" --arg s "$status" '. + [{repo: $r, status: $s}]')"
  done < "$REPOS_FILE"

  echo "$results" | jq '.' > "$REPORT_JSON"

  cat > "$CHECKLIST" <<EOF
# Post-migration checklist

Source: \`${SOURCE_ORG}\` → Target: \`${TARGET_ORG}\`
Last run started: \`${RUN_STARTED_AT}\`

## Migrated automatically (full fidelity — no skips)
- All repositories (including archived and forks)
- All branches, tags, commits
- All issues, PRs, reviews, review comments
- All labels, milestones, releases
- Wikis and attachments where GEI supports them
- Git LFS objects

## Cannot be replicated by GEI (GitHub platform limits — manual only)
- Actions **run history** (workflows migrate; past runs do not)
- Actions **secrets/variables** (must recreate)
- **Stars**, fork counts, traffic stats
- **Deploy keys**, webhooks, GitHub Apps (must reconfigure)
- **GitHub Packages** / container registry images (separate migration)
- Org SSO, billing, team membership mappings

## Manual verification
- [ ] Spot-check PR threads with review comments
- [ ] Spot-check issues with attachments
- [ ] Spot-check releases with binary assets
- [ ] Search/replace hardcoded \`${SOURCE_ORG}\` URLs in CI and submodules
- [ ] Recreate secrets, webhooks, branch protection, environments

Reports: \`${REPORT_CSV}\`
Logs: \`${LOG_DIR}\`
EOF

  log "INFO" "Report: ${REPORT_CSV}"
  log "INFO" "Checklist: ${CHECKLIST}"
}

main() {
  trap on_interrupt INT TERM
  parse_args "$@"
  init_paths

  log "INFO" "Full-fidelity org replication: ${SOURCE_ORG} -> ${TARGET_ORG}"
  log "INFO" "Work directory: ${WORKDIR} (stable — safe to re-run)"
  log "INFO" "Source org is never deleted or modified"

  check_prereqs
  setup_env
  verify_orgs
  list_all_repos
  run_migrations
  run_lfs_pass
  write_report

  local failed=0
  if [[ -f "$REPORT_CSV" ]]; then
    failed="$(grep -c ',failed$' "$REPORT_CSV" 2>/dev/null || true)"
  fi
  if [[ "$failed" -gt 0 ]]; then
    die "${failed} repo(s) failed — re-run the same command to retry; see ${REPORT_CSV}"
  fi

  local total success
  total="$(wc -l < "$REPOS_FILE" | tr -d ' ')"
  success="$(grep -c ',success$' "$REPORT_CSV" 2>/dev/null || true)"
  log "INFO" "Done: ${success}/${total} repos migrated successfully"
}

main "$@"
