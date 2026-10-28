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

from context_store import atomic_text, clean, digest, linked_path, load_settings, lock, save_model

ROOT_FILES = ("brief.md", "people.md", "decisions.md", "inbox.md", "sources.md")
FOLDERS = ("projects", "customers", "meetings")
SOURCE_NAMES = {"slack", "linear", "notion", "granola", "github"}
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
    source: Literal["slack", "linear", "notion", "granola", "github"]
    status: Literal["ok", "empty", "partial", "unavailable", "error"]
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


class RefreshState(BaseModel):
    run_id: str
    status: Literal["succeeded", "failed"]
    completed: float
    started: float = 0
    since: float
    mode: str
    detail: str = ""


class Permissions(BaseModel):
    allow: list[str]
    deny: list[str]
    defaultMode: str = "dontAsk"


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
            atomic_text(path, contents[name])
        else:
            path.unlink(missing_ok=True)


def validate_stage(stage: Path, before: Snapshot, run_id: str, context_root: Path | None = None) -> Snapshot:
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
    if manifest.run_id != run_id or len(manifest.sources) != 5 or {item.source for item in manifest.sources} != SOURCE_NAMES:
        raise ValueError("Source manifest must cover this run and all five sources.")
    failed = [item.source for item in manifest.sources if item.status not in {"ok", "empty"}]
    if failed:
        raise ValueError("Keeping the last valid digest; incomplete sources: " + ", ".join(failed))
    value = snapshot(stage)
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
    return retain_meetings(value, context_root=context_root)


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


def retain_meetings(value: Snapshot, today: dt.date | None = None, *, context_root: Path | None = None) -> Snapshot:
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=MEETING_DAYS)).isoformat()
    references = {item.path: local_references(item, context_root) for item in value.documents}
    meetings = {item.path for item in value.documents if item.path.startswith("meetings/")}
    keep = reference_closure(references.keys() - meetings, references)
    if len(keep & meetings) > MEETING_COUNT:
        raise ValueError("Consolidate old meeting references before exceeding 160 meetings.")
    for name in sorted(meetings, reverse=True):
        if Path(name).name[:10] < cutoff:
            continue
        candidate = keep | reference_closure({name}, references)
        if len(candidate & meetings) <= MEETING_COUNT:
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


def publish(root: Path, before: Snapshot, staged: Snapshot, run_id: str) -> None:
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
        transaction = Transaction(run_id=run_id, before=current, after=after)
        pending = root / "local/refresh/pending.json"
        save_model(root / "local/refresh/last-valid.json", current)
        save_model(pending, transaction)
        try:
            apply_snapshot(root, after, names)
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


def github_snapshot(since: str, repo: str, user: str) -> GithubSnapshot:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[A-Za-z0-9-]+", user):
        raise ValueError("Configure a GitHub repository and user before enabling refresh.")
    queries = [
        ["pr", "list", "-R", repo, "--author", user, "--state", "open", "--limit", "50", "--json", "number,title,url,isDraft,reviewDecision,updatedAt"],
        ["pr", "list", "-R", repo, "--search", f"review-requested:{user}", "--state", "open", "--limit", "50", "--json", "number,title,url,author,updatedAt"],
        ["search", "prs", "--repo", repo, "--involves", user, "--updated", f">={since}", "--limit", "50", "--json", "number,title,url,author,state,updatedAt"],
        ["pr", "list", "-R", repo, "--state", "merged", "--search", f"merged:>={since}", "--limit", "300", "--json", "number,title,url,author,mergedAt"],
        ["pr", "list", "-R", repo, "--state", "open", "--limit", "100", "--json", "number,title,url,author,isDraft,updatedAt"],
    ]
    values: list[list[JsonValue]] = []
    for query in queries:
        value = json.loads(command(["gh", *query]).stdout)
        if not isinstance(value, list):
            raise ValueError("GitHub returned an invalid snapshot.")
        if len(value) >= int(query[query.index("--limit") + 1]):
            raise ValueError("GitHub snapshot reached its result cap; narrow or paginate the source before publishing.")
        values.append(value)
    return GithubSnapshot(mine=values[0], review_requested=values[1], involving_user=values[2], merged_in_window=values[3], other_open=values[4])


def agents(tools_root: Path, permissions: Permissions) -> str:
    common = (tools_root / "agents/common.md").read_text()
    specs: dict[str, AgentSpec] = {}
    for name, server in (("slack", "slack"), ("linear", "linear-server"), ("notion", "notion"), ("granola", "granola")):
        specs[f"{name}-gatherer"] = AgentSpec(
            description=f"Read {name} for dated, linked facts and report source coverage.",
            prompt=common + "\n" + (tools_root / f"agents/{name}.md").read_text(),
            tools=[tool for tool in permissions.allow if tool.startswith(f"mcp__{server}__")],
        )
    return json.dumps({name: item.model_dump() for name, item in specs.items()})


