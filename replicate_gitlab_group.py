#!/usr/bin/env python3
"""
Full-fidelity GitLab group replication via project export → import.

Copies every project (including archived) from a source group to a target group
on the same GitLab account/host. The source group is never modified or deleted.

Prerequisites:
  - Create the empty target top-level group in GitLab UI first.
  - Token needs Maintainer+ on source projects and target group (api scope).

Usage:
  python replicate_gitlab_group.py \\
    --tokens-file tokens \\
    --source-group mindspireacademy123-crypto \\
    --target-group mindspireacademy123-crypto-lh2

  python replicate_gitlab_group.py \\
    --token glpat-xxx \\
    --source-group my-group \\
    --target-group my-group-lh2 \\
    --gitlab-host gitlab.com
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_AGENT = "replicate-gitlab-group/1.0"
TOKEN_KEYS = ("gitlab_token", "data-lh2-legacy-token", "data-lh2-gitlab-anurag-dahlia")


@dataclass
class ProjectInfo:
    path_with_namespace: str
    path: str
    name: str
    visibility: str
    archived: bool


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gitlab_api(host: str) -> str:
    host = host.rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return f"{host.rstrip('/')}/api/v4"
    return f"https://{host}/api/v4"


def encode_path(path: str) -> str:
    return urllib.parse.quote(path, safe="")


def load_token(tokens_file: Path | None, explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    if not tokens_file:
        raise SystemExit("Provide --token or --tokens-file")
    if not tokens_file.is_file():
        raise SystemExit(f"Tokens file not found: {tokens_file}")
    values: dict[str, str] = {}
    for line in tokens_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    for key in TOKEN_KEYS:
        if values.get(key):
            return values[key]
    raise SystemExit(
        f"No GitLab token found in {tokens_file}. Expected one of: {', '.join(TOKEN_KEYS)}"
    )


class GitLabClient:
    def __init__(self, token: str, host: str = "gitlab.com") -> None:
        self.token = token
        self.api = gitlab_api(host)
        self.host = host

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"PRIVATE-TOKEN": self.token, "User-Agent": USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.api}{path}{query}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} GET {path}: {detail}") from exc

    def get_bytes(self, path: str) -> bytes:
        url = f"{self.api}{path}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} GET {path}: {detail}") from exc

    def post_json(self, path: str, body: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(body or {}).encode("utf-8")
        url = f"{self.api}{path}"
        req = urllib.request.Request(
            url,
            data=payload,
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} POST {path}: {detail}") from exc

    def post_form(self, path: str, fields: dict[str, str], file_field: str, file_path: Path) -> Any:
        boundary = f"----GitLabReplica{uuid.uuid4().hex}"
        parts: list[bytes] = []

        for name, value in fields.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )

        filename = file_path.name
        mime = mimetypes.guess_type(filename)[0] or "application/gzip"
        with file_path.open("rb") as fh:
            file_data = fh.read()
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8")
        )
        parts.append(file_data)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        payload = b"".join(parts)

        url = f"{self.api}{path}"
        req = urllib.request.Request(
            url,
            data=payload,
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} POST {path}: {detail}") from exc

    def paginate(self, path: str, params: dict[str, str] | None = None) -> list[Any]:
        items: list[Any] = []
        query = dict(params or {})
        query.setdefault("per_page", "100")
        page = 1
        while True:
            query["page"] = str(page)
            url = f"{self.api}{path}?{urllib.parse.urlencode(query)}"
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read()
                data = json.loads(body.decode("utf-8")) if body else []
                headers = {k: v for k, v in resp.headers.items()}
            if not isinstance(data, list):
                return data if data is not None else items
            items.extend(data)
            next_page = headers.get("X-Next-Page")
            if next_page:
                page = int(next_page)
                continue
            if len(data) < int(query.get("per_page", "100")):
                break
            page += 1
        return items

    def get_project(self, path_with_namespace: str) -> dict[str, Any]:
        return self.get_json(f"/projects/{encode_path(path_with_namespace)}")

    def get_group(self, full_path: str) -> dict[str, Any]:
        return self.get_json(f"/groups/{encode_path(full_path)}")

    def list_group_projects(self, group: str) -> list[ProjectInfo]:
        encoded = encode_path(group)
        raw = self.paginate(
            f"/groups/{encoded}/projects",
            {"include_subgroups": "true", "with_shared": "false"},
        )
        projects: list[ProjectInfo] = []
        for item in raw:
            path_with_namespace = item["path_with_namespace"]
            if not (
                path_with_namespace == group
                or path_with_namespace.startswith(group + "/")
            ):
                continue
            projects.append(
                ProjectInfo(
                    path_with_namespace=path_with_namespace,
                    path=item["path"],
                    name=item["name"],
                    visibility=item.get("visibility", "private"),
                    archived=bool(item.get("archived", False)),
                )
            )
        seen: set[str] = set()
        unique: list[ProjectInfo] = []
        for project in sorted(projects, key=lambda p: p.path_with_namespace):
            if project.path_with_namespace in seen:
                continue
            seen.add(project.path_with_namespace)
            unique.append(project)
        return unique


class GroupReplicator:
    def __init__(
        self,
        client: GitLabClient,
        source_group: str,
        target_group: str,
        workdir: Path,
        poll_seconds: int = 15,
        export_timeout: int = 7200,
        import_timeout: int = 7200,
    ) -> None:
        self.client = client
        self.source_group = source_group.strip("/")
        self.target_group = target_group.strip("/")
        self.workdir = workdir
        self.poll_seconds = poll_seconds
        self.export_timeout = export_timeout
        self.import_timeout = import_timeout
        self.run_started_at = utc_now()

        self.log_dir = workdir / "logs"
        self.state_dir = workdir / "state"
        self.export_dir = workdir / "exports"
        self.projects_file = workdir / "projects.txt"
        self.report_csv = workdir / "migration-report.csv"
        self.report_json = workdir / "migration-report.json"
        self.checklist = workdir / "POST_MIGRATION_CHECKLIST.md"
        self.main_log = self.log_dir / "main.log"
        for path in (self.log_dir, self.state_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)

        self._group_id_cache: dict[str, int] = {}
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

    def state_file(self, project_path: str) -> Path:
        safe = project_path.replace("/", "__")
        return self.state_dir / f"{safe}.status"

    def meta_file(self, project_path: str) -> Path:
        safe = project_path.replace("/", "__")
        return self.state_dir / f"{safe}.meta.json"

    def map_target_paths(self, source_path: str) -> tuple[str, str]:
        """Return (target_namespace_path, project_slug)."""
        prefix = self.source_group + "/"
        if not source_path.startswith(prefix):
            raise ValueError(f"Project {source_path!r} is not under source group {self.source_group!r}")
        remainder = source_path[len(prefix) :]
        if "/" in remainder:
            subpath, slug = remainder.rsplit("/", 1)
            namespace = f"{self.target_group}/{subpath}"
        else:
            namespace = self.target_group
            slug = remainder
        return namespace, slug

    def target_project_path(self, source_path: str) -> str:
        namespace, slug = self.map_target_paths(source_path)
        return f"{namespace}/{slug}"

    def verify_groups(self) -> None:
        self.log("INFO", f"Verifying source group: {self.source_group}")
        self.client.get_group(self.source_group)
        self.log("INFO", f"Verifying target group: {self.target_group}")
        self.client.get_group(self.target_group)

    def ensure_subgroup(self, full_path: str) -> int:
        if full_path in self._group_id_cache:
            return self._group_id_cache[full_path]

        if full_path == self.target_group:
            group = self.client.get_group(full_path)
            self._group_id_cache[full_path] = int(group["id"])
            return int(group["id"])

        try:
            group = self.client.get_group(full_path)
            self._group_id_cache[full_path] = int(group["id"])
            return int(group["id"])
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise

        parts = full_path.split("/")
        parent_path = "/".join(parts[:-1])
        parent_id = self.ensure_subgroup(parent_path)
        slug = parts[-1]
        self.log("INFO", f"Creating subgroup: {full_path}")
        created = self.client.post_json(
            "/groups",
            {
                "name": slug,
                "path": slug,
                "parent_id": parent_id,
                "visibility": "private",
            },
        )
        group_id = int(created["id"])
        self._group_id_cache[full_path] = group_id
        return group_id

    def ensure_namespace_tree(self, projects: list[ProjectInfo]) -> None:
        namespaces: set[str] = set()
        for project in projects:
            namespace, _ = self.map_target_paths(project.path_with_namespace)
            parts = namespace.split("/")
            for idx in range(2, len(parts) + 1):
                namespaces.add("/".join(parts[:idx]))
        for namespace in sorted(namespaces, key=lambda p: (p.count("/"), p)):
            if namespace == self.target_group:
                continue
            self.ensure_subgroup(namespace)

    def list_projects(self) -> list[ProjectInfo]:
        projects = self.client.list_group_projects(self.source_group)
        if not projects:
            raise SystemExit(f"No projects found under source group {self.source_group!r}")
        with self.projects_file.open("w", encoding="utf-8") as fh:
            for project in projects:
                fh.write(project.path_with_namespace + "\n")
        self.log("INFO", f"Found {len(projects)} projects (including subgroups and archived)")
        return projects

    def target_ready(self, source_path: str) -> bool:
        target_path = self.target_project_path(source_path)
        try:
            project = self.client.get_project(target_path)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise
        import_status = project.get("import_status")
        return import_status in (None, "none", "finished")

    def wait_for_export(self, source_path: str, log_file: Path) -> None:
        encoded = encode_path(source_path)
        deadline = time.time() + self.export_timeout
        while time.time() < deadline:
            if self._interrupted:
                raise RuntimeError("Interrupted during export")
            status = self.client.get_json(f"/projects/{encoded}/export")
            export_status = status.get("export_status")
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(status) + "\n")
            if export_status == "finished":
                return
            if export_status in {"failed", "error"}:
                raise RuntimeError(f"Export failed for {source_path}: {status}")
            time.sleep(self.poll_seconds)
        raise RuntimeError(f"Export timed out for {source_path} after {self.export_timeout}s")

    def download_export(self, source_path: str, dest: Path) -> None:
        encoded = encode_path(source_path)
        data = self.client.get_bytes(f"/projects/{encoded}/export/download")
        dest.write_bytes(data)

    def wait_for_import(self, target_path: str, log_file: Path) -> None:
        encoded = encode_path(target_path)
        deadline = time.time() + self.import_timeout
        while time.time() < deadline:
            if self._interrupted:
                raise RuntimeError("Interrupted during import")
            status = self.client.get_json(f"/projects/{encoded}/import")
            import_status = status.get("import_status")
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(status) + "\n")
            if import_status == "finished":
                return
            if import_status == "failed":
                error = status.get("import_error") or status
                raise RuntimeError(f"Import failed for {target_path}: {error}")
            time.sleep(self.poll_seconds)
        raise RuntimeError(f"Import timed out for {target_path} after {self.import_timeout}s")

    def migrate_project(self, project: ProjectInfo) -> str:
        source_path = project.path_with_namespace
        sf = self.state_file(source_path)
        if sf.exists() and sf.read_text(encoding="utf-8").strip() == "success":
            self.log("INFO", f"Already migrated: {source_path}")
            return "success"

        if self.target_ready(source_path):
            self.log("INFO", f"Target already present: {self.target_project_path(source_path)}")
            sf.write_text("success", encoding="utf-8")
            return "success"

        target_namespace, project_slug = self.map_target_paths(source_path)
        target_path = f"{target_namespace}/{project_slug}"
        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_file = self.log_dir / f"{source_path.replace('/', '__')}.{run_ts}.log"

        self.log(
            "INFO",
            f"Migrating {source_path} -> {target_path} "
            f"(visibility={project.visibility}, archived={project.archived})",
        )

        meta = {
            "source_path": source_path,
            "target_path": target_path,
            "target_namespace": target_namespace,
            "project_slug": project_slug,
            "started_at": utc_now(),
        }
        self.meta_file(source_path).write_text(json.dumps(meta, indent=2), encoding="utf-8")

        try:
            encoded_source = encode_path(source_path)
            self.client.post_json(f"/projects/{encoded_source}/export")
            self.log("INFO", f"Export scheduled: {source_path}")
            self.wait_for_export(source_path, log_file)

            export_file = self.export_dir / f"{source_path.replace('/', '__')}.tar.gz"
            self.download_export(source_path, export_file)
            self.log("INFO", f"Export downloaded: {export_file.name} ({export_file.stat().st_size} bytes)")

            import_response = self.client.post_form(
                "/projects/import",
                {
                    "path": project_slug,
                    "name": project.name,
                    "namespace_path": target_namespace,
                    "overwrite": "false",
                    "override_params[visibility]": project.visibility,
                },
                "file",
                export_file,
            )
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write("IMPORT_RESPONSE\n")
                fh.write(json.dumps(import_response, indent=2) + "\n")

            imported_path = import_response.get("path_with_namespace") or target_path
            self.log("INFO", f"Import scheduled: {imported_path}")
            self.wait_for_import(imported_path, log_file)

            sf.write_text("success", encoding="utf-8")
            self.log("INFO", f"Migration complete: {source_path}")
            return "success"
        except Exception as exc:
            sf.write_text("failed", encoding="utf-8")
            self.log("ERROR", f"Migration failed for {source_path}: {exc}")
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"ERROR: {exc}\n")
            return "failed"

    def write_report(self, projects: list[ProjectInfo]) -> None:
        rows: list[dict[str, str]] = []
        for project in projects:
            sf = self.state_file(project.path_with_namespace)
            status = sf.read_text(encoding="utf-8").strip() if sf.exists() else "unknown"
            rows.append(
                {
                    "source_project": project.path_with_namespace,
                    "target_project": self.target_project_path(project.path_with_namespace),
                    "status": status,
                }
            )

        with self.report_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["source_project", "target_project", "status"]
            )
            writer.writeheader()
            writer.writerows(rows)

        self.report_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")

        checklist = f"""# Post-migration checklist

