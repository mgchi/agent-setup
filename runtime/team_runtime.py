"""Bounded tool-free provider calls; the lead alone applies changes."""

import contextlib
import fcntl
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict, RootModel

from disclosure import DisclosureError, DisclosureIndex, RawScanner


class Provider(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    command: tuple[str, ...]
    model: str
    effort: str


class Outcome(BaseModel):
    ok: bool
    text: str = ""
    kind: str = ""
    detail: str = ""
    session_id: str = ""
    model: str = ""
    elapsed: float = 0.0
    effort: str = ""


_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "code_mode_host",
    "code_mode",
    "multi_agent",
    "multi_agent_v2",
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "in_app_browser",
    "in_app_local_automation",
    "image_generation",
    "view_image",
    "goals",
    "sleep_tool",
    "skill_search",
    "skill_mcp_dependency_install",
    "memories",
    "tool_suggest",
    "daemon_auto_start",
)
_WORK_PARENT = Path("/tmp")
_WORK_PREFIX = "agent-context-provider-"
_WORK_GRACE_SECONDS = 60.0
_MAX_LINE_BYTES = 8 * 1024 * 1024
_MAX_STREAM_BYTES = 12 * 1024 * 1024
_ERROR_TAIL = 16 * 1024
_SECRET_NAME = re.compile(
    r"(?:key|token|password|secret|credential)(?:$|_)", re.IGNORECASE
)
_TOKEN = re.compile(
    r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*",
    re.IGNORECASE,
)


def _secret_parts(values: tuple[str, ...]) -> tuple[str, ...]:
    # A multiline credential may be printed one line/event at a time. Treat its
    # nonblank lines as sensitive too, before either stream parsing or truncation.
    return tuple(
        sorted(
            {
                part
                for value in values
                for part in (value, *value.splitlines())
                if part.strip()
            },
            key=len,
            reverse=True,
        )
    )


def secret_values(environment: dict[str, str] | None = None) -> tuple[str, ...]:
    """Known environment secrets; not a detector for arbitrary private data."""
    return _secret_parts(
        tuple(
            value
            for key, value in (
                os.environ if environment is None else environment
            ).items()
            if len(value) >= 8 and _SECRET_NAME.search(key)
        )
    )


def redact_text(text: str, secrets: tuple[str, ...] | None = None) -> str:
    """Apply the same bounded-secret policy to provider and check output."""
    for secret in secret_values() if secrets is None else _secret_parts(secrets):
        text = text.replace(secret, "[REDACTED]")
        text = text.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    return _TOKEN.sub("[REDACTED]", text)


def _arguments(provider: Provider, schema: str, directory: Path) -> list[str]:
    if provider.name == "claude":
        return [
            *provider.command,
            "-p",
            "--no-session-persistence",
            "--model",
            provider.model,
            "--effort",
            provider.effort,
            "--safe-mode",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            str(directory / "empty-mcp.json"),
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            schema,
        ]
    arguments = [
        *provider.command,
        "exec",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "--ephemeral",
        "--model",
        provider.model,
        "--output-schema",
        str(directory / "output-schema.json"),
        "-c",
        f"model_reasoning_effort={json.dumps(provider.effort)}",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        "mcp_servers={}",
        "-c",
        "project_doc_max_bytes=0",
    ]
    for feature in _DISABLED_FEATURES:
        arguments.extend(("--disable", feature))
    return [*arguments, "-"]


