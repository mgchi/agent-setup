"""Behavioral checks for private memory, retrieval, and retention."""

import concurrent.futures
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from context_store import Capture, Memory, Packet, Project, Settings, Store, atomic_text, direct_user_text, git, lock


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(Settings(context_root=self.root / "context", max_runs=2,
                                   projects=[Project(slug="example", roots=[self.project])]))
        self.store.capture(Capture(run_id="test", prompt="I prefer concise responses.\nI decided to ship the editor this week.", scope="example"))

    def memory(self, **changes: object) -> Memory:
        return Memory.model_validate({"key": "style", "kind": "preference", "scope": "personal",
            "text": "Use concise responses.", "evidence": "I prefer concise responses.",
            "source": "session:test", "run_id": "test", **changes})

    def test_explicit_evidence_and_replacement(self) -> None:
        self.assertTrue(self.store.remember(self.memory()))
        self.assertFalse(self.store.remember(self.memory()))
        self.assertTrue(self.store.remember(self.memory(text="Keep answers brief.")))
        self.assertEqual([m.text for m in self.store.memories("personal")], ["Keep answers brief."])
        self.store.remember(self.memory(key="duplicate", text="Keep answers brief."))
        self.assertEqual(len(self.store.memories("personal")), 1)

    def test_assistant_and_quoted_content_cannot_supply_user_evidence(self) -> None:
        attacks = ["> I prefer concise responses.", '"I prefer concise responses."',
                   "```\nI prefer concise responses.\n```", "<tool>\nI prefer concise responses.\n</tool>",
                   "<tool>\n</other>\nI prefer concise responses.", "    I prefer concise responses."]
        for attack in attacks:
            with self.subTest(attack=attack):
                self.store.capture(Capture(run_id="test", prompt=attack, final="I prefer concise responses."))
                with self.assertRaises(ValueError):
                    self.store.remember(self.memory())

    def test_retains_real_prose_after_matched_wrapper(self) -> None:
        self.assertIn("I prefer concise responses.", direct_user_text("<data>\ntext\n</data>\nI prefer concise responses."))

    def test_plain_less_than_instructions_survive_xml_filter(self) -> None:
        prompt = "I prefer checks that take <60s."
        self.assertEqual(direct_user_text(prompt), prompt)

    def test_task_instructions_do_not_become_accepted_decisions(self) -> None:
        self.store.capture(Capture(run_id="test", prompt="Build a migration tool.", scope="example"))
        with self.assertRaises(ValueError):
            self.store.remember(self.memory(kind="decision", scope="example", evidence="Build a migration tool."))

    def test_query_retrieves_whole_relevant_memories_within_budget(self) -> None:
        lines = [f"I prefer detailed engineering rule {index}." for index in range(20)]
        lines.append("I prefer Linear tickets assigned to owner@example.invalid.")
        self.store.capture(Capture(run_id="test", prompt="\n".join(lines), scope="example"))
        for index, line in enumerate(lines):
            self.store.remember(self.memory(key=f"rule-{index}", text=line + " " + "Complete rule. " * 45, evidence=line))
        packet = self.store.load(self.root, "Linear tickets assigned")
        self.assertLessEqual(len(packet.text), self.store.settings.packet_chars)
        self.assertIn("owner@example.invalid", packet.text)
        for line in packet.text.splitlines():
            if line.startswith("- ["):
                self.assertTrue(line.endswith("Complete rule. "))

    def test_source_and_scope_must_match_capture(self) -> None:
        with self.assertRaises(ValueError):
            self.store.remember(self.memory(source="session:other"))
        self.store.capture(Capture(run_id="test", prompt="I prefer concise responses.", scope="personal"))
        with self.assertRaises(ValueError):
            self.store.remember(self.memory(scope="example"))

    def test_expired_capture_and_memories_are_removed(self) -> None:
        self.store.remember(self.memory(expires=time.time() - 1))
        self.assertEqual(self.store.memories("personal"), [])
        self.store.capture(Capture(run_id="test", prompt="I prefer concise responses.", created=time.time() - 9 * 86400))
        with self.assertRaises(ValueError):
            self.store.remember(self.memory())
        self.store.prune()
        self.assertEqual(self.store.status(self.root).capture_count, 0)

    def test_lesson_requires_actual_file_evidence(self) -> None:
        evidence = self.root / "test-output.txt"
        evidence.write_text("3 tests passed")
        self.store.remember(self.memory(kind="lesson", source=str(evidence), evidence="3 tests passed"))
        with self.assertRaises(ValueError):
            self.store.remember(self.memory(kind="lesson", source=str(evidence), evidence="4 tests passed"))

    def test_standing_preferences_survive_lesson_churn_and_time(self) -> None:
        self.store.settings.max_memory = 2
        self.store.remember(self.memory())
        evidence = self.root / "checks.txt"
        evidence.write_text("3 checks passed")
        for index in range(5):
            self.store.remember(self.memory(key=f"lesson-{index}", kind="lesson",
                text=f"Reusable lesson number {index}.", source=str(evidence), evidence="3 checks passed"))
        self.assertEqual({item.key for item in self.store.memories("personal")}, {"style", "lesson-4"})
        with patch("context_store.time.time", return_value=time.time() + 400 * 86400):
            self.store.prune()
            self.assertEqual([item.key for item in self.store.memories("personal")], ["style"])

    def test_full_preference_store_rejects_additions_without_losing_rules(self) -> None:
        self.store.settings.max_memory = 1
        self.store.remember(self.memory())
        with self.assertRaisesRegex(ValueError, "standing preferences"):
            self.store.remember(self.memory(key="another", text="A different standing preference."))
        evidence = self.root / "checks.txt"
        evidence.write_text("3 checks passed")
        with self.assertRaisesRegex(ValueError, "standing preferences"):
            self.store.remember(self.memory(key="lesson", kind="lesson", text="A reusable lesson.",
                source=str(evidence), evidence="3 checks passed"))
        self.assertEqual([item.text for item in self.store.memories("personal")], ["Use concise responses."])
        self.store.remember(self.memory(text="Keep answers brief."))
        self.assertEqual([item.text for item in self.store.memories("personal")], ["Keep answers brief."])

    def test_preference_replaces_old_lesson_when_store_is_full(self) -> None:
        self.store.settings.max_memory = 1
        evidence = self.root / "checks.txt"
        evidence.write_text("3 checks passed")
        self.store.remember(self.memory(key="lesson", kind="lesson", text="A reusable lesson.",
            source=str(evidence), evidence="3 checks passed"))
        self.store.remember(self.memory())
        self.assertEqual([item.key for item in self.store.memories("personal")], ["style"])

    def test_decisions_publish_once_after_crash_and_concurrent_retry(self) -> None:
        decision = self.memory(kind="decision", scope="example", evidence="I decided to ship the editor this week.", text="Ship the editor this week.")
        with patch.object(self.store, "flush_outbox", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.store.remember(decision)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for future in [pool.submit(self.store.flush_outbox) for _ in range(3)]:
                future.result()
        self.store.remember(decision)
        inbox = (self.store.root / "workspaces/example/inbox.md").read_text()
        self.assertEqual(inbox.count("Ship the editor this week."), 1)
        self.assertEqual(self.store.status(self.root).pending_inbox, 0)

    def test_forget_removes_unpublished_decision(self) -> None:
        with patch.object(self.store, "flush_outbox"):
            self.store.remember(self.memory(kind="decision", scope="example", evidence="I decided to ship the editor this week."))
        self.store.forget("example", "style")
        self.store.flush_outbox()
        self.assertFalse((self.store.root / "workspaces/example/inbox.md").exists())

    def test_linked_directories_database_and_locks_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        target = self.root / "linked-context"
        target.mkdir()
        (target / "local").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            Store(Settings(context_root=target))
        with self.assertRaises(ValueError):
            with lock(target / "local" / "a.lock"):
                self.fail("linked lock accepted")
        self.store.db.unlink()
        (outside / "data").write_text("unchanged")
        self.store.db.symlink_to(outside / "data")
        with self.assertRaises(ValueError):
            Store(self.store.settings)
        self.assertEqual((outside / "data").read_text(), "unchanged")

    def test_cleanup_only_deletes_owned_inactive_runs(self) -> None:
        runs = self.store.local / "runs"
        runs.mkdir()
        old = time.time() - 20 * 86400
        paths = [runs / ("run-" + str(i) * 24) for i in range(4)]
        for path in paths:
            path.mkdir()
            (path / "result").write_text("data")
            os.utime(path, (old, old))
        foreign = runs / "my-notes"
        foreign.mkdir()
        (paths[1] / "pending").touch()
        os.utime(paths[1], (old, old))
        (runs / ("run-" + "e" * 24)).symlink_to(foreign, target_is_directory=True)
        with lock(paths[0] / "assessment.lock"):
            result = self.store.prune_runs()
        self.assertEqual(result.removed_runs, 2)
        self.assertTrue(paths[0].exists())
        self.assertTrue(paths[1].exists())
        self.assertTrue(foreign.exists())

    def test_retrieval_is_bounded_personal_scope_omits_project(self) -> None:
        atomic_text(self.store.local / "personal.md", "Personal preferences\n" * 400)
        project_root = self.store.root / "workspaces/example"
        atomic_text(project_root / "context.md", "Internal project context")
        atomic_text(project_root / "brief.md", "Private customer detail")
        packet = self.store.load(self.project, "context: personal")
        self.assertEqual(packet.scope, "personal")
        self.assertNotIn("Internal project", packet.text)
        self.assertNotIn("Private customer", packet.text)
        self.assertLessEqual(len(packet.text), self.store.settings.packet_chars)
        self.assertNotIn(".env", json.dumps(packet.sources))

    def test_explicit_source_registration_and_additive_defaults(self) -> None:
        source = self.root / "canonical.md"
        source.write_text("Canonical project instructions.\n")
        registration = Project(slug="example", roots=[self.project], context_sources=[source, source],
                               privacy_roots=[self.root], collaboration="exchange")
        restored = Project.model_validate_json(registration.model_dump_json())
        self.assertEqual(restored.context_sources, [source])
        self.assertEqual(restored.privacy_roots, [self.root])
        self.assertEqual(restored.collaboration, "exchange")
        self.assertEqual(Project(slug="legacy").collaboration, "advisory")
        packet = Packet(scope="personal", text="", sources=[])
        self.assertEqual(packet.source_receipts, [])
        self.assertEqual(packet.omitted_sources, [])

    def test_context_sources_reject_implicit_sensitive_and_nonregular_paths(self) -> None:
        source = self.root / "canonical.md"
        source.write_text("Safe context.\n")
        linked_file = self.root / "linked.md"
        linked_file.symlink_to(source)
        linked_directory = self.root / "linked-directory"
        linked_directory.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        forbidden = [Path("relative.md"), self.root, self.root / "missing.md", linked_file,
                     linked_directory / source.name, fifo]
        for name in ("*.md", "?.md", "[a].md", ".env", ".env.local", "credentials.json", "private.key"):
            path = self.root / name
            path.write_text("Must not load.\n")
            forbidden.append(path)
        private_directory = self.root / "secrets"
        private_directory.mkdir()
        nested = private_directory / "instructions.md"
        nested.write_text("Must not load.\n")
        forbidden.append(nested)
        for path in forbidden:
            with self.subTest(path=path), self.assertRaises(ValueError):
                Project(slug="example", context_sources=[path])

    def test_privacy_roots_are_explicit_directories_and_never_retrieved(self) -> None:
        protected = self.root / "protected"
        protected.mkdir()
        source = protected / "hidden.md"
        source.write_text("Scanner-only content must not enter packets.\n")
        linked = self.root / "linked-directory"
        linked.symlink_to(protected, target_is_directory=True)
        for path in (Path("relative"), source, self.root / "missing", linked, Path("/"), Path.home()):
            with self.subTest(path=path), self.assertRaises(ValueError):
                Project(slug="example", privacy_roots=[path])
        self.store.settings.projects = [Project(slug="example", roots=[self.project], privacy_roots=[protected])]
        packet = self.store.load(self.project, "Scanner-only content")
        self.assertNotIn("Scanner-only", packet.text)
        self.assertEqual(packet.source_receipts, [])

    def test_external_sources_only_reach_the_matching_project(self) -> None:
        other = self.root / "other-project"
        other.mkdir()
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text("FIRST PROJECT CANONICAL CONTEXT\n")
        second.write_text("SECOND PROJECT CANONICAL CONTEXT\n")
        self.store.settings.projects = [
            Project(slug="example", roots=[self.project], context_sources=[first]),
            Project(slug="other", roots=[other], context_sources=[second]),
        ]
        packet = self.store.load(self.project)
        self.assertIn(first.read_text(), packet.text)
        self.assertNotIn(second.read_text(), packet.text)
        self.assertEqual(packet.sources, [str(first)])
        self.assertEqual([item.path for item in packet.source_receipts], [str(first)])
        packet = self.store.load(other)
        self.assertIn(second.read_text(), packet.text)
        self.assertNotIn(first.read_text(), packet.text)
        for cwd, query in ((self.root, ""), (self.project, "context: personal")):
            packet = self.store.load(cwd, query)
            self.assertEqual(packet.scope, "personal")
            self.assertEqual(packet.source_receipts, [])
            self.assertNotIn("CANONICAL CONTEXT", packet.text)

    def test_external_sources_follow_registered_worktrees_without_nested_repo_leaks(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Example User",
                        "-c", "user.email=owner@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "--allow-empty", "-qm", "Initial"], check=True, capture_output=True)
        worktree = self.root / "worktree"
        subprocess.run(["git", "-C", str(self.project), "worktree", "add", "--detach", str(worktree)],
                       check=True, capture_output=True)
        source = self.root / "canonical.md"
        source.write_text("Registered worktree context.\n")
        common = Path(git(self.project, ["rev-parse", "--path-format=absolute", "--git-common-dir"]))
        self.store.settings.projects = [Project(slug="example", roots=[self.project],
                                               common_dirs=[common], context_sources=[source])]
        self.assertIn(source.read_text(), self.store.load(worktree).text)
        nested = self.project / "unregistered-repository"
        subprocess.run(["git", "init", "-q", str(nested)], check=True, capture_output=True)
        packet = self.store.load(nested)
        self.assertEqual(packet.scope, "personal")
        self.assertEqual(packet.source_receipts, [])

    def test_configured_sources_are_revalidated_before_reading(self) -> None:
        source = self.root / "canonical.md"
        source.write_text("Original context.\n")
        self.store.settings.projects = [Project(slug="example", roots=[self.project], context_sources=[source])]
        source.unlink()
        with self.assertRaisesRegex(ValueError, "configured context source"):
            self.store.load(self.project)
        target = self.root / "replacement.md"
        target.write_text("Must not follow replacement symlink.\n")
        source.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "configured context source"):
            self.store.load(self.project)
        source.unlink()
        source.mkdir()
        with self.assertRaisesRegex(ValueError, "configured context source"):
            self.store.load(self.project)

    def test_unreadable_or_invalid_utf8_configured_sources_fail_explicitly(self) -> None:
        source = self.root / "canonical.md"
        source.write_text("Original context.\n")
        self.store.settings.projects = [Project(slug="example", roots=[self.project], context_sources=[source])]
        original_open = os.open

        def reject_source(path: object, *args: object, **kwargs: object) -> int:
            if path == source:
                raise PermissionError("Permission denied")
            return original_open(path, *args, **kwargs)

        with patch("context_store.os.open", side_effect=reject_source):
            with self.assertRaisesRegex(ValueError, "Permission denied"):
                self.store.load(self.project)
        source.write_bytes(b"Readable prefix\n" * 3000 + b"\xff")
        with self.assertRaisesRegex(ValueError, "configured context source"):
            self.store.load(self.project)

    def test_default_source_prefix_truncation_is_visible_before_packet_cap(self) -> None:
        source = self.store.root / "workspaces/example/context.md"
        body = "Opening context.\n" * 300 + "Required final instruction.\n"
        atomic_text(source, body)
        packet = self.store.load(self.project)
        self.assertLess(len(packet.text), self.store.settings.packet_chars)
        self.assertTrue(packet.truncated)
        self.assertIn(f"[Source truncated; read full file: {source}]", packet.text)
        self.assertNotIn("Required final instruction.", packet.text)
        receipt = packet.source_receipts[0]
        self.assertEqual(receipt.path, str(source))
        self.assertEqual(receipt.sha256, hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(receipt.size_bytes, len(body.encode()))
        self.assertTrue(receipt.truncated)
        self.assertFalse(receipt.omitted)

    def test_raw_fingerprints_cover_bytes_beyond_visible_prefix(self) -> None:
        source = self.root / "canonical.md"
        content = b"A" * 65535 + "\u00e9".encode() * 40000 + b"old tail"
        source.write_bytes(content)
        self.store.settings.projects = [Project(slug="example", roots=[self.project], context_sources=[source])]
        first = self.store.load(self.project)
        receipt = first.source_receipts[0]
        self.assertEqual(receipt.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(receipt.size_bytes, len(content))
        self.assertTrue(first.truncated)
        source.write_bytes(content[:-8] + b"new tail")
        second = self.store.load(self.project)
        self.assertEqual(first.text, second.text)
        self.assertNotEqual(receipt.sha256, second.source_receipts[0].sha256)

    def test_total_packet_budget_keeps_full_omitted_source_locations(self) -> None:
        paths = [self.root / f"canonical-{index}.md" for index in range(5)]
        for index, path in enumerate(paths):
            path.write_text(f"Source {index}.\n" * 150)
        self.store.settings.packet_chars = 2000
        self.store.settings.projects = [Project(slug="example", roots=[self.project], context_sources=paths)]
        packet = self.store.load(self.project)
        self.assertLessEqual(len(packet.text), 2000)
        self.assertTrue(packet.truncated)
        self.assertTrue(packet.omitted_sources)
        self.assertEqual(len(packet.source_receipts), len(paths))
        for receipt in packet.source_receipts:
            self.assertIn(receipt.path, packet.text)
            self.assertEqual(receipt.sha256, hashlib.sha256(Path(receipt.path).read_bytes()).hexdigest())
            self.assertEqual(receipt.omitted, receipt.path in packet.omitted_sources)
            if receipt.omitted:
                self.assertTrue(receipt.truncated)
                self.assertNotIn(receipt.path, packet.sources)
        self.assertEqual(Packet.model_validate_json(packet.model_dump_json()), packet)

    def test_location_notices_that_cannot_fit_fail_without_silent_omissions(self) -> None:
        paths = [self.root / f"canonical-source-{index:02d}.md" for index in range(30)]
        for path in paths:
            path.write_text("Context.\n")
        self.store.settings.packet_chars = 2000
        self.store.settings.projects = [Project(slug="example", roots=[self.project], context_sources=paths)]
        with self.assertRaisesRegex(ValueError, "every source location"):
            self.store.load(self.project)

    def test_retrieval_excerpts_disclose_selected_lines_as_partial_sources(self) -> None:
        source = self.store.root / "workspaces/example/people.md"
        lines = [f"Unrelated entry {index}." for index in range(30)]
        lines[15] = "Orbit calibration specialist."
        atomic_text(source, "\n".join(lines))
        packet = self.store.load(self.project, "orbit calibration")
        self.assertIn("16: Orbit calibration specialist.", packet.text)
        self.assertNotIn("Unrelated entry 29.", packet.text)
        self.assertTrue(packet.truncated)
        self.assertTrue(packet.source_receipts[0].truncated)
        self.assertIn(f"[Source truncated; read full file: {source}]", packet.text)

    def test_external_sources_do_not_redirect_outbox_or_receive_writes(self) -> None:
        source = self.root / "canonical.md"
        source.write_text("Canonical context remains unchanged.\n")
        before = source.stat()
        self.store.settings.projects = [Project(slug="example", roots=[self.project], context_sources=[source])]
        self.store.load(self.project)
        self.store.remember(self.memory(kind="decision", scope="example", text="Ship the editor this week.",
                                       evidence="I decided to ship the editor this week."))
        self.assertEqual(source.read_text(), "Canonical context remains unchanged.\n")
        self.assertEqual((source.stat().st_ino, source.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        inbox = self.store.root / "workspaces/example/inbox.md"
        self.assertIn("Ship the editor this week.", inbox.read_text())
        self.assertIn(str(inbox), self.store.load(self.project).sources)


if __name__ == "__main__":
    unittest.main()
