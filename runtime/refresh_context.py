"""Stage, validate, and publish a registered project's private digest."""

import argparse
import contextlib
import datetime as dt
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from context_store import RefreshPermissions as Permissions, RefreshSettings, atomic_text, clean, digest, linked_path, load_settings, lock, read_text, save_model
from refresh_health import SOURCE_NAMES, Outcome, Reason, RefreshState, SourceHealth, SourceName

ROOT_FILES = ("brief.md", "people.md", "decisions.md", "inbox.md", "sources.md")
FOLDERS = ("projects", "customers", "meetings", "source-updates")
MAX_BYTES = 2_000_000
MEETING_DAYS = 90
MEETING_COUNT = 160
NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*\.md$")
INBOX_MARKER = re.compile(r"(?m)^<!-- agent-context-refresh:[a-f0-9]{64} -->\n?")


class Document(BaseModel):
    path: str
    text: str


class Snapshot(BaseModel):
    documents: list[Document]


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: SourceName
    status: Literal["ok", "empty", "partial", "unavailable", "error", "needs-sign-in", "truncated", "failed"]
    detail: str = Field(default="", max_length=600)


class SourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    sources: list[SourceStatus]


class AgentResult(BaseModel):
    subtype: str
    is_error: bool
    total_cost_usd: float = 0


class Transaction(BaseModel):
    run_id: str
    before: Snapshot
    after: Snapshot
    commits_coverage: bool = False


class SourceFailure(ValueError):
    def __init__(self, outcomes: dict[SourceName, Outcome], message: str):
        super().__init__(message)
        self.outcomes = outcomes


class PermissionSettings(BaseModel):
    permissions: Permissions


class MCPConfig(BaseModel):
    mcpServers: dict[str, JsonValue] = Field(default_factory=dict)


class AgentSpec(BaseModel):
    description: str
    prompt: str
    tools: list[str]
    model: str = "sonnet"


class GithubSnapshot(BaseModel):
    mine: list[JsonValue]
    review_requested: list[JsonValue]
    involving_user: list[JsonValue]
    merged_in_window: list[JsonValue]
    other_open: list[JsonValue]


def allowed_document(relative: str) -> bool:
    parts = Path(relative).parts
    if parts and parts[0] == "source-updates":
        return len(parts) == 2 and parts[1] in {name + ".md" for name in SOURCE_NAMES}
    return relative in ROOT_FILES or (len(parts) == 2 and parts[0] in FOLDERS and bool(NAME.fullmatch(parts[1])))


def snapshot(root: Path) -> Snapshot:
    documents: list[Document] = []
    paths = [root / name for name in ROOT_FILES]
    for folder in FOLDERS:
        if linked_path(root / folder):
            raise ValueError("Digest folders must be real directories.")
        paths.extend(sorted((root / folder).glob("*.md")))
    total = 0
    for path in paths:
        if linked_path(path):
            raise ValueError("Digest files must be regular files.")
        if not path.exists():
            continue
        relative = str(path.relative_to(root))
        if not allowed_document(relative) or not path.is_file() or path.stat().st_size > 100_000:
            raise ValueError(f"Unsupported digest file: {relative}")
        total += path.stat().st_size
        if total > MAX_BYTES or len(documents) >= 280:
            raise ValueError("Digest exceeds its storage budget.")
        documents.append(Document(path=relative, text=path.read_text(encoding="utf-8")))
    return Snapshot(documents=documents)


def apply_snapshot(root: Path, value: Snapshot, managed: set[str]) -> None:
    contents = {item.path: item.text for item in value.documents}
    if any(not allowed_document(name) for name in managed | contents.keys()):
        raise ValueError("Snapshot contains an unsupported path.")
    for name in sorted(managed | contents.keys()):
        path = root / name
        if linked_path(path):
            raise ValueError("Refusing to publish through a symlink.")
        if name in contents:
            if not path.is_file() or path.read_bytes() != contents[name].encode():
                atomic_text(path, contents[name])
        else:
            path.unlink(missing_ok=True)


