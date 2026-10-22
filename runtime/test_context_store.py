"""Behavioral checks for private memory, retrieval, and retention."""

import concurrent.futures
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from context_store import Capture, Memory, Project, Settings, Store, atomic_text, direct_user_text, lock


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


if __name__ == "__main__":
    unittest.main()
