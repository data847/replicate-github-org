#!/usr/bin/env python3
"""
Bitbucket Cloud workspace replication via API + git mirror.

Copies every repository from a source workspace into a target workspace.
Bitbucket has no GEI/export-import equivalent, so this script:

  1. Creates matching projects in the target workspace
  2. Creates empty destination repositories
  3. Mirror-clones git (+ LFS) from source and mirror-pushes to target
  4. Optionally mirrors wiki git repos when present

The source workspace is never modified or deleted. Re-runs are safe and
resume from per-repo state.

Prerequisites:
  - Create the empty target workspace in Bitbucket UI first.
  - API token with repo read on source and repo/project write on target.
  - git, and ideally git-lfs.

Usage:
  python replicate_bitbucket_workspace.py \\
    --tokens-file tokens \\
    --source-workspace vendor-ws \\
    --target-workspace vendor-ws-lh2

  python replicate_bitbucket_workspace.py \\
    --email you@company.com \\
    --token ATATT3x... \\
    --source-workspace vendor-ws \\
    --target-workspace vendor-ws-lh2 \\
    --repo app-backend \\
    --repos-file selected-repos.txt
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "replicate-bitbucket-workspace/1.0"
API_BASE = "https://api.bitbucket.org/2.0"
GIT_HOST = "bitbucket.org"

TOKEN_KEYS = ("bitbucket_token", "bitbucket-data-token", "data-lh2-bitbucket-token")
EMAIL_KEYS = ("bitbucket_email", "bitbucket-email", "atlassian_email")
USERNAME_KEYS = ("bitbucket_username", "bitbucket-username")


@dataclass
class RepoInfo:
    workspace: str
    slug: str
    name: str
    full_name: str
    is_private: bool
    description: str
    project_key: str
    project_name: str
    project_is_private: bool
    has_issues: bool
    has_wiki: bool
    fork_policy: str
    mainbranch: str | None
    size: int


@dataclass
class Auth:
    token: str
    email: str | None = None
    username: str | None = None

    @property
    def api_mode(self) -> str:
        # Prefer Basic (email/username + API token). Fall back to Bearer
        # for workspace/repo access tokens.
        if self.email or self.username:
            return "basic"
        return "bearer"

    @property
    def git_user(self) -> str:
        # Official static username for API-token git auth.
        return "x-bitbucket-api-token-auth"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_repo_selectors(cli_values: list[str] | None, list_file: Path | None) -> list[str]:
    """Load repo slugs from repeatable CLI flags and/or a text file."""
    values: list[str] = []
    for value in cli_values or []:
        cleaned = value.strip().strip("/")
        if cleaned:
            # Allow workspace/slug or bare slug.
            values.append(cleaned.rsplit("/", 1)[-1])

    if list_file is not None:
        if not list_file.is_file():
            raise SystemExit(f"Repos file not found: {list_file}")
        for line in list_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            values.append(line.strip("/").rsplit("/", 1)[-1])

    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def filter_repos(repos: list[RepoInfo], selectors: list[str]) -> list[RepoInfo]:
    if not selectors:
        return repos
    by_slug = {repo.slug.lower(): repo for repo in repos}
    selected: list[RepoInfo] = []
    missing: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        slug = selector.strip().strip("/").rsplit("/", 1)[-1]
        match = by_slug.get(slug.lower())
        if match is None:
            missing.append(selector)
            continue
        if match.slug.lower() in seen:
            continue
        seen.add(match.slug.lower())
        selected.append(match)
    if missing:
        preview = ", ".join(missing[:10])
        more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
        raise SystemExit(f"{len(missing)} selected repo(s) not found: {preview}{more}")
    if not selected:
        raise SystemExit("Repo filter matched zero repositories")
    return selected


def load_auth(tokens_file: Path | None, token: str | None, email: str | None, username: str | None) -> Auth:
    values: dict[str, str] = {}
    if tokens_file:
        if not tokens_file.is_file():
            raise SystemExit(f"Tokens file not found: {tokens_file}")
        for line in tokens_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    resolved_token = (token or "").strip()
    if not resolved_token:
        for key in TOKEN_KEYS:
            if values.get(key):
                resolved_token = values[key]
                break
    if not resolved_token:
        raise SystemExit(
            "Provide --token or tokens-file with one of: " + ", ".join(TOKEN_KEYS)
        )

    resolved_email = (email or "").strip() or next((values[k] for k in EMAIL_KEYS if values.get(k)), "")
    resolved_username = (username or "").strip() or next(
        (values[k] for k in USERNAME_KEYS if values.get(k)), ""
    )

    return Auth(
        token=resolved_token,
        email=resolved_email or None,
        username=resolved_username or None,
    )


class BitbucketClient:
    def __init__(self, auth: Auth) -> None:
        self.auth = auth

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.auth.api_mode == "basic":
            user = self.auth.email or self.auth.username or ""
            raw = f"{user}:{self.auth.token}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            headers["Authorization"] = f"Bearer {self.auth.token}"
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path_or_url: str,
        body: dict[str, Any] | None = None,
        timeout: int = 180,
    ) -> Any:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = f"{API_BASE}{path_or_url}"

        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers = self._headers({"Content-Type": "application/json"})

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.status == 204 or not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {method} {url}: {detail}") from exc

    def get_json(self, path_or_url: str) -> Any:
        return self.request("GET", path_or_url)

    def post_json(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, body=body)

    def put_json(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.request("PUT", path, body=body)

    def paginate(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        query = dict(params or {})
        query.setdefault("pagelen", "100")
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(query)}"
        items: list[Any] = []
        while url:
            data = self.get_json(url)
            if not isinstance(data, dict):
                return items
            values = data.get("values") or []
            if isinstance(values, list):
                items.extend(values)
            url = data.get("next") or ""
        return items

    def get_workspace(self, workspace: str) -> dict[str, Any]:
        return self.get_json(f"/workspaces/{urllib.parse.quote(workspace, safe='')}")

    def list_projects(self, workspace: str) -> list[dict[str, Any]]:
        return self.paginate(f"/workspaces/{urllib.parse.quote(workspace, safe='')}/projects")

    def create_project(
        self,
        workspace: str,
        key: str,
        name: str,
        description: str = "",
        is_private: bool = True,
    ) -> dict[str, Any]:
        return self.post_json(
            f"/workspaces/{urllib.parse.quote(workspace, safe='')}/projects",
            {
                "key": key,
                "name": name,
                "description": description or "",
                "is_private": is_private,
            },
        )

    def get_repo(self, workspace: str, repo_slug: str) -> dict[str, Any]:
        ws = urllib.parse.quote(workspace, safe="")
        slug = urllib.parse.quote(repo_slug, safe="")
        return self.get_json(f"/repositories/{ws}/{slug}")

    def create_repo(self, workspace: str, repo: RepoInfo, project_key: str) -> dict[str, Any]:
        ws = urllib.parse.quote(workspace, safe="")
        slug = urllib.parse.quote(repo.slug, safe="")
        body: dict[str, Any] = {
            "scm": "git",
            "is_private": repo.is_private,
            "description": repo.description or "",
            "fork_policy": repo.fork_policy or "allow_forks",
            "has_issues": repo.has_issues,
            "has_wiki": repo.has_wiki,
            "project": {"key": project_key},
        }
        return self.post_json(f"/repositories/{ws}/{slug}", body)

    def list_repos(self, workspace: str) -> list[RepoInfo]:
        raw = self.paginate(f"/repositories/{urllib.parse.quote(workspace, safe='')}")
        repos: list[RepoInfo] = []
        for item in raw:
            project = item.get("project") or {}
            mainbranch = None
            if isinstance(item.get("mainbranch"), dict):
                mainbranch = item["mainbranch"].get("name")
            repos.append(
                RepoInfo(
                    workspace=workspace,
                    slug=item["slug"],
                    name=item.get("name") or item["slug"],
                    full_name=item.get("full_name") or f"{workspace}/{item['slug']}",
                    is_private=bool(item.get("is_private", True)),
                    description=item.get("description") or "",
                    project_key=(project.get("key") or "PROJ"),
                    project_name=(project.get("name") or project.get("key") or "PROJ"),
                    project_is_private=bool(project.get("is_private", True)),
                    has_issues=bool(item.get("has_issues", False)),
                    has_wiki=bool(item.get("has_wiki", False)),
                    fork_policy=item.get("fork_policy") or "allow_forks",
                    mainbranch=mainbranch,
                    size=int(item.get("size") or 0),
                )
            )
        return sorted(repos, key=lambda r: r.slug)


class WorkspaceReplicator:
    def __init__(
        self,
        client: BitbucketClient,
        source_workspace: str,
        target_workspace: str,
        workdir: Path,
        keep_mirrors: bool = False,
        skip_wiki: bool = False,
        repo_selectors: list[str] | None = None,
    ) -> None:
        self.client = client
        self.source_workspace = source_workspace.strip().strip("/")
        self.target_workspace = target_workspace.strip().strip("/")
        self.workdir = workdir
        self.keep_mirrors = keep_mirrors
        self.skip_wiki = skip_wiki
        self.repo_selectors = repo_selectors or []
        self.run_started_at = utc_now()

        self.log_dir = workdir / "logs"
        self.state_dir = workdir / "state"
        self.mirrors_dir = workdir / "mirrors"
        self.repos_file = workdir / "repos.txt"
        self.report_csv = workdir / "migration-report.csv"
        self.report_json = workdir / "migration-report.json"
        self.checklist = workdir / "POST_MIGRATION_CHECKLIST.md"
        self.main_log = self.log_dir / "main.log"
        for path in (self.log_dir, self.state_dir, self.mirrors_dir):
            path.mkdir(parents=True, exist_ok=True)

        self._project_keys: set[str] = set()
        self._interrupted = False
        signal.signal(signal.SIGINT, self._handle_interrupt)
        signal.signal(signal.SIGTERM, self._handle_interrupt)

    def _handle_interrupt(self, signum: int, _frame: Any) -> None:
        self._interrupted = True
        self.log("WARN", f"Interrupted (signal {signum}) — re-run the same command to resume.")

    def log(self, level: str, message: str) -> None:
        line = f"[{utc_now()}] [{level}] {message}"
        print(line, flush=True)
        with self.main_log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def state_file(self, repo_slug: str) -> Path:
        return self.state_dir / f"{repo_slug}.status"

    def meta_file(self, repo_slug: str) -> Path:
        return self.state_dir / f"{repo_slug}.meta.json"

    def require_cmds(self) -> None:
        for cmd in ("git",):
            if shutil.which(cmd) is None:
                raise SystemExit(f"Missing required command: {cmd}")
        if shutil.which("git-lfs") is None:
            self.log("WARN", "git-lfs not found — LFS objects will not be migrated")

    def verify_workspaces(self) -> None:
        self.log("INFO", f"Verifying source workspace: {self.source_workspace}")
        self.client.get_workspace(self.source_workspace)
        self.log("INFO", f"Verifying target workspace: {self.target_workspace}")
        self.client.get_workspace(self.target_workspace)

    def list_repos(self) -> list[RepoInfo]:
        repos = self.client.list_repos(self.source_workspace)
        if not repos:
            raise SystemExit(f"No repositories found in source workspace {self.source_workspace!r}")
        total_discovered = len(repos)
        self.log("INFO", f"Found {total_discovered} repositories")
        if self.repo_selectors:
            repos = filter_repos(repos, self.repo_selectors)
            self.log(
                "INFO",
                f"Repo filter active: migrating {len(repos)}/{total_discovered} repositories",
            )
        with self.repos_file.open("w", encoding="utf-8") as fh:
            for repo in repos:
                fh.write(f"{repo.slug}\n")
        return repos

    def ensure_projects(self, repos: list[RepoInfo]) -> None:
        existing = {
            (p.get("key") or "").upper(): p
            for p in self.client.list_projects(self.target_workspace)
            if p.get("key")
        }
        self._project_keys = set(existing.keys())

        needed: dict[str, RepoInfo] = {}
        for repo in repos:
            key = (repo.project_key or "PROJ").upper()
            if key not in existing and key not in needed:
                needed[key] = repo

        for key, sample in sorted(needed.items()):
            self.log("INFO", f"Creating project {key} ({sample.project_name}) in {self.target_workspace}")
            created = self.client.create_project(
                self.target_workspace,
                key=key,
                name=sample.project_name or key,
                description="",
                is_private=sample.project_is_private,
            )
            created_key = (created.get("key") or key).upper()
            existing[created_key] = created
            self._project_keys.add(created_key)

    def target_ready(self, repo_slug: str) -> bool:
        try:
            repo = self.client.get_repo(self.target_workspace, repo_slug)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise
        # Empty freshly-created repos have size 0; treat non-empty as ready.
        return int(repo.get("size") or 0) > 0

    def git_url(self, workspace: str, repo_slug: str, wiki: bool = False) -> str:
        auth = self.client.auth
        user = urllib.parse.quote(auth.git_user, safe="")
        token = urllib.parse.quote(auth.token, safe="")
        path = f"{workspace}/{repo_slug}"
        if wiki:
            # Bitbucket Cloud wiki is a sibling git repo under /wiki
            return f"https://{user}:{token}@{GIT_HOST}/{path}.git/wiki"
        return f"https://{user}:{token}@{GIT_HOST}/{path}.git"

    def run_git(self, args: list[str], cwd: Path | None, log_file: Path, env: dict[str, str] | None = None) -> None:
        # Never log URLs that embed credentials.
        redacted = []
        for arg in args:
            if "@bitbucket.org/" in arg and "://" in arg:
                redacted.append(arg.split("@", 1)[0].rsplit(":", 1)[0] + "@bitbucket.org/REDACTED")
            else:
                redacted.append(arg)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"$ git {' '.join(redacted)}\n")

        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        # Avoid interactive credential prompts.
        merged_env.setdefault("GIT_TERMINAL_PROMPT", "0")

        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=True,
            text=True,
        )
        with log_file.open("a", encoding="utf-8") as fh:
            if proc.stdout:
                fh.write(proc.stdout)
                if not proc.stdout.endswith("\n"):
                    fh.write("\n")
            if proc.stderr:
                fh.write(proc.stderr)
                if not proc.stderr.endswith("\n"):
                    fh.write("\n")
            fh.write(f"exit={proc.returncode}\n")
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(redacted)} failed (exit {proc.returncode})")

    def mirror_repo(self, repo: RepoInfo, log_file: Path) -> None:
        mirror_path = self.mirrors_dir / f"{repo.slug}.git"
        if mirror_path.exists():
            shutil.rmtree(mirror_path)

        source_url = self.git_url(self.source_workspace, repo.slug)
        target_url = self.git_url(self.target_workspace, repo.slug)

        self.log("INFO", f"Mirror cloning {self.source_workspace}/{repo.slug}")
        self.run_git(["clone", "--mirror", source_url, str(mirror_path)], cwd=None, log_file=log_file)

        if shutil.which("git-lfs"):
            try:
                self.run_git(["lfs", "fetch", "--all"], cwd=mirror_path, log_file=log_file)
            except RuntimeError as exc:
                self.log("WARN", f"LFS fetch skipped/failed for {repo.slug}: {exc}")

        self.log("INFO", f"Mirror pushing -> {self.target_workspace}/{repo.slug}")
        self.run_git(["push", "--mirror", target_url], cwd=mirror_path, log_file=log_file)

        if shutil.which("git-lfs"):
            try:
                self.run_git(["lfs", "push", "--all", target_url], cwd=mirror_path, log_file=log_file)
            except RuntimeError as exc:
                self.log("WARN", f"LFS push skipped/failed for {repo.slug}: {exc}")

        if not self.keep_mirrors:
            shutil.rmtree(mirror_path, ignore_errors=True)

    def mirror_wiki(self, repo: RepoInfo, log_file: Path) -> None:
        if self.skip_wiki or not repo.has_wiki:
            return

        wiki_dir = self.mirrors_dir / f"{repo.slug}.wiki.git"
        if wiki_dir.exists():
            shutil.rmtree(wiki_dir)

        source_url = self.git_url(self.source_workspace, repo.slug, wiki=True)
        target_url = self.git_url(self.target_workspace, repo.slug, wiki=True)

        self.log("INFO", f"Attempting wiki mirror for {repo.slug}")
        try:
            self.run_git(["clone", "--mirror", source_url, str(wiki_dir)], cwd=None, log_file=log_file)
            self.run_git(["push", "--mirror", target_url], cwd=wiki_dir, log_file=log_file)
            self.log("INFO", f"Wiki migrated: {repo.slug}")
        except RuntimeError as exc:
            self.log("WARN", f"Wiki not migrated for {repo.slug}: {exc}")
        finally:
            if not self.keep_mirrors:
                shutil.rmtree(wiki_dir, ignore_errors=True)

    def ensure_target_repo(self, repo: RepoInfo) -> None:
        try:
            self.client.get_repo(self.target_workspace, repo.slug)
            self.log("INFO", f"Target repo exists: {self.target_workspace}/{repo.slug}")
            return
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise

        project_key = (repo.project_key or "PROJ").upper()
        self.log(
            "INFO",
            f"Creating repo {self.target_workspace}/{repo.slug} in project {project_key}",
        )
        self.client.create_repo(self.target_workspace, repo, project_key=project_key)

    def migrate_repo(self, repo: RepoInfo) -> str:
        sf = self.state_file(repo.slug)
        if sf.exists() and sf.read_text(encoding="utf-8").strip() == "success":
            self.log("INFO", f"Already migrated: {repo.slug}")
            return "success"

        if self.target_ready(repo.slug):
            self.log("INFO", f"Target already has content: {self.target_workspace}/{repo.slug}")
            sf.write_text("success", encoding="utf-8")
            return "success"

        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_file = self.log_dir / f"{repo.slug}.{run_ts}.log"
        meta = {
            "source": f"{self.source_workspace}/{repo.slug}",
            "target": f"{self.target_workspace}/{repo.slug}",
            "project_key": repo.project_key,
            "is_private": repo.is_private,
            "size": repo.size,
            "started_at": utc_now(),
        }
        self.meta_file(repo.slug).write_text(json.dumps(meta, indent=2), encoding="utf-8")

        self.log(
            "INFO",
            f"Migrating {self.source_workspace}/{repo.slug} -> "
            f"{self.target_workspace}/{repo.slug} (private={repo.is_private})",
        )

        try:
            self.ensure_target_repo(repo)
            # Brief pause so Bitbucket finishes provisioning empty repo.
            time.sleep(2)
            self.mirror_repo(repo, log_file)
            self.mirror_wiki(repo, log_file)

            # Confirm content landed.
            deadline = time.time() + 120
            while time.time() < deadline:
                if self.target_ready(repo.slug):
                    break
                time.sleep(3)
            else:
                # Some tiny repos may report size 0 briefly; accept if repo exists.
                self.client.get_repo(self.target_workspace, repo.slug)

            sf.write_text("success", encoding="utf-8")
            self.log("INFO", f"Migration complete: {repo.slug}")
            return "success"
        except Exception as exc:
            sf.write_text("failed", encoding="utf-8")
            self.log("ERROR", f"Migration failed for {repo.slug}: {exc}")
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"ERROR: {exc}\n")
            return "failed"

    def write_report(self, repos: list[RepoInfo]) -> None:
        rows: list[dict[str, str]] = []
        for repo in repos:
            sf = self.state_file(repo.slug)
            status = sf.read_text(encoding="utf-8").strip() if sf.exists() else "unknown"
            rows.append(
                {
                    "source_repo": f"{self.source_workspace}/{repo.slug}",
                    "target_repo": f"{self.target_workspace}/{repo.slug}",
                    "project_key": repo.project_key,
                    "status": status,
                }
            )

        with self.report_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["source_repo", "target_repo", "project_key", "status"],
            )
            writer.writeheader()
            writer.writerows(rows)

        self.report_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")

        checklist = f"""# Post-migration checklist

