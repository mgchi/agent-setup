"""Behavioral checks for lead continuity and bounded automatic work."""

import concurrent.futures
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from context_store import Capture, Memory, Project, Settings, Store, atomic_text, digest, load_settings, lock, save_model
from agent_context import (
    CONTINUATION, Advice, CheckpointInput, HookInput, MemoryDraft, PhaseResult,
    add_project, begin, call_provider, checkpoint, collect, hook, read_run, review, run_path, worker,
)
from disclosure import DisclosureError
from team_runtime import Outcome, Provider


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.settings_path = self.root / "settings.json"
        self.providers = [Provider(name=name, command=("unused",), model=name, effort="max")
                          for name in ("claude", "codex")]
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.store = Store(Settings(context_root=self.root / "context", providers=self.providers, max_runs=1,
                                   projects=[Project(slug="example", roots=[self.cwd])]))
        self.spawn = patch("agent_context.spawn_worker")
        self.spawner = self.spawn.start()
        self.addCleanup(self.spawn.stop)

    def start(self, prompt: str = "Plan the database migration.", turn: str = "") -> str:
        return begin(self.store, self.settings_path, self.cwd, prompt,
                     client="codex", session="session", turn=turn).run_id

    def event(self, name: str, **values: object) -> HookInput:
        return HookInput.model_validate({"session_id": "session", "cwd": str(self.cwd),
                                        "hook_event_name": name, **values})

    def test_one_failed_adviser_preserves_successful_advice(self) -> None:
        run_id = self.start()
        signature = read_run(self.store, run_id).prompt_hash
        advice = Advice(recommendation="Check the migration rollback.", findings=[], checks=[], uncertainty="")

        def invoke_provider(store: Store, provider: Provider, prompt: str, directory: Path) -> Outcome:
            if provider.name == "claude":
                raise RuntimeError("quota exhausted")
            return Outcome(ok=True, kind="success", text=advice.model_dump_json())

        with patch("agent_context.call_provider", side_effect=invoke_provider):
            worker(self.store, run_id, "assessment", signature)
        result = collect(self.store, run_id)
        self.assertIsNotNone(result.assessment)
        assert result.assessment is not None
        self.assertEqual(result.assessment.status, "complete")
        self.assertEqual(sum(item.ok for item in result.assessment.outcomes), 1)
        self.assertTrue(any("migration rollback" in item.text for item in result.assessment.outcomes))
        self.assertTrue(any(item.kind == "worker-error" for item in result.assessment.outcomes))

    def test_blocked_assessment_needs_an_explicit_sanitized_review_request(self) -> None:
        run_id = self.start("Public " * 5000 + "sk-" + "a" * 30)
        self.spawner.assert_not_called()
        result = collect(self.store, run_id)
        assert result.assessment is not None
        self.assertTrue(any(item.kind == "disclosure" for item in result.assessment.outcomes))
        with self.assertRaisesRegex(DisclosureError, "sanitized-request-required"):
            review(self.store, self.settings_path, run_id, "Candidate")
        review(self.store, self.settings_path, run_id, "Candidate", request="Review safe behavior.")
        self.spawner.assert_called_once()

    def test_review_scans_and_signs_complete_inputs(self) -> None:
        run_id = self.start()
        first = review(self.store, self.settings_path, run_id, "safe " * 3000 + "first")
        second = review(self.store, self.settings_path, run_id, "safe " * 3000 + "second")
        assert first.review is not None and second.review is not None
        self.assertNotEqual(first.review.signature, second.review.signature)
        for target in ("answer", "request", "evidence"):
            secret = "public " * 5000 + "sk-" + "b" * 30
            evidence = self.store.local / "evidence.txt"
            evidence.write_text(secret if target == "evidence" else "Public evidence")
            with self.subTest(target=target), self.assertRaisesRegex(DisclosureError, "provider-token"):
                review(self.store, self.settings_path, run_id,
                       secret if target == "answer" else "Candidate",
                       request=secret if target == "request" else "Request", evidence=[evidence])

    def test_provider_raw_logs_are_removed_on_success_and_exception(self) -> None:
        for fail in (False, True):
            directory = self.store.local / f"provider-test-{fail}"
            attempt = directory / "attempt-1"
            attempt.mkdir(parents=True)
            for name in ("stdout.jsonl", "stderr.log"):
                (attempt / name).write_text("Synthetic provider output")
            advice = Advice(recommendation="Check behavior", findings=[], checks=[], uncertainty="")
            with self.subTest(fail=fail), patch("agent_context.invoke") as invoke:
                if fail:
                    invoke.side_effect = RuntimeError("Synthetic interruption")
                    with self.assertRaises(RuntimeError):
                        call_provider(self.store, self.providers[0], "Safe request", directory)
                else:
                    invoke.return_value = Outcome(ok=True, kind="success", text=advice.model_dump_json())
                    self.assertTrue(call_provider(self.store, self.providers[0], "Safe request", directory).ok)
            for name in ("stdout.jsonl", "stderr.log"):
                self.assertFalse((attempt / name).exists())

    def test_malformed_expired_and_abandoned_workers_terminate(self) -> None:
        for failure in ("malformed", "expired", "abandoned"):
            with self.subTest(failure=failure):
                run_id = self.start(turn=failure)
                run = read_run(self.store, run_id)
                path = run_path(self.store, run_id) / "assessment.json"
                if failure == "malformed":
                    atomic_text(path, "{broken")
                else:
                    result = PhaseResult(run_id=run_id, phase="assessment", signature=run.prompt_hash,
                        status="pending", created=time.time() - 60, started=time.time() - 30,
                        deadline=time.time() - 1 if failure == "expired" else time.time() + 60,
                        outcomes=[Outcome(ok=True, kind="success", text="Completed advice")])
                    save_model(path, result)
                receipt = collect(self.store, run_id)
                assert receipt.assessment is not None
                self.assertEqual(receipt.assessment.status, "complete")
                self.assertTrue(any(not item.ok for item in receipt.assessment.outcomes))
                if failure != "malformed":
                    self.assertTrue(any(item.text == "Completed advice" for item in receipt.assessment.outcomes))

    def test_concurrent_reviews_spawn_only_one_worker(self) -> None:
        run_id = self.start()
        self.spawner.reset_mock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            calls = [pool.submit(review, self.store, self.settings_path, run_id, "Candidate") for _ in range(4)]
            receipts = [call.result() for call in calls]
        self.assertEqual(self.spawner.call_count, 1)
        self.assertEqual(len({value.review.signature for value in receipts if value.review}), 1)

    def test_cleanup_cannot_remove_active_work(self) -> None:
        run_id = self.start()
        directory = run_path(self.store, run_id)
        state = PhaseResult(run_id=run_id, phase="assessment", signature=read_run(self.store, run_id).prompt_hash,
                            status="complete")
        save_model(directory / "assessment.json", state)
        with lock(directory / "active.lock", shared=True):
            os.utime(directory, (1, 1))
            self.store.prune()
            self.assertTrue(directory.exists())
        os.utime(directory, (1, 1))
        self.store.prune()
        self.assertFalse(directory.exists())

    def test_repeated_delivery_deduplicates_but_new_turn_repeats(self) -> None:
        first = self.start(turn="turn-1")
        self.assertEqual(first, self.start(turn="turn-1"))
        second = self.start(turn="turn-2")
        self.assertNotEqual(first, second)
        checkpoint(self.store, second, CheckpointInput())
        self.assertEqual(second, self.start(turn="turn-2"))
        self.assertNotEqual(second, self.start())

    def test_stop_continues_once_and_synthetic_reminder_is_not_evidence(self) -> None:
        run_id = self.start()
        self.spawner.reset_mock()
        response = hook(self.store, self.settings_path, self.event("Stop", last_assistant_message="Candidate"), "codex")
        self.assertEqual(json.loads(response)["decision"], "block")
        self.assertEqual(hook(self.store, self.settings_path, self.event("Stop"), "codex"), "")
        reminder = self.event("UserPromptSubmit", prompt=CONTINUATION + " I prefer lengthy output.")
        hook(self.store, self.settings_path, reminder, "codex")
        captured = self.store.captured(run_id)
        assert captured is not None
        self.assertNotIn("I prefer lengthy", captured.prompt)
        self.assertEqual(self.store.status(self.cwd).capture_count, 1)
        self.spawner.assert_not_called()

    def test_checkpointed_run_stops_without_a_review(self) -> None:
        run_id = self.start()
        self.assertTrue(read_run(self.store, run_id).providers)
        checkpoint(self.store, run_id, CheckpointInput())
        self.assertEqual(hook(self.store, self.settings_path, self.event("Stop", last_assistant_message="Answer"), "codex"), "")

    def test_topic_changes_and_new_records_invalidate_checkpoint(self) -> None:
        topics = self.root / "topics"
        topics.mkdir()
        self.store.settings.projects[0].topic_dirs = [topics]
        record = topics / "design.md"
        record.write_text("Current design decision.\n")
        run_id = self.start()
        record.write_text("Context-Status: historical\nRetired design decision.\n")
        with self.assertRaisesRegex(ValueError, "sources changed"):
            checkpoint(self.store, run_id, CheckpointInput())
        next_run = self.start(turn="new-record")
        (topics / "operations.md").write_text("Current operations decision.\n")
        with self.assertRaisesRegex(ValueError, "sources changed"):
            checkpoint(self.store, next_run, CheckpointInput())

    def test_stop_reminder_asks_for_the_complete_answer_again(self) -> None:
        self.start()
        response = hook(self.store, self.settings_path, self.event("Stop", last_assistant_message="Answer"), "codex")
        self.assertIn("restate the complete final answer", json.loads(response)["reason"])

    def test_nullable_last_message_and_hook_recursion_guards(self) -> None:
        self.start()
        response = hook(self.store, self.settings_path, self.event("Stop", last_assistant_message=None), "codex")
        self.assertEqual(json.loads(response)["decision"], "block")
        for variable in ("AGENT_CONTEXT_WORKER", "AGENT_CONTEXT_REFRESH"):
            with patch.dict(os.environ, {variable: "1"}):
                self.assertEqual(hook(self.store, self.settings_path, self.event("SessionStart"), "codex"), "")

    def test_accepted_personal_decision_gets_a_checkpoint_reminder(self) -> None:
        self.cwd = self.root
        run_id = self.start("I decided to use mornings for writing.")
        self.assertFalse(read_run(self.store, run_id).providers)
        response = hook(self.store, self.settings_path, self.event("Stop"), "codex")
        self.assertEqual(json.loads(response)["decision"], "block")
        checkpoint(self.store, run_id, CheckpointInput(memories=[MemoryDraft(key="writing-time", kind="decision",
            scope="personal", text="Use mornings for writing.", evidence="I decided to use mornings for writing.")]))
        self.assertEqual(self.store.memories("personal")[0].key, "writing-time")
        self.assertFalse((self.store.root / "inbox.md").exists())

    def test_provider_free_personal_work_finishes_without_continuation(self) -> None:
        self.cwd = self.root
        run_id = self.start("Explain this short paragraph.")
        response = hook(self.store, self.settings_path, self.event("Stop"), "codex")
        self.assertEqual(response, "")
        self.assertTrue((run_path(self.store, run_id) / "checkpoint.json").exists())

    def test_invalid_checkpoint_saves_no_partial_memories(self) -> None:
        run_id = self.start("I prefer concise output.\nRun the migration.")
        request = CheckpointInput(memories=[
            MemoryDraft(key="style", kind="preference", scope="personal", text="Keep answers concise.", evidence="I prefer concise output."),
            MemoryDraft(key="choice", kind="decision", scope="example", text="Run the migration.", evidence="Run the migration."),
        ])
        with self.assertRaises(ValueError):
            checkpoint(self.store, run_id, request)
        self.assertEqual(self.store.memories("example"), [])

    def test_capacity_failure_rolls_back_the_whole_checkpoint(self) -> None:
        self.store.settings.max_memory = 1
        run_id = self.start("I prefer concise output.\nAlways use plain language.")
        existing = Memory(key="style", kind="preference", scope="personal", text="Keep answers concise.",
                          evidence="I prefer concise output.", source=f"session:{run_id}", run_id=run_id)
        self.store.remember(existing)
        request = CheckpointInput(memories=[
            MemoryDraft(key="style", kind="preference", scope="personal", text="Keep responses brief.", evidence="I prefer concise output."),
            MemoryDraft(key="language", kind="preference", scope="personal", text="Use plain language.", evidence="Always use plain language."),
        ])
        with self.assertRaisesRegex(ValueError, "capacity"):
            checkpoint(self.store, run_id, request)
        self.assertEqual([(item.key, item.text) for item in self.store.memories("example")], [(existing.key, existing.text)])
        self.assertFalse((run_path(self.store, run_id) / "checkpoint.json").exists())

    def test_new_batch_entries_cannot_evict_each_other(self) -> None:
        self.store.settings.max_memory = 1
        run_id = self.start("I decided to ship the editor this week.\nI decided to retain the old URLs.")
        request = CheckpointInput(memories=[
            MemoryDraft(key="editor", kind="decision", scope="example", text="Ship the editor this week.", evidence="I decided to ship the editor this week."),
            MemoryDraft(key="urls", kind="decision", scope="example", text="Retain the old URLs.", evidence="I decided to retain the old URLs."),
        ])
        with self.assertRaisesRegex(ValueError, "capacity"):
            checkpoint(self.store, run_id, request)
        self.assertEqual(self.store.memories("example"), [])
        self.assertEqual(self.store.status(self.cwd).pending_inbox, 0)
        self.assertFalse((self.store.root / "workspaces/example/inbox.md").exists())


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / 'source project'
        self.repo.mkdir()
        self.git(self.repo, 'init', '-q')
        self.git(self.repo, '-c', 'user.name=Test User', '-c', 'user.email=test@example.invalid',
                 'commit', '--allow-empty', '-qm', 'Initialize test')
        self.settings_path = self.root / 'settings.json'
        save_model(self.settings_path, Settings(context_root=self.root / 'context'))

    def git(self, directory: Path, *args: str) -> str:
        return subprocess.run(['git', '-C', str(directory), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_worktrees_share_registration_and_personal_override_wins(self) -> None:
        nested = self.repo / 'src'
        nested.mkdir()
        project = add_project(self.settings_path, 'example', nested)
        self.assertEqual(project.roots, [self.repo])
        worktree = self.root / 'other worktree'
        self.git(self.repo, 'worktree', 'add', '-q', '-b', 'test-worktree', str(worktree))
        store = Store(load_settings(self.settings_path))
        self.assertEqual(store.scope(worktree), 'example')
        self.assertEqual(store.scope(worktree, 'context: personal'), 'personal')
        self.assertEqual(store.scope(store.root / 'workspaces/example'), 'example')
        for name in ('context', 'decisions', 'playbook', 'inbox'):
            self.assertTrue((store.root / f'workspaces/example/{name}.md').is_file())

    def test_registration_keeps_adviser_choice_and_existing_context(self) -> None:
        save_model(self.settings_path, Settings(context_root=self.root / 'context',
                   projects=[Project(slug='example', roots=[self.repo], advisers=False)]))
        project = add_project(self.settings_path, 'example', self.repo)
        context = self.root / 'context/workspaces/example/context.md'
        atomic_text(context, 'Keep existing project knowledge.\n')
        again = add_project(self.settings_path, 'example', self.repo)
        self.assertFalse(again.advisers)
        self.assertEqual(project, again)
        self.assertEqual(context.read_text(), 'Keep existing project knowledge.\n')
        self.assertEqual(len(load_settings(self.settings_path).projects), 1)

    def test_registration_keeps_an_adopted_context_dir(self) -> None:
        context_root = self.root / 'context'
        context_root.mkdir(exist_ok=True)
        save_model(self.settings_path, Settings(context_root=context_root,
                   projects=[Project(slug='example', roots=[self.repo], context_dir=context_root)]))
        project = add_project(self.settings_path, 'example', self.repo)
        self.assertEqual(project.context_dir, context_root)
        self.assertTrue((context_root / 'inbox.md').exists())
        self.assertFalse((context_root / 'workspaces').exists())

    def test_nested_and_unregistered_repositories_stay_isolated(self) -> None:
        add_project(self.settings_path, 'example', self.repo)
        nested = self.repo / 'unrelated'
        nested.mkdir()
        self.git(nested, 'init', '-q')
        store = Store(load_settings(self.settings_path))
        self.assertEqual(store.scope(nested, 'Work on example'), 'personal')
        other = self.root / 'other'
        other.mkdir()
        self.git(other, 'init', '-q')
        self.assertEqual(store.scope(other, 'Work on example'), 'personal')

    def test_invalid_paths_and_context_links_preserve_settings(self) -> None:
        alias = self.root / 'alias'
        alias.symlink_to(self.repo)
        before = self.settings_path.read_bytes()
        for path in (alias, self.root / 'missing'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                add_project(self.settings_path, 'example', path)
        target = self.root / 'context/workspaces/example/context.md'
        target.parent.mkdir(parents=True)
        outside = self.root / 'outside.md'
        atomic_text(outside, 'Preserve this file.\n')
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            add_project(self.settings_path, 'example', self.repo)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertEqual(outside.read_text(), 'Preserve this file.\n')

    def test_duplicate_project_mapping_is_rejected_before_context_writes(self) -> None:
        add_project(self.settings_path, 'example', self.repo)
        before = self.settings_path.read_bytes()
        with self.assertRaises(ValueError):
            add_project(self.settings_path, 'another', self.repo)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertFalse((self.root / 'context/workspaces/another').exists())


if __name__ == "__main__":
    unittest.main()
