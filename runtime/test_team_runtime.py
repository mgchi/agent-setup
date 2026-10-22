"""Offline protocol and process-boundary checks using actual child executables."""

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from disclosure import DisclosureIndex, build_index
from team_runtime import Outcome, Provider, invoke, redacted_tail

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
CODEX_SUCCESS = [
    {"type": "thread.started", "thread_id": "thread-fixture"},
    {"type": "turn.started"},
    {
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": '{"summary":"complete"}',
        },
    },
    {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 5}},
]
CLAUDE_SUCCESS = [
    {
        "type": "system",
        "subtype": "init",
        "model": "resolved-claude",
        "session_id": "session-fixture",
        "tools": [],
    },
    {
        "type": "assistant",
        "message": {
            "model": "resolved-claude",
            "content": [{"type": "text", "text": '{"summary":"complete"}'}],
        },
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"summary":"complete"}',
        "session_id": "session-fixture",
    },
]
FAKE_IMPORTS = """\
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
"""


@unittest.skipUnless(
    os.name == "posix" and hasattr(os, "WNOWAIT"), "POSIX process groups required"
)
class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sequence = 0

    def provider(self, name: str, body: str) -> Provider:
        self.sequence += 1
        executable = self.root / f"fake-{self.sequence}.py"
        executable.write_text(FAKE_IMPORTS + textwrap.dedent(body))
        return Provider(name=name, command=(sys.executable, str(executable)), model="chosen-model", effort="max")

    def call(
        self, provider: Provider, *, prompt: str = "packet", **options: float
    ) -> Outcome:
        directory = self.root / f"attempt-{self.sequence}"
        return invoke(
            provider,
            prompt,
            directory,
            schema=SCHEMA,
            timeout=options.get("timeout", 3),
            idle_timeout=options.get("idle_timeout", 2),
        )

    def events(
        self,
        provider: str,
        records: list[dict[str, object]],
        *,
        stderr: str = "",
        code: int = 0,
    ) -> Outcome:
        return self.call(
            self.provider(
                provider,
                f"""
            sys.stdin.read()
            for record in {records!r}:
                print(json.dumps(record), flush=True)
            print({stderr!r}, file=sys.stderr)
            sys.exit({code})
        """,
            )
        )

    def test_codex_terminal_json_and_session_are_required_and_model_not_inferred(
        self,
    ) -> None:
        result = self.events("codex", CODEX_SUCCESS)
        self.assertTrue(result.ok, result)
        self.assertEqual(json.loads(result.text), {"summary": "complete"})
        self.assertEqual(result.session_id, "thread-fixture")
        self.assertEqual(result.model, "")
        self.assertGreater(result.elapsed, 0)
        self.assertEqual(
            (self.root / "attempt-1/stdout.jsonl").read_text().count("\n"), 4
        )

    def test_claude_success_reads_reported_model(self) -> None:
        result = self.events("claude", CLAUDE_SUCCESS)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.model, "resolved-claude")
        self.assertEqual(result.session_id, "session-fixture")

    def test_decoded_provider_message_is_checked_for_private_context(self) -> None:
        context = self.root / "context"
        context.mkdir()
        phrase = " ".join(f"privateword{number:03d}" for number in range(48))
        (context / "brief.md").write_text(phrase)
        records = [*CODEX_SUCCESS[:2], {"type": "item.completed",
                   "item": {"type": "agent_message", "text": json.dumps({"summary": phrase})}},
                   CODEX_SUCCESS[-1]]
        provider = self.provider("codex", f"""
            sys.stdin.read()
            for record in {records!r}:
                serialized = json.dumps(record, separators=(',', ':'))
                print(serialized.replace(' ', '\\\\u0020'), flush=True)
        """)
        outcome = invoke(provider, "Safe request", self.root / "private-output", schema=SCHEMA,
                         timeout=3, idle_timeout=2, disclosure=build_index(context.resolve()))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, "disclosure")
        self.assertNotIn(phrase, outcome.model_dump_json())

    def test_prompt_disclosure_is_rejected_before_starting_a_provider(self) -> None:
        provider = self.provider("codex", "raise RuntimeError('Should never start')")
        outcome = invoke(provider, "public " * 5000 + "sk-" + "a" * 30,
                         self.root / "blocked-input", schema=SCHEMA, timeout=3, idle_timeout=2,
                         disclosure=DisclosureIndex(root=self.root.resolve()))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.kind, "disclosure")

    def test_claude_structured_output_can_have_an_empty_result(self) -> None:
        records = [
            CLAUDE_SUCCESS[0],
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "",
                "structured_output": {"summary": "schema output"},
            },
        ]
        result = self.events("claude", records)
        self.assertTrue(result.ok, result)
        self.assertEqual(json.loads(result.text), {"summary": "schema output"})

    def test_codex_explicit_metadata_is_reported(self) -> None:
        result = self.events(
            "codex",
            [{"type": "thread.started", "model": "resolved-codex"}, *CODEX_SUCCESS[1:]],
        )
        self.assertTrue(result.ok, result)
        self.assertEqual(result.model, "resolved-codex")

    def test_reported_effort_is_recorded_but_never_inferred_from_request(self) -> None:
        absent = self.events("codex", CODEX_SUCCESS)
        self.assertEqual(absent.effort, "")
        reported = self.events(
            "codex",
            [
                {
                    "type": "thread.started",
                    "model": "resolved-codex",
                    "reasoning_effort": "high",
                },
                *CODEX_SUCCESS[1:],
            ],
        )
        self.assertTrue(reported.ok)
        self.assertEqual(reported.effort, "high")

    def test_check_tail_redacts_before_a_truncation_boundary_can_split_a_secret(
        self,
    ) -> None:
        value = "synthetic-private-value-1234567890"
        with (
            tempfile.TemporaryFile() as source,
            patch.dict(os.environ, {"TEST_TOKEN": value}),
        ):
            source.write(b"prefix\n" + value.encode() + b"x" * 15995)
            tail = redacted_tail(source)
        self.assertLessEqual(len(tail.encode()), 16000)
        self.assertNotIn(value[-5:], tail)
        self.assertNotIn(value, tail)

    def test_multiline_secret_fragments_are_redacted_across_provider_events(
        self,
    ) -> None:
        first, second = "synthetic-first-line-123456", "synthetic-second-line-987654"
        secret = first + "\n" + second
        records = [
            *CODEX_SUCCESS[:2],
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "text": first,
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps({"summary": second}),
                },
            },
            CODEX_SUCCESS[-1],
        ]
        with patch.dict(os.environ, {"AUDIT_TOKEN": secret}):
            result = self.events("codex", records, stderr=secret)
        self.assertTrue(result.ok, result)
        for surface in (
            result.text,
            (self.root / "attempt-1/stdout.jsonl").read_text(),
            (self.root / "attempt-1/stderr.log").read_text(),
        ):
            self.assertNotIn(secret, surface)
            self.assertNotIn(first, surface)
            self.assertNotIn(second, surface)

    def test_success_text_and_stderr_mentioning_quota_are_not_errors(self) -> None:
        records = [
            CODEX_SUCCESS[0],
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": '{"summary":"test quota and invalid credentials paths"}',
                },
            },
            CODEX_SUCCESS[-1],
        ]
        result = self.events(
            "codex", records, stderr="Warning: this fixture checks quota errors."
        )
        self.assertTrue(result.ok, result)

    def test_no_terminal_success_cannot_use_partial_assistant_output(self) -> None:
        for provider, records in (
            ("codex", CODEX_SUCCESS[:-1]),
            ("claude", CLAUDE_SUCCESS[:-1]),
        ):
            with self.subTest(provider=provider):
                result = self.events(provider, records)
                self.assertFalse(result.ok)
                self.assertEqual(result.kind, "invalid_output")
                self.assertEqual(result.text, "")

    def test_empty_success_and_tool_xml_and_invalid_json_are_rejected(self) -> None:
        for provider in ("claude", "codex"):
            for answer in (
                "",
                "<tool_call><name>Write</name></tool_call>",
                "[]",
                "null",
                '{"partial":',
                '{"n":NaN}',
                '{"n":1e999}',
                r'{"summary":"\ud800"}',
                '{"key":1,"key":2}',
                '```json\n{"summary":"fenced"}\n```',
            ):
                with self.subTest(provider=provider, answer=answer):
                    records = (
                        [
                            {
                                "type": "result",
                                "subtype": "success",
                                "is_error": False,
                                "result": answer,
                            }
                        ]
                        if provider == "claude"
                        else [
                            {
                                "type": "item.completed",
                                "item": {"type": "agent_message", "text": answer},
                            },
                            CODEX_SUCCESS[-1],
                        ]
                    )
                    result = self.events(provider, records)
                    self.assertEqual(result.kind, "invalid_output", result)
                    self.assertEqual(result.text, "")

    def test_invalid_event_or_truncated_tail_invalidates_earlier_success(self) -> None:
        for tail in (
            "not JSON\n",
            '{"type":',
            "[]\n",
            "{}\n",
            '{"type":"unknown","n":NaN}\n',
        ):
            with self.subTest(tail=tail):
                result = self.call(
                    self.provider(
                        "codex",
                        f"""
                    for record in {CODEX_SUCCESS!r}:
                        print(json.dumps(record), flush=True)
                    sys.stdout.write({tail!r})
                """,
                    )
                )
                self.assertEqual(result.kind, "invalid_output", result)

    def test_trailing_complete_json_without_newline_is_accepted(self) -> None:
        result = self.call(
            self.provider(
                "codex",
                rf"""
            sys.stdout.write('\n'.join(json.dumps(record) for record in {CODEX_SUCCESS!r}))
        """,
            )
        )
        self.assertTrue(result.ok, result)

    def test_terminal_then_another_answer_or_turn_is_rejected(self) -> None:
        for tail in (CODEX_SUCCESS[1], CODEX_SUCCESS[2], CODEX_SUCCESS[3]):
            with self.subTest(tail=tail):
                result = self.events("codex", [*CODEX_SUCCESS, tail])
                self.assertEqual(result.kind, "invalid_output", result)

    def test_claude_error_flag_overrides_success_subtype(self) -> None:
        result = self.events(
            "claude",
            [
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "result": "Insufficient quota",
                }
            ],
        )
        self.assertEqual(result.kind, "quota", result)

    def test_claude_rejects_an_answer_or_terminal_after_completion(self) -> None:
        for tail in CLAUDE_SUCCESS[1:]:
            with self.subTest(tail=tail):
                result = self.events("claude", [*CLAUDE_SUCCESS, tail])
                self.assertEqual(result.kind, "invalid_output", result)

    def test_invalid_utf8_cannot_be_replaced_into_success(self) -> None:
        result = self.call(
            self.provider(
                "codex",
                f"""
            sys.stdout.buffer.write(b'{{"type":"diagnostic","text":"' + bytes([255]) + b'"}}\\n')
            sys.stdout.buffer.flush()
            for record in {CODEX_SUCCESS!r}:
                print(json.dumps(record), flush=True)
        """,
            )
        )
        self.assertEqual(result.kind, "invalid_output", result)

    def test_oversized_event_is_drained_without_accepting_later_success(self) -> None:
        result = self.call(
            self.provider(
                "codex",
                f"""
            print(json.dumps({{"type":"diagnostic","text":"x" * (9 * 1024 * 1024)}}), flush=True)
            for record in {CODEX_SUCCESS!r}:
                print(json.dumps(record), flush=True)
        """,
            ),
            timeout=5,
        )
        self.assertEqual(result.kind, "invalid_output", result)
        self.assertIn("8 MiB", result.detail)
        self.assertLess((self.root / "attempt-1/stdout.jsonl").stat().st_size, 4096)

    def test_actual_structured_errors_have_actionable_classes(self) -> None:
        for detail, expected in (
            ("Usage limit exceeded; credits exhausted", "quota"),
            ("429 rate_limit_error: insufficient_quota", "quota"),
            ("Daily usage cap reached", "quota"),
            ("HTTP 429", "transient"),
            ("rate_limit_error: retry later", "transient"),
            ("Too many requests", "transient"),
            ("401 authentication failed", "auth"),
            ("model_not_found: requested model does not exist", "unavailable"),
            ("503 service unavailable", "transient"),
            ("unknown internal condition", "exit"),
        ):
            for provider in ("codex", "claude"):
                with self.subTest(provider=provider, detail=detail):
                    records = (
                        [{"type": "turn.failed", "error": {"message": detail}}]
                        if provider == "codex"
                        else [
                            {
                                "type": "result",
                                "subtype": "error_during_execution",
                                "is_error": True,
                                "errors": [detail],
                            }
                        ]
                    )
                    result = self.events(provider, records)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.kind, expected, result)

    def test_nonzero_exit_is_never_a_success_and_stderr_is_classified(self) -> None:
        result = self.events(
            "codex", CODEX_SUCCESS, stderr="HTTP 429 rate limit", code=1
        )
        self.assertEqual(result.kind, "transient", result)
        self.assertEqual(result.text, "")

    def test_rate_limit_metadata_does_not_turn_success_into_an_error(self) -> None:
        event = {
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed_warning", "utilization": 0.8},
        }
        for provider, records in (("codex", CODEX_SUCCESS), ("claude", CLAUDE_SUCCESS)):
            with self.subTest(provider=provider):
                result = self.events(provider, [event, *records, event])
                self.assertTrue(result.ok, result)
                self.assertEqual(result.kind, "")

    def test_missing_executable_reports_unavailable_without_logging_argv(self) -> None:
        result = self.call(
            Provider(
                name="codex",
                command=(str(self.root / "missing"), "secret-command-argument"),
                model="chosen",
                effort="max",
            )
        )
        self.assertEqual(result.kind, "unavailable", result)
        self.assertNotIn("secret-command-argument", result.detail)

    def test_command_prefix_cannot_opt_out_of_permission_controls(self) -> None:
        marker = self.root / "launched"
        provider = self.provider("codex", f"Path({str(marker)!r}).touch()")
        for flag in (
            "--dangerously-bypass-approvals-and-sandbox",
            "--allow-dangerously-skip-permissions",
        ):
            with self.subTest(flag=flag):
                result = self.call(
                    Provider(
                        name=provider.name,
                        command=(*provider.command, flag),
                        model=provider.model,
                        effort=provider.effort,
                    )
                )
                self.assertEqual(result.kind, "unavailable", result)
        self.assertFalse(marker.exists())

    def test_tools_exposed_or_executed_fail_closed(self) -> None:
        for provider, event in (
            ("claude", {"type": "system", "subtype": "init", "tools": ["Bash"]}),
            (
                "claude",
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Write"}]},
                },
            ),
            (
                "codex",
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": "touch unsafe"},
                },
            ),
        ):
            with self.subTest(provider=provider):
                result = self.call(
                    self.provider(
                        provider,
                        f"""
                    print(json.dumps({event!r}), flush=True)
                    time.sleep(10)
                """,
                    )
                )
                self.assertEqual(result.kind, "invalid_output", result)
                self.assertLess(result.elapsed, 2)

    def test_claude_schema_synthetic_tool_is_allowed(self) -> None:
        result = self.events(
            "claude",
            [
                {"type": "system", "subtype": "init", "tools": ["StructuredOutput"]},
                *CLAUDE_SUCCESS[1:],
            ],
        )
        self.assertTrue(result.ok, result)

    def test_provider_model_effort_prompt_and_tool_controls(self) -> None:
        for name in ("claude", "codex"):
            with (
                self.subTest(provider=name),
                patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}),
            ):
                provider = self.provider(
                    name,
                    """
                    data = {"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
                            "effort_env": os.environ.get("CLAUDE_CODE_EFFORT_LEVEL")}
                    if "--output-schema" in sys.argv:
                        data["schema"] = json.loads(Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
                    else:
                        data["schema"] = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
                        data["mcp"] = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
                    if "exec" in sys.argv:
                        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(data)}}))
                        print(json.dumps({"type": "turn.completed"}))
                    else:
                        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": data}))
                """,
                )
                provider = Provider(
                    name=provider.name, command=provider.command, model="override-model", effort="high"
                )
                result = self.call(
                    provider, prompt="the only prompt\nwith another line"
                )
                self.assertTrue(result.ok, result)
                data = json.loads(result.text)
                argv = data["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], "override-model")
                self.assertEqual(data["schema"], SCHEMA)
                self.assertEqual(data["stdin"], "the only prompt\nwith another line")
                work = Path(data["cwd"])
                self.assertFalse(work.is_relative_to(Path.cwd()))
                self.assertEqual(work.parent, Path("/tmp").resolve())
                self.assertTrue(work.name.startswith("agent-context-provider-"))
                self.assertFalse(work.exists())
                self.assertNotIn("--dangerously-skip-permissions", argv)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                if name == "claude":
                    self.assertEqual(argv[argv.index("--effort") + 1], "high")
                    self.assertEqual(data["effort_env"], "high")
                    self.assertEqual(argv[argv.index("--tools") + 1], "")
                    self.assertEqual(data["mcp"], {"mcpServers": {}})
                    self.assertIn("--safe-mode", argv)
                    self.assertIn("--no-session-persistence", argv)
                    self.assertIn("--strict-mcp-config", argv)
                else:
                    self.assertIn("--ignore-user-config", argv)
                    self.assertIn("--skip-git-repo-check", argv)
                    self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
                    self.assertIn('model_reasoning_effort="high"', argv)
                    self.assertIn('approval_policy="never"', argv)
                    self.assertIn('web_search="disabled"', argv)
                    disabled = {
                        argv[index + 1]
                        for index, flag in enumerate(argv[:-1])
                        if flag == "--disable"
                    }
                    self.assertTrue(
                        {
                            "shell_tool",
                            "multi_agent",
                            "apps",
                            "plugins",
                            "hooks",
                            "browser_use",
                            "computer_use",
                        }.issubset(disabled)
                    )

    def test_work_directory_is_removed_after_a_failed_call(self) -> None:
        note = self.root / "cwd"
        result = self.call(
            self.provider(
                "codex",
                f"""
            Path({str(note)!r}).write_text(os.getcwd())
            sys.exit(1)
        """,
            )
        )
        self.assertFalse(result.ok)
        self.assertFalse(Path(note.read_text()).exists())

    def test_orphaned_work_directories_are_swept_but_live_and_fresh_kept(
        self,
    ) -> None:
        orphan, live, fresh = (
            Path(tempfile.mkdtemp(prefix="agent-context-provider-", dir="/tmp"))
            for _ in range(3)
        )
        for path in (orphan, live, fresh):
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        stale = time.time() - 120
        os.utime(orphan, (stale, stale))
        os.utime(live, (stale, stale))
        holder = os.open(live, os.O_RDONLY)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(self.events("codex", CODEX_SUCCESS).ok)
        self.assertFalse(orphan.exists())
        self.assertTrue(live.exists())
        self.assertTrue(fresh.exists())

    def test_large_prompt_and_both_output_streams_do_not_deadlock(self) -> None:
        provider = self.provider(
            "codex",
            r"""
            for index in range(128):
                print(json.dumps({"type": "diagnostic", "text": "x" * 8192}), flush=True)
                sys.stderr.write("y" * 8192 + "\n")
                sys.stderr.flush()
            prompt = sys.stdin.read()
            print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"length": len(prompt)})}}), flush=True)
            print(json.dumps({"type": "turn.completed"}), flush=True)
        """,
        )
        result = self.call(provider, prompt="z" * (1024 * 1024), timeout=5)
        self.assertTrue(result.ok, result)
        self.assertEqual(json.loads(result.text)["length"], 1024 * 1024)
        self.assertGreater(
            (self.root / "attempt-1/stdout.jsonl").stat().st_size, 1024 * 1024
        )
        self.assertGreater(
            (self.root / "attempt-1/stderr.log").stat().st_size, 1024 * 1024
        )

    def test_long_single_event_is_kept(self) -> None:
        result = self.events(
            "codex",
            [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps({"summary": "x" * (1024 * 1024)}),
                    },
                },
                CODEX_SUCCESS[-1],
            ],
        )
        self.assertTrue(result.ok, result)
        self.assertEqual(len(json.loads(result.text)["summary"]), 1024 * 1024)

    def test_secrets_are_redacted_even_across_read_boundaries(self) -> None:
        secret = "fixture-sensitive-credential-123456789"
        provider = self.provider(
            "codex",
            r"""
            secret = os.environ["FIXTURE_API_KEY"]
            for chunk in ["Error: " + secret[:10], secret[10:] + "\n"]:
                sys.stderr.write(chunk)
                sys.stderr.flush()
                time.sleep(0.02)
            print(json.dumps({"type": "error", "message": "401 invalid key " + secret}), flush=True)
            sys.exit(1)
        """,
        )
        with patch.dict(os.environ, {"FIXTURE_API_KEY": secret}):
            result = self.call(provider)
        self.assertEqual(result.kind, "auth", result)
        self.assertNotIn(secret, result.detail)
        for name in ("stdout.jsonl", "stderr.log"):
            text = (self.root / "attempt-1" / name).read_text()
            self.assertNotIn(secret, text)
            self.assertIn("[REDACTED]", text)
            self.assertEqual(
                (self.root / "attempt-1" / name).stat().st_mode & 0o777, 0o600
            )

    def test_attempt_artifacts_cannot_be_overwritten(self) -> None:
        provider = self.provider(
            "codex",
            f"for record in {CODEX_SUCCESS!r}:\n    print(json.dumps(record))\n",
        )
        first = self.call(provider)
        original = (self.root / "attempt-1/stdout.jsonl").read_bytes()
        second = self.call(provider)
        self.assertTrue(first.ok, first)
        self.assertEqual(second.kind, "invalid_output", second)
        self.assertEqual((self.root / "attempt-1/stdout.jsonl").read_bytes(), original)

    def test_idle_deadline_kills_a_silent_process(self) -> None:
        result = self.call(self.provider("codex", "time.sleep(20)"), idle_timeout=0.15)
        self.assertEqual(result.kind, "timeout", result)
        self.assertIn("idle", result.detail)
        self.assertLess(result.elapsed, 1.5)

    def test_overall_deadline_bounds_a_chatty_process(self) -> None:
        result = self.call(
            self.provider(
                "codex",
                """
            while True:
                print(json.dumps({"type": "diagnostic"}), flush=True)
                time.sleep(0.01)
        """,
            ),
            timeout=0.2,
            idle_timeout=1,
        )
        self.assertEqual(result.kind, "timeout", result)
        self.assertIn("Overall", result.detail)
        self.assertLess(result.elapsed, 1.5)

    def test_deadline_still_applies_when_stdin_is_never_read(self) -> None:
        result = self.call(
            self.provider("codex", "time.sleep(20)"),
            prompt="z" * (1024 * 1024),
            timeout=0.2,
            idle_timeout=1,
        )
        self.assertEqual(result.kind, "timeout", result)
        self.assertLess(result.elapsed, 1.5)

    def test_deadline_still_applies_after_output_pipes_close(self) -> None:
        result = self.call(
            self.provider(
                "codex",
                """
            sys.stdout.close()
            sys.stderr.close()
            os.close(1)
            os.close(2)
            time.sleep(20)
        """,
            ),
            timeout=0.2,
            idle_timeout=1,
        )
        self.assertEqual(result.kind, "timeout", result)
        self.assertLess(result.elapsed, 1.5)

    def test_timeout_kills_descendant_even_after_leader_exit(self) -> None:
        marker = self.root / "escaped-write"
        child = f"import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.7); Path({str(marker)!r}).write_text('late')"
        result = self.call(
            self.provider(
                "codex",
                f"""
            subprocess.Popen([sys.executable, '-c', {child!r}])
            sys.exit(0)
        """,
            ),
            timeout=0.15,
            idle_timeout=1,
        )
        self.assertEqual(result.kind, "timeout", result)
        time.sleep(0.75)
        self.assertFalse(marker.exists(), "descendant survived the leader's exit")

    def test_success_cleanup_kills_descendant_with_closed_output_pipes(self) -> None:
        marker = self.root / "escaped-write"
        child = f"import time; from pathlib import Path; time.sleep(0.5); Path({str(marker)!r}).write_text('late')"
        result = self.call(
            self.provider(
                "codex",
                f"""
            subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for record in {CODEX_SUCCESS!r}:
                print(json.dumps(record), flush=True)
        """,
            )
        )
        self.assertTrue(result.ok, result)
        time.sleep(0.55)
        self.assertFalse(marker.exists(), "descendant survived successful cleanup")

    def test_supervisor_interruption_does_not_leave_its_process_group(self) -> None:
        marker = self.root / "late-write"
        ready = self.root / "ready"
        fake = self.provider(
            "codex",
            f"""
            Path({str(ready)!r}).write_text('ready')
            time.sleep(0.8)
            Path({str(marker)!r}).write_text('late')
        """,
        )
        driver = self.root / "driver.py"
        driver.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            "from team_runtime import Provider, invoke\n"
            f"invoke(Provider(name='codex', command={fake.command!r}, model='model', effort='max'), 'packet', Path({str(self.root / 'interrupt')!r}), schema={{}})\n"
        )
        supervisor = subprocess.Popen(
            [sys.executable, str(driver)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            supervisor.send_signal(signal.SIGINT)
            supervisor.wait(timeout=2)
            time.sleep(0.85)
            self.assertFalse(marker.exists())
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
