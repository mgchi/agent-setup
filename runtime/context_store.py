"""Bounded private context, sourced memory, and transactional local state."""

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from team_runtime import Provider, redact_text

Scope = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=48)]


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: Scope
    roots: list[Path] = Field(default_factory=list)
    common_dirs: list[Path] = Field(default_factory=list)
    advisers: bool = True

    @field_validator("slug")
    @classmethod
    def project_slug(cls, value: str) -> str:
        if value in {"personal", "local", "workspaces", "projects", "customers", "meetings"}:
            raise ValueError("This name is reserved for context storage.")
        return value

    @field_validator("roots", "common_dirs")
    @classmethod
    def project_paths(cls, values: list[Path]) -> list[Path]:
        result: list[Path] = []
        for value in values:
            path = value.expanduser()
            if not path.is_absolute() or linked_path(path):
                raise ValueError("Project paths must be absolute and cannot traverse symlinks.")
            path = path.resolve()
            if path == Path(path.anchor) or path == Path.home():
                raise ValueError("Register a project directory, not the whole computer or home.")
            if path not in result:
                result.append(path)
        return result


class RefreshSettings(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    project: Scope | None = None
    resource_root: Path | None = None
    github_repo: str = ""
    github_user: str = ""

    @field_validator("resource_root")
    @classmethod
    def canonical_root(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        path = value.expanduser()
        if not path.is_absolute() or linked_path(path):
            raise ValueError("Refresh resources must use an absolute directory without symlinks.")
        return path.resolve()


def default_providers() -> list[Provider]:
    return [
        Provider(name="claude", command=("claude",), model="claude-fable-5-1", effort="max"),
        Provider(name="codex", command=("codex",), model="gpt-6-astra", effort="max"),
    ]


class Settings(BaseModel):
    model_config = ConfigDict(extra="allow")
    context_root: Path = Path.home() / "context"
    orchestration_exclusions: list[Path] = Field(default_factory=list)
    providers: list[Provider] = Field(default_factory=default_providers)
    projects: list[Project] = Field(default_factory=list)
    refresh: RefreshSettings = Field(default_factory=RefreshSettings)
    packet_chars: int = Field(default=16000, ge=2000, le=24000)
    call_timeout: int = Field(default=480, ge=1, le=1200)
    idle_timeout: int = Field(default=180, ge=1, le=600)
    provider_wait: int = Field(default=20, ge=0, le=60)
    capture_days: int = Field(default=7, ge=1, le=30)
    run_days: int = Field(default=14, ge=1, le=60)
    max_runs: int = Field(default=32, ge=1, le=100)
    max_memory: int = Field(default=48, ge=1, le=100)
    max_capture: int = Field(default=80, ge=1, le=500)
    max_local_bytes: int = Field(default=268435456, ge=1048576)
    max_database_bytes: int = Field(default=8388608, ge=1048576)
    core_memory_keys: list[str] = Field(default_factory=list)

    @field_validator("context_root")
    @classmethod
    def private_root(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute() or linked_path(path):
            raise ValueError("Context root must be an absolute directory without symlinks.")
        if path == Path(path.anchor) or path == Path.home():
            raise ValueError("Choose a dedicated context directory.")
        return path.resolve()

    @model_validator(mode="after")
    def unique_projects(self) -> "Settings":
        slugs: set[str] = set()
        roots: set[Path] = set()
        common_dirs: set[Path] = set()
        for project in self.projects:
            if project.slug in slugs or roots.intersection(project.roots) or common_dirs.intersection(project.common_dirs):
                raise ValueError("Project names and repository registrations must be unambiguous.")
            slugs.add(project.slug)
            roots.update(project.roots)
            common_dirs.update(project.common_dirs)
        return self

    def project(self, scope: str) -> Project | None:
        return next((project for project in self.projects if project.slug == scope), None)


class Memory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,79}$")
    kind: Literal["preference", "decision", "lesson"]
    scope: Scope
    text: str = Field(min_length=5, max_length=1200)
    evidence: str = Field(min_length=5, max_length=2400)
    source: str = Field(min_length=1, max_length=600)
    run_id: str = Field(default="legacy", min_length=1, max_length=100)
    updated: float = Field(default_factory=time.time)
    expires: float = 0


class Packet(BaseModel):
    scope: Scope
    text: str
    sources: list[str]
    truncated: bool = False


class Capture(BaseModel):
    run_id: str
    prompt: str
    scope: Scope = "personal"
    final: str = ""
    created: float = Field(default_factory=time.time)
    preference_import: bool = False


class Cooldown(BaseModel):
    identity: str
    kind: str
    detail: str
    until: float


class Session(BaseModel):
    key: str
    run_id: str
    turn: str
    prompt_hash: str
    continued: bool = False
    packet_hash: str = ""
    updated: float = Field(default_factory=time.time)


class StoreStatus(BaseModel):
    memory_count: int
    capture_count: int
    pending_inbox: int
    cooldowns: list[Cooldown]
    context_root: str
    scope: str


class CleanupResult(BaseModel):
    removed_runs: int = 0
    retained_bytes: int = 0


class RunLease(BaseModel):
    status: str = "complete"
    deadline: float = 0


_EXTRA_SECRETS = re.compile(
    r"xox[abprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{25,}|"
    r"github_pat_[A-Za-z0-9_]{25,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)|"
    r"(?:postgres(?:ql)?|mysql|redis)://[^\s]+:[^\s@]+@[^\s]+",
    re.IGNORECASE,
)
_SENSITIVE_PATH = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]*)?|auth(?:\.json)?|credentials?[^/]*|secrets?[^/]*|"
    r"id_rsa|id_ed25519|[^/]*\.(?:pem|p12|pfx|key))(?:/|$)", re.IGNORECASE
)
_STOP_WORDS = set("the and that this with from what when where which have want need work about into then they them your will should could would does please context".split())
_RUN_NAME = re.compile(r"^(?:run-[a-f0-9]{24}|smoke-[0-9]+)$")


