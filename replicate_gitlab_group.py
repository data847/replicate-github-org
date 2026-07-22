#!/usr/bin/env python3
"""
Full-fidelity GitLab group replication via project export → import.

Copies every project (including archived) from a source group to a target group
on the same GitLab host (gitlab.com or self-hosted).

Safety:
  - Source is never modified or deleted.
  - Existing target subgroups/projects are never overwritten or removed.
  - Extra content already on the target is kept.
  - End-of-run hierarchy verify checks every group/subgroup/project mapping.

Prerequisites:
  - Create the target top-level group in GitLab UI first (may already have content).
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
    --gitlab-host https://gitlab.example.com \\
    --project my-group/app \\
    --projects-file selected-projects.txt \\
    --insecure
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import signal
import ssl
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
TOKEN_KEYS = (
    "gitlab_token",
    "data-lh2-token-gitlab",
    "data-lh2-legacy-token",
    "data-lh2-gitlab-anurag-dahlia",
)


@dataclass
class ProjectInfo:
    path_with_namespace: str
    path: str
    name: str
    visibility: str
    archived: bool


@dataclass
class GroupInfo:
    full_path: str
    path: str
    name: str
    visibility: str
    parent_id: int | None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gitlab_api(host: str) -> str:
    """
    Build the GitLab REST API v4 base URL.

    Accepts:
      - hostname: gitlab.com
      - base URL: https://gitlab.example.com
      - custom path: https://company.com/gitlab
      - already includes /api/v4
    """
    host = host.strip().rstrip("/")
    if not host:
        raise SystemExit("--gitlab-host must not be empty")

    if not (host.startswith("http://") or host.startswith("https://")):
        host = f"https://{host}"

    # Allow users to pass .../api/v4 or .../api/v4/ directly.
    if host.endswith("/api/v4"):
        return host
    return f"{host}/api/v4"


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


def load_path_list(cli_values: list[str] | None, list_file: Path | None) -> list[str]:
    """Load selectors from repeatable CLI flags and/or a text file (one path per line)."""
    values: list[str] = []
    for value in cli_values or []:
        cleaned = value.strip().strip("/")
        if cleaned:
            values.append(cleaned)

    if list_file is not None:
        if not list_file.is_file():
            raise SystemExit(f"Projects file not found: {list_file}")
        for line in list_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            values.append(line.strip("/"))

    # Preserve order, drop duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def normalize_project_selector(selector: str, source_group: str) -> str:
    """
    Accept either a full path_with_namespace or a path relative to the source group.
      techweirdo1/app          -> techweirdo1/app
      app                      -> techweirdo1/app
      techweirdo1/sub/app      -> techweirdo1/sub/app
      sub/app                  -> techweirdo1/sub/app
    """
    selector = selector.strip().strip("/")
    source_group = source_group.strip().strip("/")
    if not selector:
        raise SystemExit("Empty project selector")
    if selector == source_group or selector.startswith(source_group + "/"):
        return selector
    return f"{source_group}/{selector}"


def filter_projects(
    projects: list[ProjectInfo],
    selectors: list[str],
    source_group: str,
) -> list[ProjectInfo]:
    if not selectors:
        return projects

    by_path = {p.path_with_namespace: p for p in projects}
    # Also allow matching by project slug alone when unique under the group.
    by_slug: dict[str, list[ProjectInfo]] = {}
    for project in projects:
        by_slug.setdefault(project.path, []).append(project)

    selected: list[ProjectInfo] = []
    missing: list[str] = []
    seen: set[str] = set()

    for raw in selectors:
        normalized = normalize_project_selector(raw, source_group)
        match = by_path.get(normalized)
        if match is None and "/" not in raw.strip().strip("/"):
            slug_matches = by_slug.get(raw.strip().strip("/"), [])
            if len(slug_matches) == 1:
                match = slug_matches[0]
            elif len(slug_matches) > 1:
                options = ", ".join(p.path_with_namespace for p in slug_matches)
                raise SystemExit(
                    f"Ambiguous project selector {raw!r}; matches: {options}. "
                    "Use a full path_with_namespace."
                )
        if match is None:
            missing.append(normalized)
            continue
        if match.path_with_namespace in seen:
            continue
        seen.add(match.path_with_namespace)
        selected.append(match)

    if missing:
        preview = ", ".join(missing[:10])
        more = f" (+{len(missing) - 10} more)" if len(missing) > 10 else ""
        raise SystemExit(
            f"{len(missing)} selected project(s) not found under {source_group!r}: "
            f"{preview}{more}"
        )
    if not selected:
        raise SystemExit("Project filter matched zero projects")
    return selected


def subgroups_needed_for_projects(
    source_groups: list[GroupInfo],
    projects: list[ProjectInfo],
    source_group: str,
) -> list[GroupInfo]:
    """Keep only subgroups that appear in selected project namespaces."""
    needed_paths: set[str] = set()
    prefix = source_group + "/"
    for project in projects:
        path = project.path_with_namespace
        if not path.startswith(prefix):
            continue
        parts = path[len(prefix) :].split("/")
        # Drop the project slug; keep intermediate subgroup paths.
        for depth in range(1, len(parts)):
            needed_paths.add(f"{source_group}/{'/'.join(parts[:depth])}")
    return [g for g in source_groups if g.full_path in needed_paths]


class GitLabClient:
    def __init__(
        self,
        token: str,
        host: str = "gitlab.com",
        insecure: bool = False,
    ) -> None:
        self.token = token
        self.api = gitlab_api(host)
        self.host = host
        self.insecure = insecure
        self._ssl_context: ssl.SSLContext | None = None
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"PRIVATE-TOKEN": self.token, "User-Agent": USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def _urlopen(self, req: urllib.request.Request, timeout: int):
        return urllib.request.urlopen(req, timeout=timeout, context=self._ssl_context)

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.api}{path}{query}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with self._urlopen(req, timeout=180) as resp:
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
            with self._urlopen(req, timeout=3600) as resp:
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
            with self._urlopen(req, timeout=180) as resp:
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
            with self._urlopen(req, timeout=3600) as resp:
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
            with self._urlopen(req, timeout=180) as resp:
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

    def list_descendant_groups(self, group: str) -> list[GroupInfo]:
        """All subgroups under group (any depth). Never returns the root group itself."""
        encoded = encode_path(group)
        raw = self.paginate(
            f"/groups/{encoded}/descendant_groups",
            {"all_available": "true"},
        )
        groups: list[GroupInfo] = []
        for item in raw:
            full_path = item.get("full_path") or ""
            if not (full_path == group or full_path.startswith(group + "/")):
                continue
            if full_path == group:
                continue
            groups.append(
                GroupInfo(
                    full_path=full_path,
                    path=item.get("path") or full_path.rsplit("/", 1)[-1],
                    name=item.get("name") or item.get("path") or full_path,
                    visibility=item.get("visibility", "private"),
                    parent_id=item.get("parent_id"),
                )
            )
        seen: set[str] = set()
        unique: list[GroupInfo] = []
        for group_info in sorted(groups, key=lambda g: (g.full_path.count("/"), g.full_path)):
            if group_info.full_path in seen:
                continue
            seen.add(group_info.full_path)
            unique.append(group_info)
        return unique

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
        project_selectors: list[str] | None = None,
    ) -> None:
        self.client = client
        self.source_group = source_group.strip("/")
        self.target_group = target_group.strip("/")
        self.workdir = workdir
        self.poll_seconds = poll_seconds
        self.export_timeout = export_timeout
        self.import_timeout = import_timeout
        self.project_selectors = project_selectors or []
        self.run_started_at = utc_now()

        self.log_dir = workdir / "logs"
        self.state_dir = workdir / "state"
        self.export_dir = workdir / "exports"
        self.projects_file = workdir / "projects.txt"
        self.groups_file = workdir / "groups.txt"
        self.report_csv = workdir / "migration-report.csv"
        self.report_json = workdir / "migration-report.json"
        self.verify_csv = workdir / "hierarchy-verify.csv"
        self.verify_json = workdir / "hierarchy-verify.json"
        self.checklist = workdir / "POST_MIGRATION_CHECKLIST.md"
        self.main_log = self.log_dir / "main.log"
        for path in (self.log_dir, self.state_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)

        self._group_id_cache: dict[str, int] = {}
        self._interrupted = False
        self._target_root_depth = len(self.target_group.split("/"))
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

    def map_source_to_target(self, source_path: str) -> str:
        """Map any source group/project path to the equivalent target path."""
        if source_path == self.source_group:
            return self.target_group
        prefix = self.source_group + "/"
        if not source_path.startswith(prefix):
            raise ValueError(
                f"Path {source_path!r} is not under source group {self.source_group!r}"
            )
        return f"{self.target_group}/{source_path[len(prefix):]}"

    def map_target_paths(self, source_path: str) -> tuple[str, str]:
        """Return (target_namespace_path, project_slug)."""
        target_path = self.map_source_to_target(source_path)
        namespace, slug = target_path.rsplit("/", 1)
        return namespace, slug

    def target_project_path(self, source_path: str) -> str:
        return self.map_source_to_target(source_path)

    def verify_groups(self) -> None:
        self.log("INFO", f"Verifying source group: {self.source_group}")
        self.client.get_group(self.source_group)
        self.log("INFO", f"Verifying target group: {self.target_group}")
        self.client.get_group(self.target_group)
        self.log(
            "INFO",
            "Safety: never deletes/removes source or target groups/projects; "
            "existing target content is kept and skipped",
        )

    def ensure_subgroup(self, full_path: str, *, name: str | None = None, visibility: str = "private") -> int:
        """Reuse existing subgroup if present; create only when missing. Never deletes."""
        if full_path in self._group_id_cache:
            return self._group_id_cache[full_path]

        if full_path == self.target_group:
            group = self.client.get_group(full_path)
            self._group_id_cache[full_path] = int(group["id"])
            return int(group["id"])

        if not (
            full_path == self.target_group or full_path.startswith(self.target_group + "/")
        ):
            raise ValueError(f"Refusing to touch group outside target: {full_path}")

        try:
            group = self.client.get_group(full_path)
            self._group_id_cache[full_path] = int(group["id"])
            self.log("INFO", f"Reusing existing subgroup: {full_path}")
            return int(group["id"])
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise

        parts = full_path.split("/")
        parent_path = "/".join(parts[:-1])
        parent_id = self.ensure_subgroup(parent_path)
        slug = parts[-1]
        self.log("INFO", f"Creating missing subgroup: {full_path}")
        created = self.client.post_json(
            "/groups",
            {
                "name": name or slug,
                "path": slug,
                "parent_id": parent_id,
                "visibility": visibility or "private",
            },
        )
        group_id = int(created["id"])
        self._group_id_cache[full_path] = group_id
        return group_id

    def list_source_groups(self) -> list[GroupInfo]:
        groups = self.client.list_descendant_groups(self.source_group)
        with self.groups_file.open("w", encoding="utf-8") as fh:
            fh.write(self.source_group + "\n")
            for group in groups:
                fh.write(group.full_path + "\n")
        self.log("INFO", f"Found {len(groups)} subgroups under {self.source_group}")
        return groups

    def ensure_namespace_tree(
        self,
        projects: list[ProjectInfo],
        source_groups: list[GroupInfo],
    ) -> None:
        """Ensure every source subgroup path exists under target. Never removes extras."""
        namespaces: set[str] = set()

        for group in source_groups:
            target_path = self.map_source_to_target(group.full_path)
            parts = target_path.split("/")
            for idx in range(self._target_root_depth + 1, len(parts) + 1):
                namespaces.add("/".join(parts[:idx]))

        for project in projects:
            namespace, _ = self.map_target_paths(project.path_with_namespace)
            parts = namespace.split("/")
            for idx in range(self._target_root_depth + 1, len(parts) + 1):
                namespaces.add("/".join(parts[:idx]))

        # Prefer source subgroup metadata (name/visibility) when creating.
        source_by_target = {
            self.map_source_to_target(g.full_path): g for g in source_groups
        }

        for namespace in sorted(namespaces, key=lambda p: (p.count("/"), p)):
            if namespace == self.target_group:
                continue
            meta = source_by_target.get(namespace)
            self.ensure_subgroup(
                namespace,
                name=meta.name if meta else None,
                visibility=(meta.visibility if meta else "private"),
            )

    def list_projects(self) -> list[ProjectInfo]:
        projects = self.client.list_group_projects(self.source_group)
        if not projects:
            raise SystemExit(f"No projects found under source group {self.source_group!r}")
        total_discovered = len(projects)
        self.log(
            "INFO",
            f"Found {total_discovered} projects under {self.source_group} "
            "(including subgroups and archived)",
        )

        if self.project_selectors:
            projects = filter_projects(projects, self.project_selectors, self.source_group)
            self.log(
                "INFO",
                f"Project filter active: migrating {len(projects)}/{total_discovered} projects",
            )

        with self.projects_file.open("w", encoding="utf-8") as fh:
            for project in projects:
                fh.write(project.path_with_namespace + "\n")
        return projects

    def inspect_target_project(self, source_project: ProjectInfo) -> tuple[str, list[str]]:
        """
        Return (status, mismatch_notes).
        status: missing | present | importing | mismatch
        Existing target projects are never overwritten/deleted.
        """
        target_path = self.target_project_path(source_project.path_with_namespace)
        try:
            project = self.client.get_project(target_path)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return "missing", []
            raise

        import_status = project.get("import_status")
        if import_status not in (None, "none", "finished"):
            return "importing", [f"import_status={import_status}"]

        notes: list[str] = []
        if (project.get("path") or "") != source_project.path:
            notes.append(f"path {project.get('path')!r} != {source_project.path!r}")
        if (project.get("name") or "") != source_project.name:
            notes.append(f"name {project.get('name')!r} != {source_project.name!r}")
        if (project.get("visibility") or "") != source_project.visibility:
            notes.append(
                f"visibility {project.get('visibility')!r} != {source_project.visibility!r}"
            )
        if bool(project.get("archived", False)) != source_project.archived:
            notes.append(
                f"archived {bool(project.get('archived', False))} != {source_project.archived}"
            )

        expected_ns, _ = self.map_target_paths(source_project.path_with_namespace)
        actual_ns = project.get("namespace", {}).get("full_path") or ""
        if actual_ns and actual_ns != expected_ns:
            notes.append(f"namespace {actual_ns!r} != {expected_ns!r}")

        if notes:
            return "mismatch", notes
        return "present", []

    def target_ready(self, source_path: str) -> bool:
        """True if target project exists and is not mid-import. Never overwrites it."""
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
            self.log("INFO", f"Already migrated (state): {source_path}")
            return "success"

        status, notes = self.inspect_target_project(project)
        target_path = self.target_project_path(source_path)

        if status in {"present", "mismatch"}:
            # CRITICAL: never overwrite/delete existing target projects.
            if notes:
                self.log(
                    "WARN",
                    f"Target already present with differences (kept as-is): {target_path} — "
                    + "; ".join(notes),
                )
            else:
                self.log("INFO", f"Target already present — skipping import: {target_path}")
            sf.write_text("success", encoding="utf-8")
            return "success"

        if status == "importing":
            self.log("INFO", f"Target import already in progress: {target_path}")
            try:
                run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                log_file = self.log_dir / f"{source_path.replace('/', '__')}.{run_ts}.wait.log"
                self.wait_for_import(target_path, log_file)
                sf.write_text("success", encoding="utf-8")
                return "success"
            except Exception as exc:
                sf.write_text("failed", encoding="utf-8")
                self.log("ERROR", f"Wait for in-progress import failed for {target_path}: {exc}")
                return "failed"

        target_namespace, project_slug = self.map_target_paths(source_path)
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
                    # Never replace an existing target project.
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

    def verify_hierarchy(
        self,
        source_groups: list[GroupInfo],
        projects: list[ProjectInfo],
    ) -> list[dict[str, str]]:
        """
        Check every source group/subgroup/project has a matching target path.
        Does not remove any extra target groups/projects.
        """
        self.log("INFO", "Verifying full hierarchy match (groups + projects)")
        rows: list[dict[str, str]] = []

        # Root groups (top-level slug may differ by design, e.g. vendor vs vendor-lh2)
        try:
            self.client.get_group(self.source_group)
            self.client.get_group(self.target_group)
            rows.append(
                {
                    "kind": "group",
                    "source_path": self.source_group,
                    "target_path": self.target_group,
                    "match_status": "ok",
                    "notes": "top-level groups verified",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "kind": "group",
                    "source_path": self.source_group,
                    "target_path": self.target_group,
                    "match_status": "error",
                    "notes": str(exc),
                }
            )

        for group in source_groups:
            target_path = self.map_source_to_target(group.full_path)
            try:
                target = self.client.get_group(target_path)
                notes: list[str] = []
                if (target.get("path") or "") != group.path:
                    notes.append(f"path {target.get('path')!r} != {group.path!r}")
                if (target.get("visibility") or "") != group.visibility:
                    notes.append(
                        f"visibility {target.get('visibility')!r} != {group.visibility!r}"
                    )
                rows.append(
                    {
                        "kind": "group",
                        "source_path": group.full_path,
                        "target_path": target_path,
                        "match_status": "mismatch" if notes else "ok",
                        "notes": "; ".join(notes),
                    }
                )
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    rows.append(
                        {
                            "kind": "group",
                            "source_path": group.full_path,
                            "target_path": target_path,
                            "match_status": "missing",
                            "notes": "target subgroup not found",
                        }
                    )
                else:
                    rows.append(
                        {
                            "kind": "group",
                            "source_path": group.full_path,
                            "target_path": target_path,
                            "match_status": "error",
                            "notes": str(exc),
                        }
                    )

        for project in projects:
            target_path = self.target_project_path(project.path_with_namespace)
            status, notes = self.inspect_target_project(project)
            match_status = {
                "present": "ok",
                "mismatch": "mismatch",
                "missing": "missing",
                "importing": "importing",
            }.get(status, status)
            rows.append(
                {
                    "kind": "project",
                    "source_path": project.path_with_namespace,
                    "target_path": target_path,
                    "match_status": match_status,
                    "notes": "; ".join(notes),
                }
            )

        # Report extras on target (kept; never deleted)
        try:
            target_groups = self.client.list_descendant_groups(self.target_group)
            expected_group_targets = {
                self.map_source_to_target(g.full_path) for g in source_groups
            }
            for group in target_groups:
                if group.full_path not in expected_group_targets:
                    rows.append(
                        {
                            "kind": "group",
                            "source_path": "",
                            "target_path": group.full_path,
                            "match_status": "extra_on_target",
                            "notes": "present only on target — kept (not removed)",
                        }
                    )

            target_projects = self.client.list_group_projects(self.target_group)
            expected_project_targets = {
                self.target_project_path(p.path_with_namespace) for p in projects
            }
            for project in target_projects:
                if project.path_with_namespace not in expected_project_targets:
                    rows.append(
                        {
                            "kind": "project",
                            "source_path": "",
                            "target_path": project.path_with_namespace,
                            "match_status": "extra_on_target",
                            "notes": "present only on target — kept (not removed)",
                        }
                    )
        except Exception as exc:
            self.log("WARN", f"Could not enumerate extras on target: {exc}")

        with self.verify_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["kind", "source_path", "target_path", "match_status", "notes"],
            )
            writer.writeheader()
            writer.writerows(rows)
        self.verify_json.write_text(json.dumps(rows, indent=2), encoding="utf-8")

        counts: dict[str, int] = {}
        for row in rows:
            counts[row["match_status"]] = counts.get(row["match_status"], 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        self.log("INFO", f"Hierarchy verify summary: {summary}")
        self.log("INFO", f"Hierarchy verify report: {self.verify_csv}")
        return rows

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

## Safety guarantees
- Source groups/projects are **never** deleted, moved, or modified
- Target groups/projects that already exist are **never** overwritten or removed
- Extra subgroups/projects already on target are **kept**
- Import uses `overwrite=false`
- See `{self.verify_csv}` for full group/subgroup/project match results

## Cannot be replicated automatically (manual follow-up)
- CI/CD **variables and secrets**
- **Pipeline run history**
- Container registry and package registry
- Webhooks, deploy keys, runners
- Group-level permissions and SAML settings

## Manual verification
- [ ] Review `{self.verify_csv}` for any `missing` / `mismatch` rows
- [ ] Spot-check MR threads with review comments
- [ ] Spot-check issues with attachments
- [ ] Recreate CI/CD variables and secrets on target projects
- [ ] Recreate webhooks, branch protection, and environments
- [ ] Search/replace hardcoded `{self.source_group}` URLs in CI and submodules

Reports: `{self.report_csv}`
Verify: `{self.verify_csv}`
Logs: `{self.log_dir}`
"""
        self.checklist.write_text(checklist, encoding="utf-8")
        self.log("INFO", f"Report: {self.report_csv}")
        self.log("INFO", f"Checklist: {self.checklist}")

    def run(self) -> int:
        if self.source_group == self.target_group:
            raise SystemExit("Source and target group must differ")

        self.log("INFO", f"GitLab group replication: {self.source_group} -> {self.target_group}")
        self.log("INFO", f"API base: {self.client.api}")
        if self.client.insecure:
            self.log("WARN", "TLS certificate verification disabled (--insecure)")
        self.log("INFO", f"Work directory: {self.workdir} (stable — safe to re-run)")
        self.log("INFO", "Source/target existing content is never deleted or overwritten")
        if self.project_selectors:
            self.log(
                "INFO",
                f"Using project allowlist ({len(self.project_selectors)} selector(s))",
            )

        self.verify_groups()
        source_groups = self.list_source_groups()
        projects = self.list_projects()
        if self.project_selectors:
            source_groups = subgroups_needed_for_projects(
                source_groups, projects, self.source_group
            )
            self.log(
                "INFO",
                f"Creating/reusing {len(source_groups)} subgroup(s) needed for selected projects",
            )
        self.ensure_namespace_tree(projects, source_groups)

        total = len(projects)
        failed = 0
        for idx, project in enumerate(projects, start=1):
            if self._interrupted:
                break
            self.log("INFO", f"=== Project {idx}/{total}: {project.path_with_namespace} ===")
            status = self.migrate_project(project)
            if status != "success":
                failed += 1

        verify_rows = self.verify_hierarchy(source_groups, projects)
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
        missing = sum(1 for row in verify_rows if row["match_status"] == "missing")
        mismatched = sum(1 for row in verify_rows if row["match_status"] == "mismatch")

        self.log("INFO", f"Done: {success}/{total} projects migrated/skipped successfully")
        self.log(
            "INFO",
            f"Hierarchy check: missing={missing}, mismatch={mismatched} "
            f"(extras on target are kept)",
        )
        if failed or missing:
            self.log(
                "ERROR",
                f"{failed} project migrate failure(s), {missing} missing path(s) — "
                f"see {self.report_csv} and {self.verify_csv}",
            )
            return 1
        if mismatched:
            self.log(
                "WARN",
                f"{mismatched} path(s) exist with attribute differences — "
                f"left unchanged; see {self.verify_csv}",
            )
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replicate a GitLab group via project export/import "
            "(gitlab.com or self-hosted)."
        )
    )
    parser.add_argument("--source-group", required=True, help="Source top-level group path")
    parser.add_argument("--target-group", required=True, help="Target top-level group path (must exist)")
    parser.add_argument("--token", help="GitLab personal access token")
    parser.add_argument("--tokens-file", type=Path, help="File containing gitlab_token=...")
    parser.add_argument(
        "--gitlab-host",
        default="gitlab.com",
        help=(
            "GitLab hostname or base URL for gitlab.com or self-hosted "
            "(e.g. gitlab.example.com, https://gitlab.example.com, "
            "https://company.com/gitlab, or .../api/v4)"
        ),
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (self-signed / private CA hosts)",
    )
    parser.add_argument(
        "--project",
        action="append",
        dest="projects",
        default=[],
        metavar="PATH",
        help=(
            "Project to migrate (repeatable). Accepts full path_with_namespace "
            "or a path relative to --source-group"
        ),
    )
    parser.add_argument(
        "--projects-file",
        type=Path,
        help=(
            "Text file with one project path per line (# comments allowed). "
            "Same path rules as --project"
        ),
    )
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
    project_selectors = load_path_list(args.projects, args.projects_file)

    client = GitLabClient(token=token, host=args.gitlab_host, insecure=args.insecure)
    replicator = GroupReplicator(
        client=client,
        source_group=source,
        target_group=target,
        workdir=workdir,
        poll_seconds=args.poll_seconds,
        export_timeout=args.export_timeout,
        import_timeout=args.import_timeout,
        project_selectors=project_selectors,
    )
    return replicator.run()


if __name__ == "__main__":
    sys.exit(main())