def settings_for_stage(source: PermissionSettings, stage: Path) -> PermissionSettings:
    permissions = source.permissions.model_copy(deep=True)
    permissions.allow = [rule for rule in permissions.allow if rule.startswith("mcp__")]
    permissions.allow.extend([f"Read(/{stage}/**)", f"Write(/{stage}/**)", f"Edit(/{stage}/**)", "Agent", "TodoWrite"])
    return PermissionSettings(permissions=permissions)


def run_agent(stage: Path, tools_root: Path, prompt: str, budget: int, timeout: int,
              stdout_path: Path, stderr_path: Path) -> AgentResult:
    original = PermissionSettings.model_validate_json((tools_root / "settings.json").read_text())
    configured = MCPConfig.model_validate_json((Path.home() / ".claude.json").read_text())
    selected = MCPConfig(mcpServers={key: value for key, value in configured.mcpServers.items() if key in {"slack", "linear-server", "notion", "granola"}})
    if len(selected.mcpServers) != 4:
        raise ValueError("All four configured read-only MCP sources are required.")
    with tempfile.TemporaryDirectory(prefix="config-", dir=tools_root) as configuration:
        config_root = Path(configuration)
        save_model(config_root / "settings.json", settings_for_stage(original, stage))
        save_model(config_root / "mcp.json", selected)
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
                "--tools", "Read,Write,Edit,Glob,Grep,Agent,TodoWrite", "--settings", str(config_root / "settings.json"), "--agents", agents(tools_root, original.permissions),
                "--permission-mode", "dontAsk", "--model", "sonnet", "--max-budget-usd", str(budget), "--output-format", "json"]
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


def refresh(root: Path, tools_root: Path, bootstrap: bool, *, github_repo: str = "", github_user: str = "") -> None:
    logs = tools_root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock(tools_root / "refresh.lock", blocking=False):
        started = time.time()
        prune_logs(tools_root)
        with lock(root / "local/publish.lock"):
            recover(root)
            before = snapshot(root)
        last_file = root / "local/refresh/success.json"
        last = 0.0
        if last_file.exists():
            previous = RefreshState.model_validate_json(last_file.read_text())
            last = previous.started or previous.since or max(0, previous.completed - 5400)
        if not last and (root / ".git").exists():
            value = command(["git", "-C", str(root), "log", "-1", "--format=%ct", "--grep=^refresh:"], check=False).stdout.strip()
            last = max(0, float(value) - 5400) if value.isdigit() else 0
        mode = "bootstrap" if bootstrap or not last else "incremental"
        since = started - 14 * 86400 if mode == "bootstrap" else last - 3600
        budget, timeout = (40, 5400) if mode == "bootstrap" else (10, 2400)
        run_id = time.strftime("%Y-%m-%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        try:
            staging = tools_root / "staging"
            staging.mkdir(exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix="staging-", dir=staging) as temporary:
                stage = Path(temporary)
                apply_snapshot(stage, before, set())
                save_model(stage / ".cache/github.json", github_snapshot(dt.datetime.fromtimestamp(since).strftime("%Y-%m-%d"), github_repo, github_user))
                prompt = (f"Now: {dt.datetime.now().astimezone().isoformat()}\nWindow start: {dt.datetime.fromtimestamp(since).astimezone().isoformat()} (Unix {int(since)})\n"
                          f"Mode: {mode}\nRun ID: {run_id}\nStaging directory: {stage}\n\n" + (tools_root / "prompt.md").read_text())
                result = run_agent(stage, tools_root, prompt, budget, timeout, logs / f"{run_id}.json", logs / f"{run_id}.err")
                after = validate_stage(stage, before, run_id, root)
                publish(root, before, after, run_id)
            state = RefreshState(run_id=run_id, status="succeeded", started=started, completed=time.time(), since=since, mode=mode, detail=f"cost=${result.total_cost_usd:.2f}")
            save_model(last_file, state)
            save_model(root / "local/refresh/latest.json", state)
            print(f"Context refresh succeeded: {run_id}; {state.detail}")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            state = RefreshState(run_id=run_id, status="failed", started=started, completed=time.time(), since=since, mode=mode, detail=type(error).__name__)
            save_model(root / "local/refresh/latest.json", state)
            recovery = "publication recovery pending; saved snapshot retained" if (root / "local/refresh/pending.json").exists() else "last valid context retained"
            print(f"Context refresh failed; {recovery}. {state.detail}")
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--validate-stage", type=Path)
    parser.add_argument("--run-id", default="validation")
    parser.add_argument("--settings", type=Path, default=Path.home() / ".config/agent-context/settings.json")
    args = parser.parse_args()
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
            validate_stage(args.validate_stage, snapshot(root), args.run_id, root)
        else:
            tools_root = settings.refresh.resource_root
            if tools_root is None or not tools_root.is_dir() or linked_path(tools_root):
                raise ValueError("Configure a real refresh resource directory before enabling refresh.")
            refresh(root, tools_root, args.bootstrap, github_repo=settings.refresh.github_repo, github_user=settings.refresh.github_user)
    except BlockingIOError:
        print("Context refresh skipped: another refresh is active.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Context refresh failed ({type(error).__name__}).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