Source: `{self.source_workspace}` → Target: `{self.target_workspace}`
Last run started: `{self.run_started_at}`

## Copied automatically
- All repositories (git history: branches, tags, commits)
- Git LFS objects (when `git-lfs` is installed)
- Projects (created in target when missing)
- Repo settings that the create API accepts (private flag, description, fork policy, issues/wiki flags)
- Wiki git content when present and cloneable

## Source safety
- Source workspace/repos are **not** deleted, transferred, or modified
- Script only **reads** from source and **writes** to target

## Cannot be replicated automatically (manual follow-up)
- Pull requests, PR comments, and approvals
- Issues and issue comments (issue tracker data)
- Pipelines / CI history, variables, and deployment environments
- Branch permissions / merge checks / branching model
- Webhooks, deploy keys, access tokens
- Downloads, package/container registries
- Workspace members, groups, and permissions
- Repository forks relationship / fork network

Bitbucket Cloud has no full-fidelity org importer like GitHub GEI or GitLab export/import.
This tool mirrors git content and recreates repo shells; metadata must be rebuilt manually if needed.

## Manual verification
- [ ] Spot-check default branch and recent tags
- [ ] Confirm LFS files open correctly on a sample repo
- [ ] Recreate branch permissions / merge checks
- [ ] Recreate pipeline variables and deployment settings
- [ ] Recreate webhooks and access controls
- [ ] Search/replace hardcoded `{self.source_workspace}` URLs in CI and submodules