def stage_manifest(stage: Path, run_id: str, expected: set[str]) -> SourceManifest:
    for path in stage.rglob("*"):
        relative = str(path.relative_to(stage))
        if linked_path(path):
            raise ValueError("Staged context cannot contain symlinks.")
        if path.is_dir() and relative in (*FOLDERS, ".cache"):
            continue
        if path.is_file() and (allowed_document(relative) or relative in {".cache/github.json", "source-status.json"}):
            continue
        raise ValueError(f"Unexpected staged path: {relative}")
    status_path = stage / "source-status.json"
    if status_path.stat().st_size > 8000:
        raise ValueError("Source manifest is too large.")
    manifest = SourceManifest.model_validate_json(status_path.read_text())
    if manifest.run_id != run_id or len(manifest.sources) != len(expected) or {item.source for item in manifest.sources} != expected:
        raise ValueError("Source manifest must cover this run and exactly the requested sources.")
    return manifest


def validate_documents(value: Snapshot, before: Snapshot, context_root: Path | None, config: RefreshSettings) -> Snapshot:
    old = {item.path: item.text for item in before.documents}
    contents = {item.path: item.text for item in value.documents}
    if set(ROOT_FILES) - contents.keys():
        raise ValueError("Staged context is missing a core digest file.")
    if "sources.md" in old and old["sources.md"].split("## Discovered", 1)[0] != contents["sources.md"].split("## Discovered", 1)[0]:
        raise ValueError("Pinned source definitions changed.")
    for item in value.documents:
        if clean(item.text, 100_001) != item.text:
            raise ValueError(f"Credential pattern in staged file: {item.path}")
        if item.path == "brief.md":
            limit = 80
        elif item.path == "sources.md":
            limit = 200
        elif item.path == "inbox.md":
            limit = 300
        else:
            limit = 150
        if len(item.text.splitlines()) > limit or len(item.text.encode()) > 24_000:
            raise ValueError(f"Context size limit exceeded: {item.path}")
        if re.search(r"(?:^|\s)@[/~.]", item.text) or "\x00" in item.text:
            raise ValueError(f"Invalid context content: {item.path}")
    if len([name for name in contents if name.startswith("projects/")]) > 40:
        raise ValueError("Project context needs consolidation to 40 files.")
    if len([name for name in contents if name.startswith("customers/")]) > 60:
        raise ValueError("Customer context needs consolidation to 60 files.")
    return retain_meetings(value, context_root=context_root, days=config.meeting_days, count=config.meeting_count)


def validate_stage(stage: Path, before: Snapshot, run_id: str, context_root: Path | None = None,
                   *, config: RefreshSettings | None = None) -> Snapshot:
    config = config or RefreshSettings()
    expected = config.enabled_sources()
    manifest = stage_manifest(stage, run_id, expected)
    statuses = {item.source: item.status for item in manifest.sources}
    good = {name for name, status in statuses.items() if status in {"ok", "empty"}}
    if not good:
        raise ValueError("No enabled source completed collection.")
    staged = snapshot(stage)
    validate_documents(staged, before, context_root, config)
    return assemble_digest(initial_digest(before), staged, statuses, good == expected, context_root, config)


def local_references(document: Document, context_root: Path | None = None) -> set[str]:
    targets = re.findall(r"\]\(\s*(<[^>]+>|[^\s)]+)", document.text)
    targets.extend(re.findall(r"(?m)^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)", document.text))
    references: set[str] = set()
    for target in targets:
        url = urlsplit(target.strip("<>"))
        if url.scheme or url.netloc:
            continue
        path = unquote(url.path)
        if not path.endswith(".md"):
            continue
        root = context_root or Path.home() / "context"
        prefixes = [str(root) + "/"]
        if root.is_relative_to(Path.home()):
            prefixes.append("~/" + str(root.relative_to(Path.home())) + "/")
        for prefix in prefixes:
            if path.startswith(prefix):
                path = "~/context/" + path.removeprefix(prefix)
                break
        rooted = path.startswith(("~/context/", "/"))
        if path.startswith("~/context/"):
            path = path.removeprefix("~/context/")
        if rooted or path.lstrip("/").startswith(tuple(folder + "/" for folder in FOLDERS)):
            resolved = posixpath.normpath(path.lstrip("/"))
        else:
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document.path), path))
        references.add(resolved)
    return references