def clean(text: str, limit: int = 32000) -> str:
    return _EXTRA_SECRETS.sub("[REDACTED]", redact_text(text))[:limit]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sensitive_path(path: str) -> bool:
    return bool(_SENSITIVE_PATH.search(path))


def linked_path(path: Path) -> bool:
    aliases = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var"),
               Path("/etc"): Path("/private/etc")}
    return any(part.is_symlink() and aliases.get(part) != part.resolve()
               for part in (path, *path.parents))


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def atomic_text(path: Path, text: str) -> None:
    if linked_path(path):
        raise ValueError("Refusing to write through a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def save_model(path: Path, value: BaseModel) -> None:
    atomic_text(path, value.model_dump_json(indent=2) + "\n")


@contextlib.contextmanager
def lock(path: Path, *, blocking: bool = True, shared: bool = False) -> Iterator[None]:
    if linked_path(path):
        raise ValueError("Refusing to lock through a symlink.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, flags | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def read_text(path: Path, limit: int = 40000) -> str:
    if linked_path(path) or sensitive_path(str(path)):
        return ""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return clean(handle.read(limit + 1), limit)
    except (OSError, UnicodeError):
        return ""


def git(cwd: Path, arguments: list[str], timeout: int = 5) -> str:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *arguments], capture_output=True,
                                text=True, timeout=timeout, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def terms(query: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", query.lower()) if word not in _STOP_WORDS}


def direct_user_text(prompt: str) -> str:
    """Keep unquoted top-level prose for literal evidence checks."""
    result: list[str] = []
    fence = ""
    tags: list[str] = []
    malformed = False
    for raw in prompt.splitlines():
        line = raw.strip()
        match = re.match(r"^(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            result.append("")
            continue
        if fence:
            continue
        markup = list(re.finditer(r"<(\/?)([A-Za-z][\w-]*)\b[^>]*>", line))
        if markup:
            for match in markup:
                if match.group(1):
                    if tags and tags[-1] == match.group(2):
                        tags.pop()
                    else:
                        malformed = True
                elif not match.group(0).endswith("/>"):
                    tags.append(match.group(2))
            result.append("")
            continue
        if tags or malformed or raw.startswith(("    ", "\t")) or line.startswith((">", '"', "'")):
            result.append("")
            continue
        result.append(line)
    return "\n".join(result)


def preference_evidence(evidence: str) -> bool:
    return "?" not in evidence and bool(re.match(
        r"(?i)^(?:remember (?:my preference|that i prefer)|from now on[, :]|"
        r"my (?:default|preference) is|i prefer\b|always\b|never\b|"
        r"(?:do not|don't)\b.{0,160}\b(?:ever|by default|in future|going forward)\b)", evidence.strip()
    ))


def decision_evidence(evidence: str) -> bool:
    return "?" not in evidence and bool(re.match(
        r"(?i)^(?:we (?:have )?decided\b|i (?:have )?decided\b|"
        r"(?:decision|approved decision|confirmed decision):|"
        r"(?:yes[, :]\s*)?(?:i approve|i accept|let's go with|let us go with)\b)", evidence.strip()
    ))


def validate_memory_capture(memory: Memory, capture: Capture) -> None:
    if "[REDACTED]" in clean(memory.text + memory.evidence + memory.source, 6000):
        raise ValueError("Memory containing credentials cannot be retained.")
    if memory.run_id != capture.run_id:
        raise ValueError("Memory must match its captured run.")
    if memory.scope != "personal" and memory.scope != capture.scope:
        raise ValueError("A capture can create only personal memory or memory for its own project.")
    if memory.kind in {"preference", "decision"}:
        if memory.source != f"session:{memory.run_id}":
            raise ValueError("User statements must cite their captured session.")
        if memory.evidence not in direct_user_text(capture.prompt):
            raise ValueError("Evidence must be literal, unquoted text from the captured user prompt.")
        if memory.kind == "preference" and not (capture.preference_import or preference_evidence(memory.evidence)):
            raise ValueError("A preference requires an explicit user directive.")
        if memory.kind == "decision" and not decision_evidence(memory.evidence):
            raise ValueError("A decision requires explicit user acceptance of the stated choice.")
    else:
        source = Path(memory.source).expanduser()
        if not source.is_absolute() or sensitive_path(str(source)) or linked_path(source):
            raise ValueError("A lesson requires a readable evidence file.")
        if memory.evidence not in read_text(source, 80000):
            raise ValueError("A lesson's evidence must appear in its cited file.")


def persist_memories(connection: sqlite3.Connection, memories: list[Memory], max_memory: int) -> list[bool]:
    keys = {(item.scope, item.key) for item in memories}
    contents = {(item.scope, item.kind, item.text) for item in memories}
    if len(keys) != len(memories) or len(contents) != len(memories):
        raise ValueError("A memory batch must use distinct keys and contents.")
    now = time.time()
    changes: list[bool] = []
    connection.execute("DELETE FROM memory WHERE expires>0 AND expires<=?", (now,))
    for memory in memories:
        expires = memory.expires
        if memory.kind != "preference":
            expires = min(expires or now + 180 * 86400, now + 180 * 86400)
        memory = memory.model_copy(update={"updated": now, "expires": expires})
        changed = True
        rows = connection.execute("SELECT key, payload FROM memory WHERE scope=?", (memory.scope,)).fetchall()
        for key, payload in rows:
            old = Memory.model_validate_json(payload)
            if key == memory.key:
                changed = old.text != memory.text or old.kind != memory.kind
            elif old.text == memory.text and old.kind == memory.kind:
                connection.execute("DELETE FROM memory WHERE scope=? AND key=?", (memory.scope, key))
        connection.execute("INSERT OR REPLACE INTO memory VALUES (?, ?, ?, ?, ?)",
                           (memory.key, memory.scope, memory.updated, memory.expires, memory.model_dump_json()))
        changes.append(changed)
        if changed and memory.kind == "decision" and memory.scope != "personal":
            event = digest(memory.scope + memory.key + memory.text)
            connection.execute("INSERT OR IGNORE INTO outbox VALUES (?, ?)", (event, memory.model_dump_json()))
    excess = connection.execute("SELECT COUNT(*) FROM memory").fetchone()[0] - max_memory
    if excess > 0:
        candidates = connection.execute(
            "SELECT rowid, scope, key FROM memory WHERE json_extract(payload, '$.kind')!='preference' "
            "ORDER BY updated, rowid",
        ).fetchall()
        disposable = [(rowid,) for rowid, scope, key in candidates if (scope, key) not in keys][:excess]
        if len(disposable) < excess:
            raise ValueError("Memory capacity cannot hold these entries and standing preferences. Update or consolidate existing keys first.")
        connection.executemany("DELETE FROM memory WHERE rowid=?", disposable)
    return changes


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.context_root.expanduser().resolve()
        self.local = self.root / "local"
        if linked_path(self.local):
            raise ValueError("Private context must be a real directory.")
        self.local.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.local, 0o700)
        self.db = self.local / "state.sqlite3"
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS memory (key TEXT NOT NULL, scope TEXT NOT NULL,
                    updated REAL NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(scope, key));
                CREATE TABLE IF NOT EXISTS capture (run_id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cooldown (identity TEXT PRIMARY KEY, until REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session (key TEXT PRIMARY KEY, updated REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preference_import (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            """)
            columns = connection.execute("PRAGMA table_info(memory)").fetchall()
            if not any(row[1] == "scope" and row[5] for row in columns):
                connection.executescript("""
                    ALTER TABLE memory RENAME TO memory_old;
                    CREATE TABLE memory (key TEXT NOT NULL, scope TEXT NOT NULL, updated REAL NOT NULL,
                        expires REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(scope, key));
                    INSERT INTO memory SELECT * FROM memory_old;
                    DROP TABLE memory_old;
                """)
        os.chmod(self.db, 0o600)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if any(linked_path(Path(str(self.db) + suffix)) for suffix in ("", "-journal", "-wal", "-shm")):
            raise ValueError("Context database paths cannot be symlinks.")
        connection = sqlite3.connect(self.db, timeout=5)
        try:
            connection.execute("PRAGMA secure_delete=ON")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            connection.execute(f"PRAGMA max_page_count={self.settings.max_database_bytes // page_size}")
            with connection:
                yield connection
        finally:
            connection.close()

    def scope(self, cwd: Path, query: str = "") -> Scope:
        directive = direct_user_text(query)
        if re.search(r"(?im)^context:\s*personal\s*$", directive):
            return "personal"
        common = git(cwd, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
        if common:
            for project in self.settings.projects:
                if Path(common).resolve() in project.common_dirs:
                    return project.slug
        matches = [(len(root.parts), project.slug) for project in self.settings.projects
                   for root in project.roots if cwd.resolve().is_relative_to(root)
                   and git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]) == common]
        if matches:
            return max(matches)[1]
        if common:
            return "personal"
        for project in self.settings.projects:
            if cwd.resolve().is_relative_to(self.root / "workspaces" / project.slug):
                return project.slug
        return "personal"

    def captured(self, run_id: str) -> Capture | None:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM capture WHERE run_id=? AND created>?",
                                     (run_id, time.time() - self.settings.capture_days * 86400)).fetchone()
        return Capture.model_validate_json(row[0]) if row else None

    def validate_memory(self, memory: Memory) -> None:
        capture = self.captured(memory.run_id)
        if not capture:
            raise ValueError("Memory requires a recent captured user prompt for this run.")
        validate_memory_capture(memory, capture)

    def remember(self, memory: Memory) -> bool:
        return self.remember_many([memory])[0]

    def remember_many(self, memories: list[Memory]) -> list[bool]:
        for memory in memories:
            self.validate_memory(memory)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changes = persist_memories(connection, memories, self.settings.max_memory)
        self.flush_outbox()
        return changes

    def flush_outbox(self) -> None:
        with self.connection() as connection:
            rows = connection.execute("SELECT id, payload FROM outbox").fetchall()
        for event, payload in rows:
            memory = Memory.model_validate_json(payload)
            project_root = self.root / "workspaces" / memory.scope
            with lock(project_root / "local/publish.lock"):
                self.publish_memory(event, memory, project_root)

    def publish_memory(self, event: str, memory: Memory, project_root: Path) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT id FROM outbox WHERE id=?", (event,)).fetchone() is None:
                return
            path = project_root / "inbox.md"
            if linked_path(path) or (path.exists() and path.stat().st_size > 200000):
                raise ValueError("Inbox needs maintenance before publication.")
            previous = path.read_text() if path.exists() else ""
            marker = f"<!-- agent-memory:{event} -->"
            if marker not in previous:
                line = f"- {time.strftime('%Y-%m-%d')} · {' '.join(memory.text.split())} · {memory.scope} · {memory.source} {marker}\n"
                atomic_text(path, previous + ("\n" if previous and not previous.endswith("\n") else "") + line)
            connection.execute("DELETE FROM outbox WHERE id=?", (event,))

    def memories(self, scope: str) -> list[Memory]:
        with self.connection() as connection:
            rows = connection.execute("SELECT payload FROM memory WHERE (expires=0 OR expires>?) AND (scope=? OR scope='personal') ORDER BY updated DESC LIMIT ?",
                                     (time.time(), scope, self.settings.max_memory)).fetchall()
        return [Memory.model_validate_json(row[0]) for row in rows]

    def disclosure_memories(self) -> list[Memory]:
        with self.connection() as connection:
            rows = connection.execute("SELECT payload FROM memory").fetchall()
        return [Memory.model_validate_json(row[0]) for row in rows]

    def capture(self, capture: Capture) -> None:
        value = capture.model_copy(update={"prompt": clean(capture.prompt, 24000), "final": clean(capture.final, 8000)})
        with self.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO capture VALUES (?, ?, ?)", (value.run_id, value.created, value.model_dump_json()))
            connection.execute("DELETE FROM capture WHERE run_id IN (SELECT run_id FROM capture ORDER BY created DESC LIMIT -1 OFFSET ?)", (self.settings.max_capture,))

    def preference_candidates(self, run_id: str) -> list[str]:
        capture = self.captured(run_id)
        if not capture:
            return []
        return [line for line in direct_user_text(capture.prompt).splitlines()
                if 5 <= len(line) <= 500 and preference_evidence(line)][:10]

    def decision_candidates(self, run_id: str) -> list[str]:
        capture = self.captured(run_id)
        if not capture:
            return []
        return [line for line in direct_user_text(capture.prompt).splitlines()
                if 5 <= len(line) <= 500 and decision_evidence(line)][:10]

    def set_session(self, session: Session) -> None:
        with self.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO session VALUES (?, ?, ?)", (session.key, session.updated, session.model_dump_json()))
            connection.execute("DELETE FROM session WHERE key IN (SELECT key FROM session ORDER BY updated DESC LIMIT -1 OFFSET 512)")

    def session(self, key: str) -> Session | None:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM session WHERE key=?", (key,)).fetchone()
        return Session.model_validate_json(row[0]) if row else None

    def set_cooldown(self, cooldown: Cooldown) -> None:
        with self.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO cooldown VALUES (?, ?, ?)", (cooldown.identity, cooldown.until, cooldown.model_dump_json()))
            connection.execute("DELETE FROM cooldown WHERE identity IN (SELECT identity FROM cooldown ORDER BY until DESC LIMIT -1 OFFSET 128)")

    def cooldown(self, identity: str) -> Cooldown | None:
        with self.connection() as connection:
            row = connection.execute("SELECT payload FROM cooldown WHERE identity=? AND until>?", (identity, time.time())).fetchone()
        return Cooldown.model_validate_json(row[0]) if row else None

    def reset_cooldown(self, provider: str) -> None:
        if provider not in {"claude", "codex", "all"}:
            raise ValueError("Expected claude, codex, or all.")
        with self.connection() as connection:
            connection.execute("DELETE FROM cooldown" if provider == "all" else "DELETE FROM cooldown WHERE identity LIKE ?",
                               () if provider == "all" else (provider + ":%",))

    def forget(self, scope: Scope, key: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM memory WHERE scope=? AND key=?", (scope, key))
            connection.execute("DELETE FROM outbox WHERE json_extract(payload, '$.scope')=? AND json_extract(payload, '$.key')=?", (scope, key))

    def prune(self) -> CleanupResult:
        now = time.time()
        with self.connection() as connection:
            connection.execute("DELETE FROM memory WHERE expires>0 AND expires<=?", (now,))
            connection.execute("DELETE FROM capture WHERE created<?", (now - self.settings.capture_days * 86400,))
            connection.execute("DELETE FROM session WHERE updated<?", (now - self.settings.run_days * 86400,))
            connection.execute("DELETE FROM cooldown WHERE until<=?", (now,))
        with sqlite3.connect(self.db, timeout=5) as connection:
            connection.execute("VACUUM")
        return self.prune_runs()

    def prune_runs(self) -> CleanupResult:
        result = CleanupResult()
        root = self.local / "runs"
        if linked_path(root):
            raise ValueError("Run storage cannot be a symlink.")
        if not root.exists():
            return result
        threshold = time.time() - self.settings.run_days * 86400
        with lock(self.local / "cleanup.lock"):
            directories = [path for path in root.iterdir() if not path.is_symlink() and path.is_dir() and _RUN_NAME.fullmatch(path.name)]
            sizes: dict[Path, int] = {}
            for path in directories:
                sizes[path] = sum(file_size(item) for item in path.rglob("*") if item.is_file() and not linked_path(item))
            total = sum(sizes.values())
            remaining = len(directories)
            for path in sorted(directories, key=modified):
                expired = modified(path) < threshold
                if not expired and remaining <= self.settings.max_runs and total <= self.settings.max_local_bytes:
                    continue
                try:
                    with lock(path / "active.lock", blocking=False), lock(path / "assessment.lock", blocking=False), lock(path / "review.lock", blocking=False):
                        if modified(path / "pending") > time.time() - 1800:
                            continue
                        leases = [path / "assessment.json", *path.glob("review-*.json")]
                        busy = False
                        for lease in leases:
                            body = read_text(lease, 50000)
                            if body:
                                with contextlib.suppress(ValueError):
                                    state = RunLease.model_validate_json(body)
                                    busy = busy or (state.status == "pending" and state.deadline > time.time())
                        if busy:
                            continue
                        shutil.rmtree(path)
                        result.removed_runs += 1
                        remaining -= 1
                        total -= sizes[path]
                except (BlockingIOError, FileNotFoundError):
                    continue
        result.retained_bytes = total
        return result

    def status(self, cwd: Path) -> StoreStatus:
        with self.connection() as connection:
            memory_count = connection.execute("SELECT COUNT(*) FROM memory WHERE expires=0 OR expires>?", (time.time(),)).fetchone()[0]
            capture_count = connection.execute("SELECT COUNT(*) FROM capture").fetchone()[0]
            pending = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            rows = connection.execute("SELECT payload FROM cooldown WHERE until>?", (time.time(),)).fetchall()
        return StoreStatus(memory_count=memory_count, capture_count=capture_count, pending_inbox=pending,
                           cooldowns=[Cooldown.model_validate_json(row[0]) for row in rows], context_root=str(self.root), scope=self.scope(cwd))

    def load(self, cwd: Path, query: str = "") -> Packet:
        scope = self.scope(cwd, query)
        root = self.root if scope == "personal" else self.root / "workspaces" / scope
        with lock(root / "local/publish.lock", shared=True):
            return self._load(scope, query)

    def _load(self, scope: Scope, query: str) -> Packet:
        sections: list[str] = []
        sources: list[str] = []
        remaining_packet = self.settings.packet_chars
        truncated = False
        query_terms = terms(query)
        ranked = sorted(self.memories(scope), key=lambda item: (len(terms(item.text + " " + item.key) & query_terms),
                        item.key in self.settings.core_memory_keys, item.updated), reverse=True)
        memory_lines: list[str] = []
        heading = "Saved context (current instructions take precedence):\n"
        remaining = min(3000, remaining_packet) - len(heading)
        for item in ranked:
            line = f"- [{item.key}; {item.source}] {item.text}"
            if len(line) + 1 > remaining:
                truncated = True
                continue
            memory_lines.append(line)
            remaining -= len(line) + 1
            if len(memory_lines) == 12:
                break
        if memory_lines:
            sections.append(heading + "\n".join(memory_lines))
            remaining_packet -= len(sections[-1]) + 2
        background: list[tuple[Path, str]] = []
        path = self.local / "personal.md"
        body = read_text(path, 5000)
        if body:
            background.append((path, body))
        project_root = self.root / "workspaces" / scope
        complete_digest = not (project_root / "local/refresh/pending.json").exists()
        if scope != "personal":
            names = ["context.md", "playbook.md"]
            if complete_digest:
                names.extend(["brief.md", "decisions.md", "inbox.md"])
            else:
                warning = "Project digest publication was interrupted. Verify live sources until refresh recovery completes."
                sections.append(warning)
                remaining_packet -= len(warning) + 2
            for name in names:
                path = project_root / name
                body = read_text(path, 4200 if name == "brief.md" else 2200)
                if body:
                    background.append((path, body))
        if scope != "personal" and query_terms and complete_digest:
            candidates: list[tuple[int, Path, str]] = []
            paths = [project_root / "people.md", project_root / "decisions.md"]
            for directory in ("projects", "customers", "meetings"):
                paths.extend(sorted((project_root / directory).glob("*.md"), reverse=True)[:300])
            for path in paths:
                body = read_text(path, 24000)
                score = len(terms(body) & query_terms) + 3 * len(terms(path.stem) & query_terms)
                if score >= 2:
                    candidates.append((score, path, body))
            for _, path, body in sorted(candidates, key=lambda value: (value[0], value[1].name), reverse=True)[:2]:
                lines = body.splitlines()
                selected = set(range(min(3, len(lines))))
                for index, line in enumerate(lines):
                    if terms(line) & query_terms:
                        selected.update(range(max(0, index - 1), min(len(lines), index + 2)))
                excerpt = "\n".join(f"{index + 1}: {lines[index]}" for index in sorted(selected))[:1800]
                background.append((path, excerpt))
        for path, body in background:
            label = f"Source: {path} (file modified {time.strftime('%Y-%m-%d', time.localtime(modified(path)))})\n"
            available = remaining_packet - len(label)
            if available < 80:
                truncated = True
                continue
            if len(body) > available:
                truncated = True
                marker = "\n[Source excerpt shortened; read the file for more.]"
                body = body[:max(0, available - len(marker))].rsplit("\n", 1)[0] + marker
            sections.append(label + body)
            sources.append(str(path))
            remaining_packet -= len(sections[-1]) + 2
        combined = "\n\n".join(sections)
        return Packet(scope=scope, text=combined, sources=sources, truncated=truncated)


def load_settings(path: Path) -> Settings:
    return Settings.model_validate_json(path.read_text())
