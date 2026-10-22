"""Checks for source coverage, publication recovery, and context retention."""

import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from context_store import Project, RefreshSettings, Settings, Store, atomic_text, digest, lock, save_model
from refresh_context import (
    AgentResult, Document, GithubSnapshot, Permissions, PermissionSettings,
    RefreshState, Snapshot, SourceManifest, SourceStatus, Transaction,
    allowed_document, apply_snapshot, github_snapshot, local_references,
    main, merge_inbox, publish, recover, refresh, retain_meetings, run_agent,
    settings_for_stage, snapshot, validate_stage,
)


class RefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "context"
        self.stage = self.base / "stage"
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.before = Snapshot(documents=[
            Document(path="brief.md", text="# Brief\nOld brief\n"),
            Document(path="people.md", text="# People\n"),
            Document(path="decisions.md", text="# Decisions\n"),
            Document(path="inbox.md", text="# Inbox\nOriginal note\n"),
            Document(path="sources.md", text="# Pinned sources\nVerified sources\n## Discovered\n"),
        ])
        apply_snapshot(self.root, self.before, set())
        apply_snapshot(self.stage, self.before, set())
        self.manifest = SourceManifest(run_id="test", sources=[
            SourceStatus(source=name, status="ok")
            for name in ("slack", "linear", "notion", "granola", "github")
        ])
        save_model(self.stage / "source-status.json", self.manifest)

    def updated(self) -> Snapshot:
        return Snapshot(documents=[item.model_copy(update={"text": item.text.replace("Old brief", "New brief").replace("Original note", "Processed note")})
                                   for item in self.before.documents])

    def journal(self, root: Path | None = None) -> Transaction:
        after = self.updated()
        inbox = next(item for item in after.documents if item.path == "inbox.md")
        inbox.text = f"<!-- agent-context-refresh:{digest('test')} -->\n" + inbox.text
        transaction = Transaction(run_id="test", before=self.before, after=after)
        save_model((root or self.root) / "local/refresh/pending.json", transaction)
        return transaction

    def test_publish_preserves_concurrent_append_and_saves_rollback_snapshot(self) -> None:
        with (self.root / "inbox.md").open("a") as handle:
            handle.write("Concurrent note\n")
        publish(self.root, self.before, self.updated(), "test")
        self.assertIn("Processed note\nConcurrent note\n", (self.root / "inbox.md").read_text())
        self.assertIn("New brief", (self.root / "brief.md").read_text())
        saved = Snapshot.model_validate_json((self.root / "local/refresh/last-valid.json").read_text())
        self.assertIn("Concurrent note", next(item.text for item in saved.documents if item.path == "inbox.md"))
        self.assertFalse((self.root / "local/refresh/pending.json").exists())
        publish(self.root, snapshot(self.root), snapshot(self.root), "second")
        self.assertEqual((self.root / "inbox.md").read_text().count("<!-- agent-context-refresh:"), 1)

    def test_non_append_inbox_or_digest_edit_prevents_publication(self) -> None:
        for name in ("inbox.md", "brief.md"):
            with self.subTest(name=name):
                apply_snapshot(self.root, self.before, set())
                atomic_text(self.root / name, "A concurrent edit\n")
                with self.assertRaises(ValueError):
                    publish(self.root, self.before, self.updated(), "test")
                self.assertEqual((self.root / name).read_text(), "A concurrent edit\n")
                self.assertFalse((self.root / "local/refresh/pending.json").exists())

    def test_partial_publication_rolls_back(self) -> None:
        calls = 0

        def interrupted(root: Path, value: Snapshot, managed: set[str]) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                atomic_text(root / "brief.md", "# Brief\nNew brief\n")
                raise OSError("interrupted")
            apply_snapshot(root, value, managed)

        with patch("refresh_context.apply_snapshot", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted"):
                publish(self.root, self.before, self.updated(), "test")
        self.assertEqual(snapshot(self.root), self.before)
        self.assertFalse((self.root / "local/refresh/pending.json").exists())

    def test_failed_rollback_retains_journal_for_later_recovery(self) -> None:
        with patch("refresh_context.apply_snapshot", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                publish(self.root, self.before, self.updated(), "test")
        self.assertTrue((self.root / "local/refresh/pending.json").exists())
        self.assertTrue(recover(self.root))
        self.assertEqual(snapshot(self.root), self.before)
        self.assertFalse(recover(self.root))

    def test_crash_recovery_preserves_appends_before_and_after_inbox_rewrite(self) -> None:
        for applied in (False, True):
            with self.subTest(applied=applied):
                apply_snapshot(self.root, self.before, set())
                transaction = self.journal()
                if applied:
                    apply_snapshot(self.root, transaction.after, set())
                with (self.root / "inbox.md").open("a") as handle:
                    handle.write("Late note\n")
                recover(self.root)
                self.assertEqual((self.root / "inbox.md").read_text(), "# Inbox\nOriginal note\nLate note\n")
                self.assertEqual((self.root / "brief.md").read_text(), "# Brief\nOld brief\n")

    def test_recovery_retains_journal_when_human_changed_interrupted_snapshot(self) -> None:
        self.journal()
        atomic_text(self.root / "brief.md", "Keep this human edit\n")
        with self.assertRaisesRegex(ValueError, "concurrent edit"):
            recover(self.root)
        self.assertTrue((self.root / "local/refresh/pending.json").exists())
        self.assertEqual((self.root / "brief.md").read_text(), "Keep this human edit\n")

    def test_marker_disambiguates_common_inbox_prefix(self) -> None:
        self.before.documents[3].text = "# Inbox\n"
        apply_snapshot(self.root, self.before, set())
        transaction = self.journal()
        transaction.after.documents[3].text += "Generated note\n"
        save_model(self.root / "local/refresh/pending.json", transaction)
        apply_snapshot(self.root, transaction.after, set())
        with (self.root / "inbox.md").open("a") as handle:
            handle.write("Concurrent note\n")
        recover(self.root)
        self.assertEqual((self.root / "inbox.md").read_text(), "# Inbox\nConcurrent note\n")

    def test_complete_manifest_is_required_and_empty_sources_are_valid(self) -> None:
        self.assertEqual(validate_stage(self.stage, self.before, "test"), self.before)
        for bad in ("partial", "unavailable", "error"):
            value = self.manifest.model_copy(deep=True)
            value.sources[0] = SourceStatus(source="slack", status=bad)
            save_model(self.stage / "source-status.json", value)
            with self.assertRaisesRegex(ValueError, "incomplete sources"):
                validate_stage(self.stage, self.before, "test")
        value.sources[0] = SourceStatus(source="slack", status="empty")
        save_model(self.stage / "source-status.json", value)
        self.assertEqual(validate_stage(self.stage, self.before, "test"), self.before)
        with self.assertRaisesRegex(ValueError, "all five sources"):
            validate_stage(self.stage, self.before, "wrong-run")
        value.sources[0] = value.sources[1]
        save_model(self.stage / "source-status.json", value)
        with self.assertRaises(ValueError):
            validate_stage(self.stage, self.before, "test")

    def test_restricted_paths_symlinks_and_pinned_sources(self) -> None:
        for name in ("../outside.md", "projects/../../outside.md", "local/personal.md", "projects/sub/page.md", ".env"):
            self.assertFalse(allowed_document(name))
            with self.assertRaises(ValueError):
                apply_snapshot(self.stage, Snapshot(documents=[Document(path=name, text="text")]), set())
        outside = self.base / "outside.md"
        outside.write_text("Keep me")
        (self.stage / "unexpected.md").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            validate_stage(self.stage, self.before, "test")
        (self.stage / "unexpected.md").unlink()
        atomic_text(self.stage / "sources.md", "Changed pinned source\n## Discovered\n")
        with self.assertRaisesRegex(ValueError, "Pinned source"):
            validate_stage(self.stage, self.before, "test")
        self.assertEqual(outside.read_text(), "Keep me")

    def test_size_limits_and_secret_patterns(self) -> None:
        for text in ("line\n" * 81, "x" * 24001, "ghp_" + "x" * 30):
            atomic_text(self.stage / "brief.md", text)
            with self.assertRaises(ValueError):
                validate_stage(self.stage, self.before, "test")

    def test_transitive_relative_and_absolute_references_retain_old_meetings(self) -> None:
        value = Snapshot(documents=[
            Document(path="brief.md", text="[Plan](projects/plan.md)"),
            Document(path="projects/plan.md", text="[Meeting](../meetings/2020-01-01-a.md#decision)"),
            Document(path="meetings/2020-01-01-a.md", text=f"[Previous](<{Path.home()}/context/meetings/2019-01-01-b.md>)"),
            Document(path="meetings/2019-01-01-b.md", text="Accepted decision"),
            Document(path="meetings/2020-02-01-unused.md", text="Obsolete meeting"),
        ])
        result = retain_meetings(value, dt.date(2026, 10, 13))
        self.assertEqual([item.path for item in result.documents], [item.path for item in value.documents[:4]])
        self.assertEqual(local_references(Document(path="brief.md", text="[x]: ~/context/people.md\n[Remote](https://example.com/x.md)")), {"people.md"})
        value.documents[1].text += "\n[Missing](../meetings/missing.md)"
        with self.assertRaisesRegex(ValueError, "Broken digest reference"):
            retain_meetings(value)

    def test_recent_meetings_are_capped_and_referenced_overflow_is_rejected(self) -> None:
        today = dt.date(2026, 10, 13)
        meetings = [Document(path=f"meetings/2026-10-13-{index:03d}.md", text="Recent meeting") for index in range(165)]
        brief = Document(path="brief.md", text="Current brief")
        result = retain_meetings(Snapshot(documents=[brief, *meetings]), today)
        self.assertEqual(len(result.documents), 161)
        brief.text = "\n".join(f"[Meeting]({item.path})" for item in meetings)
        with self.assertRaisesRegex(ValueError, "160 meetings"):
            retain_meetings(Snapshot(documents=[brief, *meetings]), today)

    def test_github_cap_is_a_failure(self) -> None:
        result = subprocess.CompletedProcess(["gh"], 0, json.dumps([{}] * 50), "")
        with patch("refresh_context.command", return_value=result):
            with self.assertRaisesRegex(ValueError, "result cap"):
                github_snapshot("2026-10-13", "example/project", "example-user")

    def test_incremental_window_uses_previous_start_with_overlap_and_keeps_failed_state(self) -> None:
        started = time.time() - 7200
        previous = RefreshState(run_id="previous", status="succeeded", completed=started + 1200,
                                started=started, since=started - 3600, mode="incremental")
        save_model(self.root / "local/refresh/success.json", previous)
        atomic_text(self.tools / "prompt.md", "Refresh these files.")
        github = GithubSnapshot(mine=[], review_requested=[], involving_user=[], merged_in_window=[], other_open=[])
        with patch("refresh_context.github_snapshot", return_value=github), patch("refresh_context.run_agent", side_effect=ValueError("provider unavailable")) as agent:
            with self.assertRaisesRegex(ValueError, "provider unavailable"):
                refresh(self.root, self.tools, False)
        latest = RefreshState.model_validate_json((self.root / "local/refresh/latest.json").read_text())
        self.assertAlmostEqual(latest.since, started - 3600)
        self.assertEqual(latest.status, "failed")
        self.assertEqual(agent.call_args.args[3:5], (10, 2400))
        self.assertEqual(RefreshState.model_validate_json((self.root / "local/refresh/success.json").read_text()), previous)
        self.assertEqual(snapshot(self.root), self.before)

    def test_permissions_are_limited_to_stage_and_read_only_source_allowlist(self) -> None:
        source = PermissionSettings(permissions=Permissions(allow=["Read", "Bash", "mcp__slack__slack_search"], deny=["Bash"]))
        permissions = settings_for_stage(source, self.stage).permissions
        self.assertIn(f"Read(/{self.stage}/**)", permissions.allow)
        self.assertIn(f"Write(/{self.stage}/**)", permissions.allow)
        self.assertIn(f"Edit(/{self.stage}/**)", permissions.allow)
        self.assertNotIn("Bash", permissions.allow)
        self.assertEqual(permissions.deny, ["Bash"])

    def test_agent_failure_payloads_never_count_as_success(self) -> None:
        fake_home = self.base / "home"
        fake_home.mkdir()
        atomic_text(fake_home / ".claude.json", json.dumps({"mcpServers": {name: {} for name in ("slack", "linear-server", "notion", "granola")}}))
        save_model(self.tools / "settings.json", PermissionSettings(permissions=Permissions(allow=[], deny=[])))
        stdout, stderr = self.base / "out.json", self.base / "err.txt"
        for rc, subtype, error in ((1, "success", False), (0, "error_max_budget_usd", True), (0, "success", True)):
            def launch(args: list[str], **kwargs: object) -> Mock:
                stdout.write_text(AgentResult(subtype=subtype, is_error=error).model_dump_json())
                return Mock(wait=Mock(return_value=rc))

            with self.subTest(rc=rc, subtype=subtype, error=error), patch("refresh_context.Path.home", return_value=fake_home), patch("refresh_context.agents", return_value="{}"), patch("refresh_context.subprocess.Popen", side_effect=launch):
                with self.assertRaisesRegex(ValueError, "Refresh agent failed"):
                    run_agent(self.stage, self.tools, "prompt", 10, 30, stdout, stderr)

    def test_shared_read_lock_blocks_partial_digest_and_pending_recovery_hides_it(self) -> None:
        checkout = self.base / "checkout"
        checkout.mkdir()
        store = Store(Settings(context_root=self.root, projects=[Project(slug="example", roots=[checkout])]))
        project = store.root / "workspaces/example"
        atomic_text(store.local / "personal.md", "Keep answers concise.")
        atomic_text(project / "context.md", "Example project kernel.")
        atomic_text(project / "brief.md", "INCOMPLETE PUBLISHED DIGEST")
        entered = threading.Event()

        def read() -> str:
            entered.set()
            return store.load(checkout, "example").text

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with lock(project / "local/publish.lock"):
                future = pool.submit(read)
                self.assertTrue(entered.wait(1))
                self.assertFalse(future.done())
                self.journal(project)
            packet = future.result(timeout=3)
        self.assertNotIn("INCOMPLETE PUBLISHED DIGEST", packet)
        self.assertIn("Keep answers concise.", packet)
        self.assertIn("Example project kernel.", packet)
        self.assertIn("interrupted", packet.lower())

    def test_disabled_refresh_needs_no_project(self) -> None:
        settings = self.base / "settings.json"
        save_model(settings, Settings(context_root=self.root))
        with patch("sys.argv", ["refresh-context", "--settings", str(settings)]), patch("refresh_context.refresh") as run:
            self.assertEqual(main(), 0)
        run.assert_not_called()

    def test_enabled_refresh_requires_registered_project(self) -> None:
        for target in (None, "unregistered"):
            with self.subTest(target=target):
                settings = self.base / "settings.json"
                save_model(settings, Settings(context_root=self.root, refresh=RefreshSettings(enabled=True, project=target)))
                with patch("sys.argv", ["refresh-context", "--settings", str(settings)]), patch("refresh_context.refresh") as run:
                    self.assertEqual(main(), 1)
                run.assert_not_called()

    def test_stage_validation_requires_registered_project_when_refresh_disabled(self) -> None:
        for target in (None, "unregistered"):
            with self.subTest(target=target):
                settings = self.base / "settings.json"
                save_model(settings, Settings(context_root=self.root, refresh=RefreshSettings(project=target)))
                with patch("sys.argv", ["refresh-context", "--settings", str(settings), "--validate-stage", str(self.stage)]), patch("refresh_context.validate_stage") as validate:
                    self.assertEqual(main(), 1)
                validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