def reference_closure(roots: set[str], references: dict[str, set[str]]) -> set[str]:
    visited: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        pending.extend(references.get(name, set()) - visited)
    return visited


def retain_meetings(value: Snapshot, today: dt.date | None = None, *, context_root: Path | None = None,
                    days: int = MEETING_DAYS, count: int = MEETING_COUNT) -> Snapshot:
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=days)).isoformat()
    references = {item.path: local_references(item, context_root) for item in value.documents}
    meetings = {item.path for item in value.documents if item.path.startswith("meetings/")}
    keep = reference_closure(references.keys() - meetings, references)
    if len(keep & meetings) > count:
        raise ValueError(f"Consolidate old meeting references before exceeding {count} meetings.")
    for name in sorted(meetings, reverse=True):
        if Path(name).name[:10] < cutoff:
            continue
        candidate = keep | reference_closure({name}, references)
        if len(candidate & meetings) <= count:
            keep = candidate
    for name in keep & references.keys():
        missing = references[name] - references.keys()
        if missing:
            raise ValueError(f"Broken digest reference in {name}: {sorted(missing)[0]}")
    return Snapshot(documents=[item for item in value.documents if item.path in keep])


def merge_inbox(before: str, staged: str, current: str) -> str:
    if not current.startswith(before):
        raise ValueError("Inbox changed beyond an append; refresh will retry later.")
    suffix = current[len(before):]
    separator = "\n" if suffix and staged and not staged.endswith("\n") else ""
    return staged + separator + suffix


def recover(root: Path) -> bool:
    path = root / "local/refresh/pending.json"
    if not path.exists():
        return False
    transaction = Transaction.model_validate_json(path.read_text())
    published = root / "local/refresh/published.json"
    if transaction.commits_coverage and published.exists():
        commit = RefreshState.model_validate_json(read_text(published, 16384))
        if commit.run_id == transaction.run_id:
            path.unlink()
            return True
    current = {item.path: item.text for item in snapshot(root).documents}
    before = {item.path: item.text for item in transaction.before.documents}
    after = {item.path: item.text for item in transaction.after.documents}
    restored: list[Document] = []
    for name in before.keys() | after.keys():
        actual = current.get(name)
        if name == "inbox.md" and actual is not None:
            expected = after.get(name, "") if actual.startswith(after.get(name, "")) else before.get(name, "")
            before[name] = merge_inbox(expected, before.get(name, ""), actual)
        elif actual not in (before.get(name), after.get(name)):
            raise ValueError(f"Interrupted refresh has a concurrent edit in {name}; recovery snapshot retained.")
        if name in before:
            restored.append(Document(path=name, text=before[name]))
    apply_snapshot(root, Snapshot(documents=restored), before.keys() | after.keys())
    path.unlink()
    return True


