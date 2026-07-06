# replicate-github-org

Tools for **full-fidelity replication** of vendor code hosting into LH2-owned copies.

| Script | Platform | Method |
|--------|----------|--------|
| `replicate_github_org.sh` | GitHub org | [GitHub Enterprise Importer (GEI)](https://docs.github.com/en/migrations/using-github-enterprise-importer) |
| `replicate_gitlab_group.py` | GitLab group | Project export → import |

Both scripts are **copy-only**: the source org/group is never modified or deleted. Re-runs are safe and resume from per-project/per-repo state.

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

`replicate_gitlab_group.py` copies every project from a source GitLab group into a target group on the same GitLab host.

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

Token keys in `tokens` (first match wins): `gitlab_token`, `data-lh2-legacy-token`, or `data-lh2-gitlab-anurag-dahlia`.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--gitlab-host` | `gitlab.com` | GitLab hostname or base URL |
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

## Notes (both platforms)

- Large orgs/groups can take many hours
- Re-running skips projects/repos already marked `success`
- Never commit `tokens` — it is listed in `.gitignore`
