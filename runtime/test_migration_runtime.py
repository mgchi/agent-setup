"""Synthetic regressions for migrated project context and completion checks."""

import concurrent.futures
import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel

from agent_context import (
    Advice, CheckpointInput, CheckpointResult, MemoryDraft, add_project, begin,
    checkpoint, collect, finish, main, read_run, refresh, run_path, worker,
)
from context_store import Packet, Project, Settings, Store, load_settings, save_model
from team_runtime import Outcome, Provider


class MigrationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "source project"
        self.repo.mkdir()
        self.git("init", "-q")
        self.tracked = self.repo / "tracked.txt"
        self.tracked.write_text("Public initial content.\n")
        self.git("add", "tracked.txt")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "Initialize fixture")
        self.source = self.root / "selected-context.md"
        self.selected_text = " ".join(f"selectedrecord{number:03d}" for number in range(48)) + "\n"
        self.source.write_text(self.selected_text)
        self.private_root = self.root / "private archive"
        self.private_root.mkdir()
        self.private_text = " ".join(f"scanneronlyrecord{number:03d}" for number in range(48)) + "\n"
        (self.private_root / "scan-only.txt").write_text(self.private_text)
        self.providers = [Provider(name=name, command=("unused",), model=f"fixture-{name}", effort="max")
                          for name in ("claude", "codex")]
        self.settings_path = self.root / "settings.json"
        self.store = Store(Settings(context_root=self.root / "context", providers=self.providers,
            projects=[Project(slug="example", roots=[self.repo], context_sources=[self.source],
                              privacy_roots=[self.private_root], collaboration="exchange")]))
        save_model(self.settings_path, self.store.settings)
        spawner = patch("agent_context.spawn_worker")
        self.spawner = spawner.start()
        self.addCleanup(spawner.stop)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *arguments],
                              check=True, capture_output=True, text=True).stdout.strip()

    def start(self, prompt: str = "Review the migration behavior.") -> str:
        return begin(self.store, self.settings_path, self.repo, prompt,
                     client="manual", session="fixture").run_id

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["agent-context", "--settings", str(self.settings_path), *arguments]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main()
        return code, output.getvalue(), errors.getvalue()

    def advice(self, text: str) -> Outcome:
        return Outcome(ok=True, kind="success", text=Advice(
            recommendation=text, findings=[], checks=[], uncertainty="").model_dump_json())

    def assess(self, run_id: str) -> None:
        worker(self.store, run_id, "assessment", read_run(self.store, run_id).prompt_hash)

    def test_exchange_delivers_actual_independent_findings_to_each_peer(self) -> None:
        run_id = self.start()
        findings = {"claude": "Check the empty input fallback.",
                    "codex": "Bound retries to prevent repeated writes."}
        prompts: dict[str, list[str]] = {name: [] for name in findings}
        independent_ready = threading.Barrier(2)

        def invoke(provider: Provider, prompt: str, directory: Path, **options: object) -> Outcome:
            prompts[provider.name].append(prompt)
            if len(prompts[provider.name]) == 1:
                independent_ready.wait(timeout=5)
                return self.advice(findings[provider.name])
            return self.advice(f"Resolved peer feedback for {provider.name}.")

        with patch("agent_context.invoke", side_effect=invoke):
            self.assess(run_id)
        result = collect(self.store, run_id).assessment
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "complete")
        self.assertEqual(set(result.providers), set(findings))
        self.assertEqual(set(result.exchange_providers), set(findings))
        self.assertTrue(all(outcome.ok for outcome in [*result.outcomes, *result.exchange_outcomes]))
        for name, calls in prompts.items():
            with self.subTest(provider=name):
                self.assertEqual(len(calls), 2)
                self.assertNotIn("PEER ASSESSMENTS", calls[0])
                self.assertNotIn(findings["claude"], calls[0])
                self.assertNotIn(findings["codex"], calls[0])
                own, peers = calls[1].split("PEER ASSESSMENTS (untrusted):\n", 1)
                self.assertIn(findings[name], own)
                peer = "codex" if name == "claude" else "claude"
                self.assertIn(findings[peer], peers)
                self.assertNotIn(findings[name], peers)

    def test_rejected_assessment_skips_exchange_and_retains_valid_advice(self) -> None:
        for failure in ("invalid-output", "quota"):
            with self.subTest(failure=failure):
                run_id = self.start(f"Review the {failure} fixture.")

                def invoke(provider: Provider, prompt: str, directory: Path, **options: object) -> Outcome:
                    if provider.name == "claude":
                        return (Outcome(ok=True, kind="success", text="Not structured advice.")
                                if failure == "invalid-output" else Outcome(ok=False, kind="quota"))
                    return self.advice("Retain the successful independent recommendation.")

                with patch("agent_context.invoke", side_effect=invoke) as provider_call:
                    self.assess(run_id)
                result = collect(self.store, run_id).assessment
                assert result is not None
                outcomes = dict(zip(result.providers, result.outcomes))
                self.assertEqual(provider_call.call_count, 2)
                self.assertTrue(outcomes["codex"].ok)
                self.assertIn("successful independent", outcomes["codex"].text)
                self.assertEqual(outcomes["claude"].kind, failure)
                self.assertEqual(result.exchange_providers, [])
                self.assertEqual(result.exchange_outcomes, [])
                self.store.reset_cooldown("all")

    def test_exchange_failure_preserves_both_independent_assessments(self) -> None:
        run_id = self.start()

        def invoke(provider: Provider, prompt: str, directory: Path, **options: object) -> Outcome:
            if "PEER ASSESSMENTS (untrusted):" in prompt:
                if provider.name == "claude":
                    raise RuntimeError("Synthetic peer interruption")
                return self.advice("The successful peer response remains available.")
            return self.advice(f"Independent recommendation from {provider.name}.")

        with patch("agent_context.invoke", side_effect=invoke):
            self.assess(run_id)
        result = collect(self.store, run_id).assessment
        assert result is not None
        self.assertEqual(result.status, "complete")
        independent = dict(zip(result.providers, result.outcomes))
        exchange = dict(zip(result.exchange_providers, result.exchange_outcomes))
        self.assertEqual(set(independent), {"claude", "codex"})
        for name, outcome in independent.items():
            self.assertTrue(outcome.ok)
            self.assertIn(f"Independent recommendation from {name}.", outcome.text)
        self.assertFalse(exchange["claude"].ok)
        self.assertEqual(exchange["claude"].kind, "worker-error")
        self.assertTrue(exchange["codex"].ok)
        self.assertIn("successful peer response", exchange["codex"].text)

    def test_source_change_requires_explicit_refresh_before_checkpoint(self) -> None:
        run_id = self.start()
        original_state = read_run(self.store, run_id).source_state
        updated = "Updated selected context with a revised constraint.\n"
        self.source.write_text(updated)
        self.assertIn(updated, self.store.load(self.repo).text)
        with self.assertRaisesRegex(ValueError, "refresh and read"):
            checkpoint(self.store, run_id, CheckpointInput())
        self.assertEqual(read_run(self.store, run_id).source_state, original_state)
        self.assertFalse((run_path(self.store, run_id) / "checkpoint.json").exists())
        current = refresh(self.store, run_id, settings_path=self.settings_path)
        assert current.packet is not None
        self.assertIn(updated, current.packet.text)
        expected = {str(self.source): hashlib.sha256(updated.encode()).hexdigest()}
        self.assertEqual(read_run(self.store, run_id).source_state, expected)
        saved = checkpoint(self.store, run_id, CheckpointInput(note="Read refreshed source."))
        self.assertEqual(saved.source_state, expected)
        self.assertTrue(finish(self.store, run_id)["done"])

    def test_personal_refresh_inside_registered_repo_keeps_project_context_out(self) -> None:
        personal_text = "Synthetic personal guidance for concise answers.\n"
        (self.store.local / "personal.md").write_text(personal_text)
        project_context = self.store.root / "workspaces/example/context.md"
        project_context.parent.mkdir(parents=True)
        project_text = "Synthetic project guidance for database changes.\n"
        project_context.write_text(project_text)
        started = begin(self.store, self.settings_path, self.repo,
                        "context: personal\nReview my personal preferences.")
        refreshed = refresh(self.store, started.run_id, settings_path=self.settings_path)
        for receipt in (started, refreshed):
            assert receipt.packet is not None
            self.assertEqual(receipt.packet.scope, "personal")
            self.assertIn(personal_text, receipt.packet.text)
            for excluded in (project_text, self.selected_text, str(project_context), str(self.source)):
                self.assertNotIn(excluded, receipt.packet.text)
            self.assertNotIn(str(self.source), receipt.packet.sources)
            self.assertNotIn(str(project_context), receipt.packet.sources)
        run = read_run(self.store, started.run_id)
        self.assertEqual(run.scope, "personal")
        self.assertEqual(run.source_state, {})
        self.source.write_text("Changed project guidance outside the personal scope.\n")
        checkpoint(self.store, started.run_id, CheckpointInput())
        self.assertTrue(finish(self.store, started.run_id)["done"])

    def test_begin_and_refresh_pin_the_returned_packet_when_source_changes_during_load(self) -> None:
        for operation in ("begin", "refresh"):
            with self.subTest(operation=operation):
                before = f"Selected source before {operation} returns.\n"
                after = f"Selected source changed while {operation} was loading.\n"
                self.source.write_text(before)
                run_id = self.start("Prepare the refresh race fixture.") if operation == "refresh" else ""
                original_load = self.store.load
                changed = False

                def load_then_change(cwd: Path, query: str = "") -> Packet:
                    nonlocal changed
                    packet = original_load(cwd, query)
                    if not changed:
                        self.source.write_text(after)
                        changed = True
                    return packet

                with patch.object(self.store, "load", side_effect=load_then_change):
                    receipt = (begin(self.store, self.settings_path, self.repo, "Prepare the begin race fixture.")
                               if operation == "begin"
                               else refresh(self.store, run_id, settings_path=self.settings_path))
                assert receipt.packet is not None
                expected = {str(self.source): hashlib.sha256(before.encode()).hexdigest()}
                received = {source.path: source.sha256 for source in receipt.packet.source_receipts
                            if source.path == str(self.source)}
                self.assertEqual(received, expected)
                self.assertEqual(read_run(self.store, receipt.run_id).source_state, received)
                self.assertIn(before, receipt.packet.text)
                self.assertNotIn(after, receipt.packet.text)
                self.assertEqual(self.source.read_text(), after)
                with self.assertRaisesRegex(ValueError, "refresh and read"):
                    checkpoint(self.store, receipt.run_id, CheckpointInput())
                current = refresh(self.store, receipt.run_id, settings_path=self.settings_path)
                assert current.packet is not None
                self.assertIn(after, current.packet.text)
                checkpoint(self.store, receipt.run_id, CheckpointInput())
                self.assertTrue(finish(self.store, receipt.run_id)["done"])

    def test_finish_rejects_changed_source_tracked_and_untracked_contents(self) -> None:
        untracked = self.repo / "untracked.txt"
        untracked.write_text("Initial untracked content.\n")
        for name, path in (("source", self.source), ("tracked", self.tracked), ("untracked", untracked)):
            with self.subTest(changed=name):
                before = path.read_text()
                run_id = self.start(f"Check the {name} completion fixture.")
                checkpoint(self.store, run_id, CheckpointInput())
                self.assertTrue(finish(self.store, run_id)["done"])
                path.write_text(before + "Changed after the checkpoint.\n")
                with self.assertRaisesRegex(ValueError, "changed after checkpoint"):
                    finish(self.store, run_id)
                path.write_text(before)
                self.assertTrue(finish(self.store, run_id)["done"])

    def test_finish_rejects_index_change_when_working_contents_match_head(self) -> None:
        run_id = self.start()
        checkpoint(self.store, run_id, CheckpointInput())
        original = self.tracked.read_text()
        self.tracked.write_text("Changed content staged for a later commit.\n")
        self.git("add", "tracked.txt")
        self.tracked.write_text(original)
        self.assertEqual(self.git("status", "--porcelain", "--", "tracked.txt"), "MM tracked.txt")
        self.assertEqual(self.git("diff", "HEAD", "--", "tracked.txt"), "")
        with self.assertRaisesRegex(ValueError, "changed after checkpoint"):
            finish(self.store, run_id)
        self.git("restore", "--staged", "tracked.txt")
        self.assertTrue(finish(self.store, run_id)["done"])

    def test_identical_checkpoint_is_idempotent_but_changed_payload_conflicts(self) -> None:
        evidence = "I decided to use the compact format."
        run_id = self.start(evidence)
        memory = MemoryDraft(key="format", kind="decision", scope="example",
                             text="Use the compact format.", evidence=evidence)
        request = CheckpointInput(memories=[memory], note="Accepted output format.")
        first = checkpoint(self.store, run_id, request)
        self.assertEqual(checkpoint(self.store, run_id, request), first)
        changed = request.model_copy(update={"memories": [memory.model_copy(
            update={"text": "Use the compact format for every report."})]})
        with self.assertRaisesRegex(ValueError, "different checkpoint"):
            checkpoint(self.store, run_id, changed)
        retained = self.store.memories("example")
        self.assertEqual([(item.key, item.text) for item in retained], [(memory.key, memory.text)])
        self.assertEqual(checkpoint(self.store, run_id, request), first)

    def test_failed_final_checkpoint_write_only_allows_identical_retry(self) -> None:
        evidence = "I decided to use the compact format."
        changed_evidence = "I decided to use the expanded format."
        run_id = self.start(evidence + "\n" + changed_evidence)
        request = CheckpointInput(note="Accepted compact format.", memories=[MemoryDraft(
            key="format", kind="decision", scope="example", text="Use the compact format.", evidence=evidence)])
        changed = CheckpointInput(note="Accepted expanded format.", memories=[MemoryDraft(
            key="format", kind="decision", scope="example", text="Use the expanded format.", evidence=changed_evidence)])
        checkpoint_path = run_path(self.store, run_id) / "checkpoint.json"
        attempted: list[CheckpointResult] = []

        def fail_final_save(path: Path, value: BaseModel) -> None:
            if path == checkpoint_path:
                attempted.append(CheckpointResult.model_validate(value.model_dump()))
                raise OSError("Synthetic final receipt write failure")
            save_model(path, value)

        with patch("agent_context.save_model", side_effect=fail_final_save):
            with self.assertRaisesRegex(OSError, "final receipt write failure"):
                checkpoint(self.store, run_id, request)
        self.assertFalse(checkpoint_path.exists())
        self.assertEqual(len(attempted), 1)
        committed = self.store.memories("example")
        self.assertEqual([(item.key, item.text) for item in committed], [("format", "Use the compact format.")])
        inbox = self.store.root / "workspaces/example/inbox.md"
        published = inbox.read_text()
        self.assertEqual(published.count("Use the compact format."), 1)
        with self.assertRaisesRegex(ValueError, "different checkpoint|checkpoint request"):
            checkpoint(self.store, run_id, changed)
        self.assertEqual(self.store.memories("example"), committed)
        recovered = checkpoint(self.store, run_id, request)
        self.assertEqual(recovered, attempted[0])
        self.assertEqual(CheckpointResult.model_validate_json(checkpoint_path.read_text()), recovered)
        self.assertEqual(self.store.memories("example"), committed)
        self.assertEqual(inbox.read_text(), published)
        self.assertTrue(finish(self.store, run_id)["done"])

    def test_concurrent_different_checkpoints_keep_one_complete_result(self) -> None:
        decisions = {"compact": "I decided to use the compact format.",
                     "expanded": "I decided to use the expanded format."}
        run_id = self.start("\n".join(decisions.values()))
        requests = [CheckpointInput(note=name, memories=[MemoryDraft(key="format", kind="decision",
                    scope="example", text=evidence, evidence=evidence)]) for name, evidence in decisions.items()]
        ready = threading.Barrier(2)

        def save(request: CheckpointInput) -> CheckpointResult:
            ready.wait(timeout=5)
            return checkpoint(self.store, run_id, request)

        successes: list[CheckpointResult] = []
        failures: list[ValueError] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(save, request) for request in requests]
            for call in calls:
                try:
                    successes.append(call.result(timeout=10))
                except ValueError as error:
                    failures.append(error)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("different checkpoint", str(failures[0]))
        saved = CheckpointResult.model_validate_json((run_path(self.store, run_id) / "checkpoint.json").read_text())
        self.assertEqual(saved, successes[0])
        self.assertEqual([(item.key, item.text) for item in self.store.memories("example")],
                         [("format", decisions[saved.note])])
        self.assertTrue(finish(self.store, run_id)["done"])

    def test_cli_requires_explicit_none_and_records_no_memory_change(self) -> None:
        run_id = self.start()
        with self.assertRaises(SystemExit) as missing:
            self.cli("checkpoint", "--run", run_id)
        self.assertEqual(missing.exception.code, 2)
        self.assertFalse((run_path(self.store, run_id) / "checkpoint.json").exists())
        code, output, errors = self.cli("checkpoint", "--run", run_id, "--none")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["memory_keys"], [])
        self.assertEqual(self.store.memories("example"), [])
        self.assertTrue(finish(self.store, run_id)["done"])

    def test_standalone_privacy_blocks_external_copies_and_accepts_public_changes(self) -> None:
        outgoing = self.repo / "outgoing.txt"
        outgoing.write_text("Ordinary public implementation details.\n")
        self.git("add", "outgoing.txt")
        code, output, errors = self.cli("privacy", "--cwd", str(self.repo))
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("passed", output)
        for name, text in (("selected source", self.selected_text), ("scanner-only source", self.private_text)):
            with self.subTest(source=name):
                outgoing.write_text("Public heading.\n" + text)
                self.git("add", "outgoing.txt")
                code, output, errors = self.cli("privacy", "--cwd", str(self.repo))
                self.assertEqual(code, 1)
                self.assertRegex(errors, "private-context|private-file")
                self.assertNotIn(text.strip(), output + errors)
        outgoing.write_text("A different public implementation detail.\n")
        self.git("add", "outgoing.txt")
        self.assertEqual(self.cli("privacy", "--cwd", str(self.repo))[0], 0)

    def test_scanner_only_context_never_enters_selected_or_provider_packets(self) -> None:
        receipt = begin(self.store, self.settings_path, self.repo, "Review the public migration contract.")
        assert receipt.packet is not None
        self.assertIn(self.selected_text, receipt.packet.text)
        self.assertNotIn(self.private_text.strip(), receipt.packet.text)
        self.assertFalse(any(str(self.private_root) in path for path in receipt.packet.sources))
        prompts: list[str] = []

        def invoke(provider: Provider, prompt: str, directory: Path, **options: object) -> Outcome:
            prompts.append(prompt)
            return self.advice("Verify the public migration contract.")

        with patch("agent_context.invoke", side_effect=invoke):
            self.assess(receipt.run_id)
        self.assertEqual(len(prompts), 4)
        for prompt in prompts:
            self.assertNotIn(self.private_text.strip(), prompt)
            self.assertNotIn(str(self.private_root), prompt)

    def test_reregistering_preserves_sources_privacy_roots_and_collaboration(self) -> None:
        project = self.store.settings.projects[0]
        project.advisers = False
        save_model(self.settings_path, self.store.settings)
        add_project(self.settings_path, "example", self.repo)
        context = self.store.root / "workspaces/example/context.md"
        context.write_text("Existing curated synthetic context.\n")
        again = add_project(self.settings_path, "example", self.repo)
        saved = load_settings(self.settings_path)
        self.assertEqual(saved.projects, [again])
        self.assertEqual(again.context_sources, [self.source])
        self.assertEqual(again.privacy_roots, [self.private_root])
        self.assertEqual(again.collaboration, "exchange")
        self.assertFalse(again.advisers)
        self.assertEqual(again.roots, [self.repo])
        self.assertEqual(context.read_text(), "Existing curated synthetic context.\n")


if __name__ == "__main__":
    unittest.main()