def publish(root: Path, before: Snapshot, staged: Snapshot, run_id: str, receipt: RefreshState | None = None) -> None:
    with lock(root / "local/publish.lock"):
        recover(root)
        current = snapshot(root)
        old = {item.path: item.text for item in before.documents}
        live = {item.path: item.text for item in current.documents}
        if {name: text for name, text in live.items() if name != "inbox.md"} != {name: text for name, text in old.items() if name != "inbox.md"}:
            raise ValueError("Context changed during refresh; keeping those edits.")
        documents = [item.model_copy() for item in staged.documents]
        inbox = next(item for item in documents if item.path == "inbox.md")
        inbox.text = merge_inbox(old.get("inbox.md", ""), inbox.text, live.get("inbox.md", ""))
        # A unique prefix distinguishes an applied inbox rewrite during recovery.
        inbox.text = f"<!-- agent-context-refresh:{digest(run_id)} -->\n" + INBOX_MARKER.sub("", inbox.text)
        after = Snapshot(documents=documents)
        names = {item.path for item in after.documents} | live.keys()
        transaction = Transaction(run_id=run_id, before=current, after=after, commits_coverage=receipt is not None)
        pending = root / "local/refresh/pending.json"
        save_model(root / "local/refresh/last-valid.json", current)
        save_model(pending, transaction)
        try:
            apply_snapshot(root, after, names)
            if receipt is not None:
                save_model(root / "local/refresh/published.json", receipt)
            pending.unlink()
        except BaseException:
            recover(root)
            raise


def command(args: list[str], *, cwd: Path | None = None, check: bool = True,
            env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise ValueError(f"Source command failed (exit {result.returncode}).")
    return result


def utc_stamp(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def github_rows(args: list[str], limit: int) -> list[dict]:
    result = command(["gh", *args, "--limit", str(limit)], check=False)
    if result.returncode:
        diagnostic = result.stderr.lower()
        auth = any(token in diagnostic for token in ("http 401", "bad credentials", "gh auth login", "not logged"))
        raise SourceFailure({"github": "needs-sign-in" if auth else "unavailable"}, "GitHub request failed.")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("number"), int) for row in rows):
        raise ValueError("GitHub returned an invalid snapshot.")
    return rows


def github_history(repo: str, user: str, start: int, end: int, *, merged: bool) -> list[dict]:
    # Search endpoints stop at 1,000 items. Bisect full windows, including
    # the one-second boundary case, and bound work even on very busy repos.
    pending = [(start, end)]
    records = {}
    for _ in range(63):
        lower, upper = pending.pop()
        interval = f"{utc_stamp(lower)}..{utc_stamp(upper)}"
        if merged:
            query = ["pr", "list", "-R", repo, "--state", "merged", "--search", f"merged:{interval}",
                     "--json", "number,title,url,author,mergedAt"]
        else:
            query = ["search", "prs", "--repo", repo, "--involves", user, "--updated", interval,
                     "--json", "number,title,url,author,state,updatedAt"]
        page = github_rows(query, 1000)
        if len(page) < 1000:
            records.update((row["number"], row) for row in page)
        elif lower < upper:
            middle = (lower + upper) // 2
            pending.extend([(middle + 1, upper), (lower, middle)])
        else:
            break
        if len(records) > 10000:
            break
        if not pending:
            return list(records.values())
    raise SourceFailure({"github": "truncated"}, "GitHub history exceeds one collection budget.")


def github_snapshot(since: float, repo: str, user: str, *, until: float, inventory_limit: int) -> GithubSnapshot:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[A-Za-z0-9-]+", user):
        raise SourceFailure({"github": "unavailable"}, "Configure the GitHub repository and user.")
    # gh pages this unfiltered inventory. Request one extra to distinguish
    # an exact fit from incomplete coverage, independently of the history window.
    opened = github_rows(["pr", "list", "-R", repo, "--state", "open", "--json",
                          "number,title,url,author,isDraft,reviewDecision,reviewRequests,updatedAt"], inventory_limit + 1)
    if len(opened) > inventory_limit:
        raise SourceFailure({"github": "truncated"}, "GitHub open inventory exceeds the configured limit.")
    mine = [row for row in opened if (row.get("author") or {}).get("login", "").casefold() == user.casefold()]
    requested = [row for row in opened if any(r.get("login", "").casefold() == user.casefold() for r in row.get("reviewRequests", []))]
    return GithubSnapshot(mine=mine, review_requested=requested, other_open=opened,
                          involving_user=github_history(repo, user, int(since), int(until), merged=False),
                          merged_in_window=github_history(repo, user, int(since), int(until), merged=True))


