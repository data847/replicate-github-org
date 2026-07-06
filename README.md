# replicate-github-org

Full-fidelity **GitHub organization replication** using [GitHub Enterprise Importer (GEI)](https://docs.github.com/en/migrations/using-github-enterprise-importer).

Copies every repository from a source org into a target org. The source org is **never modified or deleted**.

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

## Quick start

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

## Tokens file format

Copy `tokens.example` to `tokens` and add your PAT. **Do not commit `tokens`.**

```
github-data-token=ghp_...
```

## Output

Creates a stable work directory:

```
org-replica-<source>-to-<target>/
├── logs/                      # Per-repo GEI logs
├── state/                     # Per-repo success/failed status
├── repos.txt                  # All repos discovered
├── migration-report.csv       # Summary
├── migration-report.json
└── POST_MIGRATION_CHECKLIST.md
```

Re-running is **safe**: repos already marked `success` are skipped.

## Notes

- Migrations run **one repo at a time** to avoid GEI queue conflicts
- States like `PENDING_VALIDATION`, `QUEUED`, `IN_PROGRESS` are normal
- Large orgs can take many hours

## Example

```bash
./replicate_github_org.sh \
  --tokens-file tokens \
  --source-org Gold-Setu \
  --target-org Gold-Setu-LH2
```
