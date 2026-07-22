# replicate-github-org

Tools for **full-fidelity replication** of vendor code hosting into LH2-owned copies.

| Script | Platform | Method |
|--------|----------|--------|
| `replicate_github_org.sh` | GitHub org | [GitHub Enterprise Importer (GEI)](https://docs.github.com/en/migrations/using-github-enterprise-importer) |
| `replicate_gitlab_group.py` | GitLab group | Project export → import |
| `replicate_bitbucket_workspace.py` | Bitbucket Cloud workspace | API create + `git clone --mirror` / `git push --mirror` |

All scripts are **copy-only**: the source org/group/workspace is never modified or deleted. Re-runs are safe and resume from per-project/per-repo state.

> **Bitbucket fidelity note:** Bitbucket Cloud has no GEI/export-import equivalent. The Bitbucket script copies **git history (+ LFS/wiki when available)** and recreates projects/repos. Pull requests, issues, pipelines, and permissions are **not** migrated automatically.

---

## Setup

```bash
git clone https://github.com/data847/replicate-github-org.git
cd replicate-github-org
cp tokens.example tokens   # add your tokens locally — never commit tokens
```

---

# GitHub org replication

`replicate_github_org.sh` copies every repository from a source GitHub org into a target org.

## Cross-account migration

This script is designed for **vendor → LH2** (or any **source org → new target org**) setup where the orgs may live under **different GitHub accounts**.

| Requirement | Detail |
|-------------|--------|
| **Two orgs** | Source org (e.g. vendor) and target org (e.g. `VendorOrg-LH2`) must **both already exist** |
| **One PAT** | A single token must have migration access to **both** orgs (org owner or GitHub Enterprise Importer role on each) |
| **Cross-account access** | If orgs are under different accounts, the token owner must be granted admin/GEI access on the **source** org (temporary vendor invite is common) |
| **Not an org transfer** | This **copies** repos into the target org — it does **not** transfer org ownership between accounts |

If the PAT cannot read the source org, the script fails at org verification. If it cannot write to the target org, GEI migrations fail per repo.

### Source org is never modified

The script and GEI are **copy-only**:

- Source org is **never deleted, transferred, or renamed**
- Source repos are **never deleted or overwritten**
- No changes to source org settings, teams, billing, or membership
- Only **reads** from source (list repos, GEI pull) and **writes** to target

After a successful run you have **two orgs**: the original source (unchanged) and a full replica in the target.

## What gets migrated

- All repositories (including archived and forks)
- Branches, tags, commits
- Issues, pull requests, reviews, review comments
- Labels, milestones, releases
- Wikis and attachments (where GEI supports them)
- Git LFS objects (second pass)

## What does NOT migrate (manual follow-up)

- Actions run history (workflows migrate; past runs do not)
- Actions secrets/variables
- Stars, fork counts, traffic stats
- Deploy keys, webhooks, GitHub Apps
- GitHub Packages / container images
- Org SSO, billing, team membership

## Prerequisites

- [GitHub CLI](https://cli.github.com/) (`gh`)
- `git`, `git-lfs`, `jq`, `curl`
- GEI extension: `gh extension install github/gh-gei`
- **Target org must already exist** (e.g. `VendorOrg-LH2`)
- PAT with access to **both** source and target orgs (org owner or GEI role)

Required token scopes: `repo`, `read:org`, `workflow` (and GEI permissions on both orgs).

> **Cross-account:** Create the target org under the LH2 (or receiving) account first. Ensure whoever runs the script has a PAT with GEI access to **both** orgs. The vendor source org remains untouched.

## Usage

```bash
./replicate_github_org.sh \
  --tokens-file tokens \
  --source-org SOURCE_ORG \
  --target-org SOURCE_ORG-LH2
```

Or pass the token directly:

```bash
./replicate_github_org.sh \
  --token ghp_... \
  --source-org SOURCE_ORG \
  --target-org SOURCE_ORG-LH2
```

Migrate a subset with CLI flags or a text file:

```bash
./replicate_github_org.sh \
  --tokens-file tokens \
  --source-org SOURCE_ORG \
  --target-org SOURCE_ORG-LH2 \
  --repo api \
  --repo web \
  --repos-file selected-repos.txt
```

`selected-repos.txt` example:

```
# one repo name per line
api
web
# org/repo also accepted — org prefix is ignored
SOURCE_ORG/mobile
```

Token key in `tokens`: `github-data-token=ghp_...`

## Output

```
org-replica-<source>-to-<target>/
├── logs/
├── state/
├── repos.txt
├── migration-report.csv
├── migration-report.json
└── POST_MIGRATION_CHECKLIST.md
```

## Example

```bash
./replicate_github_org.sh \
  --tokens-file tokens \
  --source-org Gold-Setu \
  --target-org Gold-Setu-LH2
```

---

# GitLab group replication

`replicate_gitlab_group.py` copies every project from a source GitLab group into a target group on the same GitLab host (**gitlab.com or self-hosted**).

For Cursor/Claude usage notes on self-hosted instances, see [`SELF_HOSTED_GITLAB.md`](./SELF_HOSTED_GITLAB.md).

## What gets migrated

- All projects (including archived, including subgroups)
- Git repository (branches, tags, commits)
- Issues and issue comments
- Merge requests and MR comments
- Labels, milestones, snippets
- Wiki and uploads (within GitLab export limits)
- Git LFS objects (when included in export)

## What does NOT migrate (manual follow-up)

- CI/CD variables and secrets
- Pipeline run history
- Container registry and package registry
- Webhooks, deploy keys, runners
- Group-level permissions and SAML settings

## Prerequisites

- Python 3.10+ (stdlib only — no pip dependencies)
- **Target top-level group must already exist** in GitLab UI
- Token with **Maintainer+** on source projects and target group (`api` scope)
- For self-hosted: network reachability to the GitLab API (`/api/v4`)

## Usage

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group source-group \
  --target-group source-group-lh2
```

Or pass the token directly:

```bash
python replicate_gitlab_group.py \
  --token glpat-... \
  --source-group my-group \
  --target-group my-group-lh2 \
  --gitlab-host gitlab.com
```

### Self-hosted GitLab

`--gitlab-host` accepts a hostname or full base URL (http/https), including installs under a subpath:

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://gitlab.internal.company.com

# custom path prefix
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://company.com/gitlab

# self-signed / private CA certificate
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://gitlab.internal.company.com \
  --insecure
```

### Migrate selected projects only

Use repeatable `--project` and/or `--projects-file` (one path per line, `#` comments allowed).
Paths may be full `path_with_namespace` or relative to `--source-group`.

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group techweirdo1 \
  --target-group techweirdo1-lh2 \
  --project techweirdo1/naik-wealth/nw-backend-api \
  --project naik-wealth/nw-frontend \
  --projects-file remaining-projects.txt
```

`remaining-projects.txt` example:

```
# full path or relative to source group
techweirdo1/school
pitchlink
rethink-labs/competitions-backend
```

Token keys in `tokens` (first match wins): `gitlab_token`, `data-lh2-token-gitlab`, `data-lh2-legacy-token`, or `data-lh2-gitlab-anurag-dahlia`.

### Safety / existing target content

- Existing target subgroups and projects are **never deleted or overwritten**
- Missing subgroups are created; existing ones are reused
- Missing projects are imported with `overwrite=false`
- Extra subgroups/projects already on target are **kept**
- After migration, `hierarchy-verify.csv` checks every source group/subgroup/project mapping (scoped to selected projects when a filter is used)

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--gitlab-host` | `gitlab.com` | Hostname or base URL (`https://gitlab.example.com`, `https://host/gitlab`, or `.../api/v4`) |
| `--insecure` | off | Skip TLS certificate verification (self-signed / private CA) |
| `--project PATH` | — | Project allowlist entry (repeatable) |
| `--projects-file FILE` | — | Text file allowlist (one project path per line) |
| `--workdir` | `./group-replica-<source>-to-<target>` | Stable work directory |
| `--poll-seconds` | `15` | Export/import poll interval |
| `--export-timeout` | `7200` | Per-project export timeout (seconds) |
| `--import-timeout` | `7200` | Per-project import timeout (seconds) |

## Output

```
group-replica-<source>-to-<target>/
├── logs/
├── state/
├── exports/
├── projects.txt
├── migration-report.csv
├── migration-report.json
└── POST_MIGRATION_CHECKLIST.md
```

Subgroups under the target are created automatically when needed.

## Example

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group mindspireacademy123-crypto \
  --target-group mindspireacademy123-crypto-lh2
```

---

# Bitbucket Cloud workspace replication

`replicate_bitbucket_workspace.py` copies every repository from a source Bitbucket Cloud workspace into a target workspace.

Because Bitbucket has no full-fidelity importer, this uses:

1. REST API to create missing **projects** and empty **repositories**
2. `git clone --mirror` from source + `git push --mirror` to target
3. Optional Git LFS fetch/push and wiki mirror

### Source workspace is never modified

- Source workspace is **never deleted, transferred, or renamed**
- Source repos are **never deleted or overwritten**
- Only **reads** from source and **writes** to target

## What gets migrated

- All repositories (branches, tags, commits)
- Git LFS objects (when `git-lfs` is installed)
- Projects in the target workspace (created when missing)
- Basic repo shell settings (private flag, description, fork policy, issues/wiki flags)
- Wiki git content when present and cloneable

## What does NOT migrate (manual follow-up)

- Pull requests, PR comments, approvals
- Issues and issue comments
- Pipelines / CI history, variables, deployments
- Branch permissions, merge checks, branching model
- Webhooks, deploy keys, access tokens
- Downloads and package/container registries
- Workspace members, groups, and permissions

## Prerequisites

- Python 3.10+ (stdlib only — no pip dependencies)
- `git` (and ideally `git-lfs`)
- **Target workspace must already exist** in Bitbucket UI
- API token with access to **both** workspaces

Recommended API token scopes:

- `read:repository:bitbucket`, `write:repository:bitbucket`
- project create/admin scopes needed to recreate projects
- wiki read/write if you want wiki mirrors

Auth uses Atlassian **email + API token** (Basic) for REST, and
`x-bitbucket-api-token-auth` for git HTTPS.

## Usage

```bash
python replicate_bitbucket_workspace.py \
  --tokens-file tokens \
  --source-workspace SOURCE_WS \
  --target-workspace SOURCE_WS-lh2
```

Or pass credentials directly:

```bash
python replicate_bitbucket_workspace.py \
  --email you@company.com \
  --token ATATT3x... \
  --source-workspace SOURCE_WS \
  --target-workspace SOURCE_WS-lh2
```

Migrate a subset:

```bash
python replicate_bitbucket_workspace.py \
  --tokens-file tokens \
  --source-workspace SOURCE_WS \
  --target-workspace SOURCE_WS-lh2 \
  --repo api \
  --repo web \
  --repos-file selected-repos.txt
```

Token keys in `tokens`:

```
bitbucket_token=ATATT3x...
bitbucket_email=you@company.com
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repo SLUG` | — | Repo allowlist entry (repeatable) |
| `--repos-file FILE` | — | Text file allowlist (one repo slug per line) |
| `--workdir` | `./workspace-replica-<source>-to-<target>` | Stable work directory |
| `--keep-mirrors` | off | Keep local mirror clones under `workdir/mirrors/` |
| `--skip-wiki` | off | Do not attempt wiki git mirrors |

## Output

```
workspace-replica-<source>-to-<target>/
├── logs/
├── state/
├── mirrors/          # only if --keep-mirrors
├── repos.txt
├── migration-report.csv
├── migration-report.json
└── POST_MIGRATION_CHECKLIST.md
```

## Example

```bash
python replicate_bitbucket_workspace.py \
  --tokens-file tokens \
  --source-workspace acme-engineering \
  --target-workspace acme-engineering-lh2
```

---

## Notes (all platforms)

- Large orgs/groups/workspaces can take many hours
- Re-running skips projects/repos already marked `success`
- Never commit `tokens` — it is listed in `.gitignore`