def agents(tools_root: Path, permissions: Permissions, servers: dict[SourceName, str], model: str) -> str:
    common = (tools_root / "agents/common.md").read_text()
    specs: dict[str, AgentSpec] = {}
    for name, server in servers.items():
        specs[f"{name}-gatherer"] = AgentSpec(
            description=f"Read {name} for dated, linked facts and report source coverage.", model=model,
            prompt=common + "\n" + (tools_root / f"agents/{name}.md").read_text(),
            tools=[tool for tool in permissions.allow if tool.startswith(f"mcp__{server}__")],
        )
    return json.dumps({name: item.model_dump() for name, item in specs.items()})


def settings_for_stage(source: PermissionSettings, stage: Path) -> PermissionSettings:
    permissions = source.permissions.model_copy(deep=True)
    permissions.allow = [rule for rule in permissions.allow if rule.startswith("mcp__")]
    permissions.allow.extend([f"Read(/{stage}/**)", f"Write(/{stage}/**)", f"Edit(/{stage}/**)", "Agent", "TodoWrite"])
    return PermissionSettings(permissions=permissions)


class Connections(BaseModel):
    permissions: PermissionSettings
    servers: dict[SourceName, str]
    mcp: MCPConfig


def connections(tools_root: Path, config: RefreshSettings) -> Connections:
    path = tools_root / "settings.json"
    permissions = PermissionSettings(permissions=Permissions(allow=[], deny=[]))
    if config.permissions is not None:
        permissions = PermissionSettings(permissions=config.permissions.model_copy(deep=True))
    elif path.exists():
        permissions = PermissionSettings.model_validate_json(path.read_text())
    wanted = config.servers()
    configured = MCPConfig()
    if wanted:
        try:
            configured = MCPConfig.model_validate_json((Path.home() / ".claude.json").read_text())
        except (OSError, ValueError):
            pass  # Each affected source will be marked unavailable.
    available = {name: server for name, server in wanted.items() if server in configured.mcpServers
                 and any(tool.startswith(f"mcp__{server}__") for tool in permissions.permissions.allow)}
    prefixes = tuple(f"mcp__{server}__" for server in available.values())
    permissions.permissions.allow = [rule for rule in permissions.permissions.allow if rule.startswith(prefixes)]
    return Connections(permissions=permissions, servers=available,
                       mcp=MCPConfig(mcpServers={server: configured.mcpServers[server] for server in available.values()}))