def _classify(error: str) -> str:
    """Only called with an actual error event or a failed process's stderr."""
    text = error.lower().replace('_', ' ')
    if 'disclosure blocked' in text:
        return 'disclosure'
    if re.search(r"data retention|organization.{0,40}(?:policy|must|disabled)|workspace.{0,40}(?:policy|must|disabled)", text):
        return "account-policy"
    if re.search(
        r"quota|credit.{0,30}(?:exhaust|balance|limit)|usage.(?:limit|cap)|billing",
        text,
    ):
        return "quota"
    if re.search(
        r"\b40[13]\b|unauth|authentication|invalid.{0,20}api.?key|not logged in|(?:log|sign).in.required|please (?:log|sign) in|credential",
        text,
    ):
        return "auth"
    if re.search(
        r"model.{0,50}(?:not.found|not.available|not.supported|does not exist)|(?:unknown|unsupported).{0,20}model|\b404\b|not found|no such file|command not found|(?:unknown|unexpected|unrecognized).{0,20}(?:argument|option)|unsupported.{0,20}(?:effort|reasoning)",
        text,
    ):
        return "unavailable"
    if re.search(
        r"\b(?:429|500|502|503|504|529)\b|rate.limit|too many requests|overloaded|service unavailable|server.error|internal.error|connection|network|econn|etimedout|broken pipe|temporar|socket|timed out|timeout",
        text,
    ):
        return "transient"
    return "exit"


class JsonObject(RootModel[dict[str, object]]):
    def get(self, key: str, default: object = None) -> object:
        return self.root.get(key, default)

    def __getitem__(self, key: str) -> object:
        return self.root[key]

    def __contains__(self, key: str) -> bool:
        return key in self.root