Reports: `{self.report_csv}`
Logs: `{self.log_dir}`
"""
        self.checklist.write_text(checklist, encoding="utf-8")
        self.log("INFO", f"Report: {self.report_csv}")
        self.log("INFO", f"Checklist: {self.checklist}")

    def run(self) -> int:
        if self.source_workspace == self.target_workspace:
            raise SystemExit("Source and target workspace must differ")

        self.log(
            "INFO",
            f"Bitbucket workspace replication: {self.source_workspace} -> {self.target_workspace}",
        )
        self.log("INFO", f"Work directory: {self.workdir} (stable — safe to re-run)")
        self.log("INFO", "Source workspace is never deleted or modified")
        self.log(
            "INFO",
            "Note: Bitbucket copies git (+ optional wiki/LFS). PRs/issues/pipelines are NOT migrated.",
        )
        if self.repo_selectors:
            self.log(
                "INFO",
                f"Using repo allowlist ({len(self.repo_selectors)} selector(s))",
            )

        self.require_cmds()
        self.verify_workspaces()
        repos = self.list_repos()
        self.ensure_projects(repos)

        total = len(repos)
        failed = 0
        for idx, repo in enumerate(repos, start=1):
            if self._interrupted:
                break
            self.log("INFO", f"=== Repo {idx}/{total}: {repo.slug} ===")
            status = self.migrate_repo(repo)
            if status != "success":
                failed += 1

        self.write_report(repos)

        if self._interrupted:
            self.log("WARN", "Run interrupted — re-run the same command to resume.")
            return 130

        success = sum(
            1
            for repo in repos
            if self.state_file(repo.slug).exists()
            and self.state_file(repo.slug).read_text(encoding="utf-8").strip() == "success"
        )
        self.log("INFO", f"Done: {success}/{total} repositories migrated successfully")
        if failed:
            self.log("ERROR", f"{failed} repo(s) failed — re-run to retry; see {self.report_csv}")
            return 1
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replicate a Bitbucket Cloud workspace via API + git mirror. "
            "Copies git history/LFS/wiki shells; not full PR/issue fidelity."
        )
    )
    parser.add_argument("--source-workspace", required=True, help="Source workspace slug")
    parser.add_argument("--target-workspace", required=True, help="Target workspace slug (must exist)")
    parser.add_argument("--token", help="Bitbucket API token or access token")
    parser.add_argument("--email", help="Atlassian account email (for API token Basic auth)")
    parser.add_argument("--username", help="Bitbucket username (optional alternative to --email)")
    parser.add_argument(
        "--tokens-file",
        type=Path,
        help="File with bitbucket_token=... and bitbucket_email=...",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        help="Stable work directory (default: ./workspace-replica-<source>-to-<target>)",
    )
    parser.add_argument(
        "--keep-mirrors",
        action="store_true",
        help="Keep local mirror clones under workdir/mirrors/",
    )
    parser.add_argument(
        "--skip-wiki",
        action="store_true",
        help="Do not attempt wiki git mirrors",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        default=[],
        metavar="SLUG",
        help="Repository slug to migrate (repeatable). Also accepts workspace/slug",
    )
    parser.add_argument(
        "--repos-file",
        type=Path,
        help="Text file with one repo slug per line (# comments allowed)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    auth = load_auth(args.tokens_file, args.token, args.email, args.username)
    source = args.source_workspace.strip().strip("/")
    target = args.target_workspace.strip().strip("/")
    workdir = args.workdir or Path.cwd() / f"workspace-replica-{source}-to-{target}"
    repo_selectors = load_repo_selectors(args.repos, args.repos_file)

    client = BitbucketClient(auth)
    replicator = WorkspaceReplicator(
        client=client,
        source_workspace=source,
        target_workspace=target,
        workdir=workdir,
        keep_mirrors=args.keep_mirrors,
        skip_wiki=args.skip_wiki,
        repo_selectors=repo_selectors,
    )
    return replicator.run()


if __name__ == "__main__":
    sys.exit(main())