def run_agent(stage: Path, tools_root: Path, prompt: str, budget: float, timeout: int,
              stdout_path: Path, stderr_path: Path, *, active: Connections | None = None, model: str = "sonnet") -> AgentResult:
    active = active or connections(tools_root, RefreshSettings())
    with tempfile.TemporaryDirectory(prefix="config-", dir=tools_root) as configuration:
        config_root = Path(configuration)
        save_model(config_root / "settings.json", settings_for_stage(active.permissions, stage))
        save_model(config_root / "mcp.json", active.mcp)
        env = dict(os.environ)
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            env.pop(key, None)
        env["AGENT_CONTEXT_REFRESH"] = "1"
        env["AGENT_CONTEXT_WORKER"] = "1"
        executable = shutil.which("claude")
        if executable is None:
            candidate = Path.home() / ".local/bin/claude"
            executable = str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        if executable is None:
            raise ValueError("Install and sign in to Claude Code before enabling refresh.")
        args = [executable, "-p", prompt, "--no-session-persistence", "--restricted", "--strict-mcp-config", "--mcp-config", str(config_root / "mcp.json"),
                "--tools", "Read,Write,Edit,Glob,Grep,Agent,TodoWrite", "--settings", str(config_root / "settings.json"), "--agents", agents(tools_root, active.permissions.permissions, active.servers, model),
                "--permission-mode", "dontAsk", "--model", model, "--max-budget-usd", str(budget), "--output-format", "json"]
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            process = subprocess.Popen(args, cwd=stage, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                rc = process.wait(timeout=timeout)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                raise
    result = AgentResult.model_validate_json(stdout_path.read_text())
    if rc or result.is_error or result.subtype != "success":
        raise ValueError(f"Refresh agent failed: exit={rc}, subtype={result.subtype}")
    return result


def prune_logs(tools_root: Path) -> None:
    threshold = time.time() - 30 * 86400
    for path in (tools_root / "logs").glob("20*"):
        if path.is_file() and not linked_path(path) and path.stat().st_mtime < threshold:
            path.unlink()
    for directory in (tools_root / "staging", tools_root):
        pattern = "staging-*" if directory.name == "staging" else "config-*"
        for path in directory.glob(pattern):
            if path.is_dir() and not linked_path(path) and path.stat().st_mtime < time.time() - 86400:
                shutil.rmtree(path)


def prior_receipt(root: Path, name: str) -> RefreshState | None:
    path = root / "local/refresh" / name
    return RefreshState.model_validate_json(read_text(path, 16384)) if path.exists() else None


def source_windows(config: RefreshSettings, now: float, previous: RefreshState | None,
                   attempt: RefreshState | None, legacy: float, bootstrap: bool) -> tuple[dict, dict]:
    sources = {}
    windows = {}
    limit = config.max_window_days * 86400
    for name in sorted(SOURCE_NAMES):
        old = previous.sources.get(name) if previous else None
        if old is None:
            old = SourceHealth(through=legacy, last_success=previous.completed if previous and legacy else 0)
        # A never-successful source retains the first missed interval across retries.
        first = (attempt.sources.get(name) if attempt else None)
        start = old.through or old.since or (first.since if first else 0) or now - config.bootstrap_days * 86400
        if old.through and now - start < limit:
            start -= 3600
        if bootstrap and (previous is None or previous.status == "succeeded"):
            start = min(start, now - config.bootstrap_days * 86400)
        start = max(0, min(start, now))
        sources[name] = old.model_copy(update={"status": "disabled", "since": start})
        if name in config.enabled_sources():
            sources[name].status = "unknown"
            windows[name] = (start, min(now, start + limit))
    return sources, windows


def refresh_prompt(stage: Path, tools: Path, run_id: str, mode: str, now: float, available: set[str], windows: dict) -> str:
    schedule = "\n".join(f"{name}: {utc_stamp(windows[name][0])} through {utc_stamp(windows[name][1])}"
                         for name in sorted(available))
    return (f"Now: {utc_stamp(now)}\nMode: {mode}\nRun ID: {run_id}\nStaging directory: {stage}\n"
            + (tools / "prompt.md").read_text()
            + "\nRequired runner contract takes precedence over older template output instructions. "
            "Collect only these sources, each for its own inclusive UTC interval:\n" + schedule
            + "\nUse the supplied .cache/github.json for GitHub when present. Open PRs are a current inventory. "
            "Write source-status.json with this run_id and a sources array of exactly the listed source names. "
            "Each entry has source and status (ok, empty, needs-sign-in, unavailable, truncated, failed). "
            "Empty means a successful complete search with no changes. Mark incomplete searches truncated. "
            "For every ok source write source-updates/<source>.md, a concise rolling note with dated, linked "
            "facts and earlier still-relevant evidence from its staged note. Merge overlap by source link. "
            "Keep these notes self-contained and use external source links. Preserve previous verification dates. "
            "Update the combined Markdown digest only from verified information. Keep pinned sources intact.")


def initial_digest(before: Snapshot) -> Snapshot:
    existing = {item.path for item in before.documents}
    missing = [Document(path=name, text=f"# {Path(name).stem.title()}\n") for name in ROOT_FILES if name not in existing]
    return Snapshot(documents=[*before.documents, *missing])


def assemble_digest(before: Snapshot, staged: Snapshot, statuses: dict, full: bool,
                    root: Path | None, config: RefreshSettings) -> Snapshot:
    # With incomplete coverage there is no reliable attribution for text in an
    # older blended brief. Preserve it verbatim and publish only verified source notes.
    baseline = staged if full else before
    docs = {item.path: item for item in baseline.documents if not item.path.startswith("source-updates/")}
    notes = {item.path: item for item in staged.documents if item.path.startswith("source-updates/")}
    for item in before.documents:
        if item.path.startswith("source-updates/"):
            docs[item.path] = item
    for name, status in statuses.items():
        if status == "ok":
            path = f"source-updates/{name}.md"
            if path not in notes or not notes[path].text.strip():
                raise ValueError("A successful source requires a dated evidence note.")
            docs[path] = notes[path]
    result = Snapshot(documents=list(docs.values()))
    checked = validate_documents(result, before, root, config)
    # Retention is applied to a complete synthesis; a partial pass must not
    # prune a previous meeting because its source could not be consulted.
    return checked if full else result


def refresh(root: Path, tools_root: Path, bootstrap: bool, *, config: RefreshSettings | None = None,
            github_repo: str = "", github_user: str = "") -> None:
    config = config or RefreshSettings(github_repo=github_repo, github_user=github_user)
    enabled = config.enabled_sources()
    if not enabled:
        print("Context refresh skipped: no sources enabled.")
        return
    logs = tools_root / "logs"
    with lock(root / "local/refresh/job.lock", blocking=False), lock(tools_root / "refresh.lock", blocking=False):
        started = time.time()
        with lock(root / "local/publish.lock"):
            recover(root)
            before = snapshot(root)
        previous = prior_receipt(root, "published.json") or prior_receipt(root, "success.json")
        attempt = prior_receipt(root, "latest.json")
        legacy = 0
        if previous and not previous.sources:
            legacy = previous.started or previous.since or max(0, previous.completed - 5400)
        elif not previous and not attempt and (root / ".git").exists():
            value = command(["git", "-C", str(root), "log", "-1", "--format=%ct", "--grep=^refresh:"], check=False).stdout.strip()
            legacy = max(0, float(value) - 5400) if value.isdigit() else 0
        sources, windows = source_windows(config, started, previous, attempt, legacy, bootstrap)
        mode = "bootstrap" if bootstrap or not (previous or legacy) else "incremental"
        budget, timeout = ((config.bootstrap_budget_usd, config.bootstrap_timeout) if mode == "bootstrap"
                           else (config.incremental_budget_usd, config.incremental_timeout))
        since = min(lower for lower, _ in windows.values())
        run_id = time.strftime("%Y-%m-%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        reason: Reason = "setup"
        latest = root / "local/refresh/latest.json"
        try:
            logs.mkdir(parents=True, exist_ok=True, mode=0o700)
            prune_logs(tools_root)
            active = connections(tools_root, config)
            available = set(active.servers)
            for name in enabled - available - {"github"}:
                sources[name].status = "unavailable"
            staging = tools_root / "staging"
            staging.mkdir(exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="staging-", dir=staging) as temporary:
                stage = Path(temporary)
                base = initial_digest(before)
                apply_snapshot(stage, base, set())
                (stage / "source-updates").mkdir(exist_ok=True, mode=0o700)
                if "github" in enabled:
                    reason = "source"
                    try:
                        lower, upper = windows["github"]
                        github = github_snapshot(lower, config.github_repo, config.github_user, until=upper,
                                                 inventory_limit=config.github_inventory_limit)
                        save_model(stage / ".cache/github.json", github)
                        available.add("github")
                    except SourceFailure as error:
                        sources["github"].status = error.outcomes["github"]
                    except (OSError, ValueError, subprocess.SubprocessError):
                        sources["github"].status = "failed"
                if not available:
                    raise SourceFailure({}, "No enabled source is available.")
                prompt = refresh_prompt(stage, tools_root, run_id, mode, started, available, windows)
                reason = "agent"
                result = run_agent(stage, tools_root, prompt, budget, timeout, logs / f"{run_id}.json",
                                   logs / f"{run_id}.err", active=active, model=config.model)
                reason = "validation"
                manifest = stage_manifest(stage, run_id, available)
                staged = snapshot(stage)
                validate_documents(staged, base, root, config)
                outcomes = {item.source: {"error": "failed", "partial": "truncated"}.get(item.status, item.status)
                            for item in manifest.sources}
                for name, outcome in outcomes.items():
                    if outcome not in {"ok", "empty"}:
                        sources[name].status = outcome
                good = {name for name, outcome in outcomes.items() if outcome in {"ok", "empty"}}
                if not good:
                    raise SourceFailure({}, "No enabled source completed collection.")
                full = good == enabled and all(windows[name][1] >= started for name in good)
                after = assemble_digest(base, staged, outcomes, full, root, config)
                committed = {name: source.model_copy() for name, source in sources.items()}
                for name in good:
                    committed[name] = SourceHealth(status=outcomes[name], last_success=started,
                                                   through=windows[name][1], since=windows[name][0])
                state = RefreshState(run_id=run_id, status="succeeded" if full else "partial", started=started,
                                     completed=time.time(), since=since, mode=mode, sources=committed)
                reason = "publication"
                publish(root, before, after, run_id, receipt=state)
            if full:
                save_model(root / "local/refresh/success.json", state)
            save_model(latest, state)
            print(f"Context refresh {state.status}: {run_id}, cost=${result.total_cost_usd:.2f}")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            if isinstance(error, SourceFailure):
                reason = "source"
            elif isinstance(error, subprocess.TimeoutExpired):
                reason = "timeout"
            state = RefreshState(run_id=run_id, status="failed", started=started, completed=time.time(), since=since,
                                 mode=mode, reason=reason, sources=sources)
            save_model(latest, state)
            details = ", ".join(f"{name} {item.status}" for name, item in sources.items()
                                if item.status not in {"unknown", "disabled"})
            print(f"Context refresh failed ({details or reason}). Check agent-context status for the refresh project.")
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--validate-stage", type=Path)
    parser.add_argument("--run-id", default="validation")
    parser.add_argument("--settings", type=Path, default=Path.home() / ".config/agent-context/settings.json")
    args = parser.parse_args()
    started = time.time()
    root = None
    try:
        settings = load_settings(args.settings)
        if not args.validate_stage and not settings.refresh.enabled:
            print("Context refresh is disabled; configure sources before enabling it.")
            return 0
        target = settings.refresh.project
        if target is None or settings.project(target) is None:
            raise ValueError("Configure a registered refresh project before refreshing or validating a digest.")
        root = settings.project_root(target)
        if args.validate_stage:
            validate_stage(args.validate_stage, snapshot(root), args.run_id, root, config=settings.refresh)
        else:
            tools_root = settings.refresh.resource_root
            if tools_root is None or not tools_root.is_dir() or linked_path(tools_root):
                raise ValueError("Configure a real refresh resource directory before enabling refresh.")
            refresh(root, tools_root, args.bootstrap, config=settings.refresh)
    except BlockingIOError:
        print("Context refresh skipped: another refresh is active.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if root is not None and not args.validate_stage:
            # Setup can fail before a staging directory or provider exists.
            try:
                with lock(root / "local/refresh/job.lock", blocking=False):
                    latest = prior_receipt(root, "latest.json")
                    if latest is None or latest.started < started:
                        save_model(root / "local/refresh/latest.json", RefreshState(run_id=uuid.uuid4().hex,
                                   status="failed", started=started, completed=time.time(), since=0, mode="setup",
                                   reason="setup", sources=latest.sources if latest else {}))
            except (OSError, ValueError):
                pass
        print("Refresh could not complete. Check the registered project, refresh resources, source settings, and project status.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