def _encode_model(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump()
    raise TypeError("unsupported JSON value")


def _unique_object(pairs: list[tuple[str, object]]) -> JsonObject:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return JsonObject(root=result)


def _invalid_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _object(text: str) -> JsonObject:
    value = json.loads(
        text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
    )
    if not isinstance(value, JsonObject):
        raise TypeError("expected a JSON object")
    json.dumps(value, default=_encode_model, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


class _Events:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.session_id = ""
        self.model = ""
        self.effort = ""
        self.candidate: object = None
        self.terminal = False
        self.error = ""
        self.invalid = ""
        self.unsafe = False

    def accept(self, line: str) -> None:
        if not line.strip():
            return
        try:
            event = _object(line)
        except (ValueError, TypeError, RecursionError):
            self.invalid = "Malformed provider event stream."
            return
        kind = event.get("type")
        if not isinstance(kind, str):
            self.invalid = "Provider event has no type."
            return
        if self.provider == "claude":
            self._claude(kind, event)
        else:
            self._codex(kind, event)

    def _metadata(self, event: JsonObject) -> None:
        session = event.get("session_id") or event.get("thread_id")
        if isinstance(session, str) and session:
            self.session_id = session
        model = event.get("model")
        if isinstance(model, str) and model:
            self.model = model
        effort = event.get("reasoning_effort") or event.get("effort")
        if isinstance(effort, str) and effort:
            self.effort = effort

    def _claude(self, kind: str, event: JsonObject) -> None:
        self._metadata(event)
        if kind == "system" and event.get("subtype") == "init":
            tools = event.get("tools", [])
            if isinstance(tools, list) and any(
                tool != "StructuredOutput" for tool in tools
            ):
                self.unsafe = True
        if kind == "assistant":
            if self.terminal:
                self.invalid = "An answer followed terminal completion."
            message = event.get("message")
            if isinstance(message, JsonObject):
                self._metadata(message)
                content = message.get("content", [])
                if isinstance(content, list) and any(
                    isinstance(block, JsonObject)
                    and block.get("type") == "tool_use"
                    and block.get("name") != "StructuredOutput"
                    for block in content
                ):
                    self.unsafe = True
            if event.get("error"):
                self.error = str(event["error"])
        if kind == "result":
            if self.terminal:
                self.invalid = "Multiple terminal provider events."
            self.terminal = True
            if event.get("subtype") == "success" and event.get("is_error") is False:
                self.candidate = event.get("structured_output", event.get("result"))
            else:
                self.error = json.dumps(
                    {
                        key: event[key]
                        for key in ("subtype", "errors", "error", "result")
                        if key in event
                    },
                    ensure_ascii=False, default=_encode_model,
                )
        elif kind == "error":
            self.error = json.dumps(event, ensure_ascii=False, default=_encode_model)

    def _codex(self, kind: str, event: JsonObject) -> None:
        if kind in {"thread.started", "turn.started"}:
            self._metadata(event)
            if self.terminal:
                self.invalid = "A new turn followed terminal completion."
        if kind.startswith("item."):
            item = event.get("item")
            if isinstance(item, JsonObject):
                item_kind = item.get("type")
                if item_kind in {
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "web_search",
                    "collab_tool_call",
                    "image_generation",
                    "tool_call",
                }:
                    self.unsafe = True
                if kind == "item.completed" and item_kind == "agent_message":
                    if self.terminal:
                        self.invalid = "An answer followed terminal completion."
                    elif item.get("phase") != "commentary":
                        self.candidate = item.get("text")
        if kind in {"turn.completed", "turn.failed"}:
            if self.terminal:
                self.invalid = "Multiple terminal provider events."
            self.terminal = True
            if kind == "turn.failed":
                self.error = json.dumps(event.get("error", event), ensure_ascii=False, default=_encode_model)
        elif kind == "error":
            self.error = json.dumps(event, ensure_ascii=False, default=_encode_model)

    def outcome(self, returncode: int, stderr: str) -> Outcome:
        if self.unsafe:
            return Outcome(
                ok=False,
                kind="invalid_output",
                detail="Provider exposed or used tools in a packet-only call.",
            )
        if self.error or returncode:
            error = self.error or stderr or f"Provider exited with status {returncode}."
            kind = _classify(error)
            return Outcome(ok=False, kind=kind, detail=f"Provider failed ({kind}).")
        if self.invalid or not self.terminal:
            return Outcome(
                ok=False,
                kind="invalid_output",
                detail=self.invalid or "No terminal success event.",
            )
        try:
            value = (
                _object(self.candidate)
                if isinstance(self.candidate, str)
                else self.candidate
            )
            if not isinstance(value, JsonObject):
                raise TypeError("no completed JSON object")
            text = json.dumps(value, default=_encode_model, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            return Outcome(
                ok=False,
                kind="invalid_output",
                detail="Terminal response is not a valid JSON object.",
            )
        return Outcome(ok=True, text=text)


class _Stream:
    def __init__(
        self, target: BinaryIO, secrets: tuple[str, ...], events: _Events | None = None,
        scanner: RawScanner | None = None,
    ) -> None:
        self.target = target
        self.secrets = secrets
        self.events = events
        self.scanner = scanner
        self.pending = bytearray()
        self.discarding = False
        self.tail = ""
        self.total_bytes = 0
        self.exceeded = False

    def _line(self, raw: bytes) -> None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            if self.events:
                self.events.invalid = "Provider stream contains invalid UTF-8."
        text = redact_text(text, self.secrets)
        self.target.write(text.encode("utf-8"))
        self.target.flush()
        if self.events:
            self.events.accept(text)
        else:
            self.tail = (self.tail + text)[-_ERROR_TAIL:]

    def feed(self, data: bytes, *, final: bool = False) -> None:
        if self.scanner:
            self.scanner.feed(data, final=final)
        self.total_bytes += len(data)
        if self.total_bytes > _MAX_STREAM_BYTES:
            self.exceeded = True
            self.pending.clear()
            if self.events:
                self.events.invalid = "Provider stream exceeded its total size limit."
            return
        for fragment in data.splitlines(keepends=True):
            if not self.discarding:
                self.pending.extend(fragment)
                if len(self.pending) > _MAX_LINE_BYTES:
                    self.pending.clear()
                    self.discarding = True
                    self.target.write(b"[Provider output line exceeded limit.]\n")
                    self.target.flush()
                    if self.events:
                        self.events.invalid = "Provider event exceeds the 8 MiB limit."
            if fragment.endswith(b"\n"):
                if not self.discarding:
                    self._line(bytes(self.pending))
                self.pending.clear()
                self.discarding = False
        if final and self.pending:
            self._line(bytes(self.pending))
            self.pending.clear()


def redacted_tail(source: BinaryIO, limit: int = 16000) -> str:
    """Sanitize before truncating, so a tail boundary cannot split a secret."""
    with tempfile.TemporaryFile() as sanitized:
        stream = _Stream(sanitized, secret_values())
        source.seek(0)
        while chunk := source.read(65536):
            stream.feed(chunk)
        stream.feed(b"", final=True)
        sanitized.seek(max(0, sanitized.tell() - limit))
        return sanitized.read().decode(errors="replace")


def _exited(process: subprocess.Popen[bytes]) -> bool:
    # WNOWAIT holds the PID until group cleanup; poll()/wait() would release it.
    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    return status is not None and status.si_pid == process.pid


def terminate_process_group(process: subprocess.Popen[bytes]) -> int:
    """Kill the owned unreaped process group, then reap its leader."""
    try:
        with contextlib.suppress(ProcessLookupError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except PermissionError:
                # Darwin reports EPERM for a group containing only a zombie.
                if sys.platform != "darwin" or not _exited(process):
                    raise
    finally:
        returncode = process.wait(timeout=2)
    return returncode


def _sweep_orphaned_work_directories() -> None:
    """Sweep only old directories whose ownership lock is available."""
    with contextlib.suppress(OSError):
        for candidate in _WORK_PARENT.iterdir():
            if not candidate.name.startswith(_WORK_PREFIX):
                continue
            with contextlib.suppress(OSError):
                if time.time() - candidate.stat().st_mtime < _WORK_GRACE_SECONDS:
                    continue
                handle = os.open(candidate, os.O_RDONLY)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    shutil.rmtree(candidate)
                finally:
                    os.close(handle)


@contextlib.contextmanager
def _work_directory() -> Iterator[str]:
    """An empty directory the CLI runs in, locked for the call and then removed."""
    _sweep_orphaned_work_directories()
    with tempfile.TemporaryDirectory(prefix=_WORK_PREFIX, dir=_WORK_PARENT) as path:
        handle = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield path
        finally:
            os.close(handle)


def _exchange(
    arguments: list[str],
    prompt: str,
    environment: dict[str, str],
    cwd: str,
    events: _Events,
    stdout: _Stream,
    stderr: _Stream,
    deadline: float,
    idle_timeout: float,
) -> Outcome:
    inputs = memoryview(prompt.encode("utf-8"))
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=environment,
        start_new_session=True,
        bufsize=0,
    )
    assert (
        process.stdin is not None
        and process.stdout is not None
        and process.stderr is not None
    )
    last_output = time.monotonic()
    failure = ""
    returncode = -1
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, stream in ((process.stdout, stdout), (process.stderr, stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, stream)
            os.set_blocking(process.stdin.fileno(), False)
            if inputs:
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            while selector.get_map() or not _exited(process):
                now = time.monotonic()
                remaining = min(deadline - now, idle_timeout - (now - last_output))
                if remaining <= 0:
                    failure = (
                        "Overall deadline exceeded."
                        if now >= deadline
                        else "Provider output idle deadline exceeded."
                    )
                    break
                if stdout.exceeded or stderr.exceeded:
                    failure = "Provider stream exceeded its total size limit."
                    break
                if events.unsafe:
                    break
                for key, mask in selector.select(min(remaining, 0.05)):
                    if mask & selectors.EVENT_WRITE:
                        try:
                            inputs = inputs[os.write(key.fd, inputs[:65536]) :]
                        except BrokenPipeError:
                            inputs = memoryview(b"")
                        if not inputs:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                    else:
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            last_output = time.monotonic()
                            key.data.feed(chunk)
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
            # Also kill descendants after a clean leader exit, before reaping it.
    finally:
        try:
            returncode = terminate_process_group(process)
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()
            try:
                stdout.feed(b"", final=True)
            finally:
                stderr.feed(b"", final=True)
    if failure:
        return Outcome(ok=False, kind="output_limit" if "size limit" in failure else "timeout", detail=failure)
    return events.outcome(returncode, stderr.tail)


def invoke(
    provider: Provider,
    prompt: str,
    directory: Path,
    *,
    schema: dict[str, object],
    timeout: float = 900,
    idle_timeout: float = 300,
    disclosure: DisclosureIndex | None = None,
) -> Outcome:
    """Run once in a fresh directory and preserve only sanitized artifacts."""
    started = time.monotonic()
    events = _Events(provider.name)
    outcome = Outcome(
        ok=False, kind="unavailable", detail="Provider invocation could not start."
    )
    try:
        if disclosure:
            disclosure.scan(prompt)
        if os.name != "posix" or not hasattr(os, "WNOWAIT"):
            raise ValueError("Provider process cleanup requires POSIX waitid/WNOWAIT.")
        if provider.name not in {"claude", "codex"} or not provider.command:
            raise ValueError("Expected a Claude or Codex executable command.")
        if any(
            argument.startswith(("--dangerously-", "--allow-dangerously-"))
            for argument in provider.command
        ):
            raise ValueError("Provider commands must not bypass permissions.")
        if not provider.model or not provider.effort:
            raise ValueError("An explicit model and effort are required.")
        if not all(
            math.isfinite(value) and value > 0 for value in (timeout, idle_timeout)
        ):
            raise ValueError("Timeouts must be positive finite seconds.")
        serialized_schema = json.dumps(schema, allow_nan=False)
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = dict(os.environ)
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
            environment.pop(key, None)
        environment["AGENT_CONTEXT_WORKER"] = "1"
        if provider.name == "claude":
            environment["CLAUDE_CODE_EFFORT_LEVEL"] = provider.effort
        secrets = secret_values(environment)
        with contextlib.ExitStack() as stack:
            artifacts = {
                name: stack.enter_context((directory / name).open("xb"))
                for name in (
                    "stdout.jsonl",
                    "stderr.log",
                    "output-schema.json",
                    "empty-mcp.json",
                )
            }
            for artifact in artifacts.values():
                os.fchmod(artifact.fileno(), 0o600)
            artifacts["output-schema.json"].write(serialized_schema.encode("utf-8"))
            artifacts["empty-mcp.json"].write(b'{"mcpServers": {}}\n')
            artifacts["output-schema.json"].flush()
            artifacts["empty-mcp.json"].flush()
            cwd = stack.enter_context(_work_directory())
            outcome = _exchange(
                _arguments(provider, serialized_schema, directory),
                prompt,
                environment,
                cwd,
                events,
                _Stream(artifacts["stdout.jsonl"], secrets, events,
                        RawScanner(disclosure) if disclosure else None),
                _Stream(artifacts["stderr.log"], secrets, scanner=RawScanner(disclosure) if disclosure else None),
                started + timeout,
                idle_timeout,
            )
            if disclosure:
                disclosure.scan(outcome.text)
    except DisclosureError as error:
        outcome = Outcome(ok=False, kind="disclosure", detail=str(error))
    except FileExistsError:
        outcome = Outcome(
            ok=False,
            kind="invalid_output",
            detail="Attempt artifacts already exist; use a new directory.",
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Exception text may contain paths/argv or credentials; never persist it.
        outcome = Outcome(
            ok=False,
            kind="unavailable",
            detail=f"Provider transport failed ({type(error).__name__}).",
        )
    outcome.session_id = events.session_id
    outcome.model = events.model
    outcome.effort = events.effort
    outcome.elapsed = time.monotonic() - started
    return outcome
