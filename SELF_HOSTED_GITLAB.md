# Self-hosted GitLab replication — agent guide

Use this when helping someone run `replicate_gitlab_group.py` against a **self-hosted GitLab** (not only `gitlab.com`).

## What this tool does

`replicate_gitlab_group.py` copies projects from a **source group** into a **target group** on the **same GitLab host** via project export → import.

- Source is **read-only** (never deleted/modified)
- Target top-level group must **already exist**
- Re-runs are safe; successful projects are skipped via `workdir/state/`

## When self-hosted applies

Use self-hosted flags whenever the GitLab UI is **not** `https://gitlab.com`, for example:

- `https://gitlab.internal.company.com`
- `https://git.company.com`
- `https://company.com/gitlab` (GitLab mounted under a subpath)
- `http://gitlab.local:8080` (HTTP-only lab instances)

## Required inputs

| Input | Meaning |
|-------|---------|
| `--source-group` | Source group full path (e.g. `vendor-group`) |
| `--target-group` | Destination group full path (must exist, e.g. `vendor-group-lh2`) |
| `--gitlab-host` | Self-hosted hostname or base URL |
| Auth | `--token glpat-...` **or** `--tokens-file tokens` |

Token needs `api` scope and **Maintainer+** on source projects and the target group.

Token keys accepted in `tokens` (first match wins):

- `gitlab_token`
- `data-lh2-token-gitlab`
- `data-lh2-legacy-token`
- `data-lh2-gitlab-anurag-dahlia`

## How to set `--gitlab-host`

Accepts any of these forms:

```text
gitlab.internal.company.com
https://gitlab.internal.company.com
https://company.com/gitlab
https://gitlab.internal.company.com/api/v4
http://gitlab.local:8080
```

Rules:

1. If scheme is omitted, script assumes `https://`
2. Script appends `/api/v4` unless the URL already ends with `/api/v4`
3. Keep any path prefix (`/gitlab`) — do not strip it
4. Prefer the same base URL users open in the browser (without `/users/sign_in`)

### TLS / certificates

- Normal trusted certs: omit `--insecure`
- Self-signed or private CA: add `--insecure`
- `--insecure` disables certificate verification — use only when needed

## Full-group migration (self-hosted)

```bash
cd replicate-github-org

python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://gitlab.internal.company.com
```

Self-signed cert:

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://gitlab.internal.company.com \
  --insecure
```

Subpath install:

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://company.com/gitlab
```

## Selected projects only

Migrate a subset with CLI and/or a text file.

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://gitlab.internal.company.com \
  --project vendor-group/app-backend \
  --project app-frontend \
  --projects-file selected-projects.txt
```

`selected-projects.txt` example:

```text
# one project per line (# comments ok)
vendor-group/app-backend
billing/api
# relative to --source-group also works:
mobile-app
```

Path rules:

- Full path: `source-group/subgroup/project`
- Relative path: `subgroup/project` or `project`
- Bare project slug works only if unique under the source group

## Resume an incomplete run

Use the **same** `--source-group`, `--target-group`, `--gitlab-host`, and `--workdir`.

```bash
python replicate_gitlab_group.py \
  --tokens-file tokens \
  --source-group vendor-group \
  --target-group vendor-group-lh2 \
  --gitlab-host https://gitlab.internal.company.com \
  --workdir ./group-replica-vendor-group-to-vendor-group-lh2
```

Behavior:

- `state/*.status == success` → skipped
- `failed` / missing status → retried
- Existing target projects are never overwritten

## What gets migrated vs not

Migrated (via GitLab export/import):

- Git history, branches, tags
- Issues, MRs, labels, milestones, snippets
- Wiki/uploads (within export limits)
- LFS when included in export

Not migrated automatically:

- CI/CD variables/secrets
- Pipeline history
- Container/package registries
- Webhooks, deploy keys, runners
- Group SSO/SAML and membership model

## Troubleshooting checklist for agents

1. **Confirm host reachability**
   - Log line should show `API base: https://.../api/v4`
   - Browser/API must resolve the hostname from the machine running the script

2. **404 on groups**
   - Wrong `--gitlab-host` path prefix
   - Wrong group path
   - Token lacks access

3. **TLS / SSL errors**
   - Try `--insecure` for self-signed/private CA
   - Or install the company CA into the system trust store

4. **DNS errors (`nodename nor servname provided`)**
   - Network/VPN/DNS issue on the runner machine
   - Re-run after connectivity is restored; script resumes

5. **Export/import timeouts**
   - Increase `--export-timeout` / `--import-timeout` (defaults: `7200`)
   - Large projects can take a long time

6. **Selected project not found**
   - Use full `path_with_namespace` from the source group
   - Check spelling/case and subgroup path

## Output locations

Default workdir: `./group-replica-<source>-to-<target>/`

```text
logs/main.log
state/*.status
exports/
projects.txt
migration-report.csv
hierarchy-verify.csv
POST_MIGRATION_CHECKLIST.md
```

## Agent behavior rules

When assisting with self-hosted GitLab replication:

1. Always ask for (or confirm) the **exact GitLab base URL**
2. Confirm target group already exists before running
3. Never commit `tokens` or paste secrets into git
4. Prefer `--workdir` for resumable runs
5. For partial migrations, prefer `--projects-file` over a huge CLI list
6. Do not suggest deleting source groups/projects
7. After failures, re-run the same command first (resume), don’t start a fresh destructive flow