Source: `{self.source_group}` → Target: `{self.target_group}`
Last run started: `{self.run_started_at}`

## Copied automatically (export → import)
- All projects (including archived)
- Git repository (branches, tags, commits)
- Issues and issue comments
- Merge requests and MR comments
- Labels, milestones, snippets
- Wiki and uploads (within GitLab export limits)
- Git LFS objects (when included in export)

## Source safety
- Source projects are **not** deleted, moved, or modified
- Export only creates a temporary downloadable archive on the source side

## Cannot be replicated automatically (manual follow-up)
- CI/CD **variables and secrets**
- **Pipeline run history**
- Container registry and package registry
- Webhooks, deploy keys, runners
- Group-level permissions and SAML settings

## Manual verification
- [ ] Spot-check MR threads with review comments
- [ ] Spot-check issues with attachments
- [ ] Recreate CI/CD variables and secrets on target projects
- [ ] Recreate webhooks, branch protection, and environments
- [ ] Search/replace hardcoded `{self.source_group}` URLs in CI and submodules

Reports: `{self.report_csv}`
Logs: `{self.log_dir}`
"""
        self.checklist.write_text(checklist, encoding="utf-8")
        self.log("INFO", f"Report: {self.report_csv}")
        self.log("INFO", f"Checklist: {self.checklist}")

    def run(self) -> int:
        if self.source_group == self.target_group:
            raise SystemExit("Source and target group must differ")

        self.log("INFO", f"GitLab group replication: {self.source_group} -> {self.target_group}")
        self.log("INFO", f"Work directory: {self.workdir} (stable — safe to re-run)")
        self.log("INFO", "Source group is never deleted or modified")

        self.verify_groups()
        projects = self.list_projects()
        self.ensure_namespace_tree(projects)

        total = len(projects)
        failed = 0
        for idx, project in enumerate(projects, start=1):
            if self._interrupted:
                break
            self.log("INFO", f"=== Project {idx}/{total}: {project.path_with_namespace} ===")
            status = self.migrate_project(project)
            if status != "success":
                failed += 1

        self.write_report(projects)

        if self._interrupted:
            self.log("WARN", "Run interrupted — re-run the same command to resume.")
            return 130

        success = sum(
            1
            for project in projects
            if self.state_file(project.path_with_namespace).exists()
            and self.state_file(project.path_with_namespace).read_text(encoding="utf-8").strip()
            == "success"
        )
        self.log("INFO", f"Done: {success}/{total} projects migrated successfully")
        if failed:
            self.log("ERROR", f"{failed} project(s) failed — re-run to retry; see {self.report_csv}")
            return 1
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replicate a GitLab group via project export/import (same account)."
    )
    parser.add_argument("--source-group", required=True, help="Source top-level group path")
    parser.add_argument("--target-group", required=True, help="Target top-level group path (must exist)")
    parser.add_argument("--token", help="GitLab personal access token")
    parser.add_argument("--tokens-file", type=Path, help="File containing gitlab_token=...")
    parser.add_argument("--gitlab-host", default="gitlab.com", help="GitLab hostname or base URL")
    parser.add_argument(
        "--workdir",
        type=Path,
        help="Stable work directory (default: ./group-replica-<source>-to-<target>)",
    )
    parser.add_argument("--poll-seconds", type=int, default=15, help="Poll interval for export/import")
    parser.add_argument("--export-timeout", type=int, default=7200, help="Export timeout per project (seconds)")
    parser.add_argument("--import-timeout", type=int, default=7200, help="Import timeout per project (seconds)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = load_token(args.tokens_file, args.token)
    source = args.source_group.strip("/")
    target = args.target_group.strip("/")
    workdir = args.workdir or Path.cwd() / f"group-replica-{source}-to-{target}"

    client = GitLabClient(token=token, host=args.gitlab_host)
    replicator = GroupReplicator(
        client=client,
        source_group=source,
        target_group=target,
        workdir=workdir,
        poll_seconds=args.poll_seconds,
        export_timeout=args.export_timeout,
        import_timeout=args.import_timeout,
    )
    return replicator.run()


if __name__ == "__main__":
    sys.exit(main())
