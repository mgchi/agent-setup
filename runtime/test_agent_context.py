"""Behavioral checks for lead continuity and bounded automatic work."""

import concurrent.futures
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from context_store import Capture, Cooldown, LocalAdapter, Memory, Project, Settings, Store, atomic_text, digest, load_settings, lock, save_model
from agent_context import (
    CONTINUATION, Advice, CheckpointInput, HookInput, MemoryDraft, PhaseResult,
    add_project, begin, call_provider, check_advisers, checkpoint, collect, hook, list_advisers, main, parser, read_run, release,
    client_version, review, run_adapter, run_path, set_adviser, worker,
)
from disclosure import DisclosureError
from team_runtime import ADVISER_DEFAULTS, Outcome, Provider


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

    def test_team_directives_after_an_unclosed_tag_are_honored(self) -> None:
        self.assertEqual(len(read_run(self.store, self.start(turn="default")).providers), 2)
        run_id = self.start("Return Vec<String> from the parser.\nteam: off", turn="generic")
        self.assertEqual(read_run(self.store, run_id).providers, [])
        run_id = self.start("Render a <br> between rows.\nteam: codex\nteam-codex-effort: low", turn="break")
        self.assertEqual([(item.name, item.effort) for item in read_run(self.store, run_id).providers], [("codex", "low")])

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


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.script = self.root / 'canonical.py'
        self.script.write_text("import json, sys\nfrom pathlib import Path\nPath(sys.argv[2]).write_text(json.dumps(sys.argv[1:]))\nsys.exit(7)\n")
        self.adapter = LocalAdapter(project='example', script=self.script,
                                    sha256=digest(self.script.read_text()), verbs=['checkpoint'])
        self.store = Store(Settings(context_root=self.root / 'context',
                                   projects=[Project(slug='example', roots=[self.repo])],
                                   local_adapters={'canonical': self.adapter}))

    def test_registered_helper_preserves_literal_arguments_and_exit_status(self) -> None:
        receipt = self.root / 'receipt.json'
        arguments = ['checkpoint', str(receipt), '--file', 'private file with spaces', '$(literal)']
        options = parser().parse_args(['adapter', '--cwd', str(self.repo), 'canonical', *arguments])
        self.assertEqual(options.arguments, arguments)
        self.assertEqual(run_adapter(self.store, options.name, options.arguments, options.cwd), 7)
        self.assertEqual(json.loads(receipt.read_text()), arguments)

    def test_unknown_adapter_project_and_verb_never_execute(self) -> None:
        other = self.root / 'other'
        other.mkdir()
        with patch('context_store.git', return_value=''), patch('agent_context.subprocess.run', side_effect=AssertionError('Must not execute')):
            for name, arguments, cwd in [('missing', ['checkpoint'], self.repo),
                                         ('canonical', [], self.repo),
                                         ('canonical', ['publish'], self.repo),
                                         ('canonical', ['checkpoint'], other)]:
                with self.subTest(name=name, arguments=arguments, cwd=cwd), self.assertRaises(ValueError):
                    run_adapter(self.store, name, arguments, cwd)

    def test_changed_or_linked_scripts_fail_closed(self) -> None:
        self.script.write_text('raise SystemExit(0)\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            run_adapter(self.store, 'canonical', ['checkpoint'], self.repo)
        target = self.root / 'replacement.py'
        self.script.rename(target)
        self.script.symlink_to(target)
        with self.assertRaisesRegex(DisclosureError, 'linked-file'):
            run_adapter(self.store, 'canonical', ['checkpoint'], self.repo)
        with self.assertRaisesRegex(ValueError, 'without symlinks'):
            LocalAdapter(project='example', script=self.script, sha256=self.adapter.sha256, verbs=['checkpoint'])

    def test_registration_requires_a_known_project(self) -> None:
        with self.assertRaisesRegex(ValueError, 'registered project'):
            Settings(context_root=self.root / 'context', local_adapters={'canonical': self.adapter})


class AdviserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.settings_path = self.root / 'settings.json'
        save_model(self.settings_path, Settings(context_root=self.root / 'context'))
        releases = patch('agent_context.client_version', side_effect=lambda command: {'claude': '9.0.0', 'codex': '0.99.0'}[command[0]])
        releases.start()
        self.addCleanup(releases.stop)

    def test_new_settings_follow_defaults_and_runs_record_the_models_used(self) -> None:
        self.assertEqual(self.settings_path.read_text().count('"model": "toolkit-default"'), 2)
        project = self.root / 'project'
        project.mkdir()
        store = Store(Settings(context_root=self.root / 'context', projects=[Project(slug='example', roots=[project])]))
        with patch('agent_context.spawn_worker'):
            run_id = begin(store, self.settings_path, project, 'Plan the migration.\nteam-codex-effort: low').run_id
        claude, codex = ADVISER_DEFAULTS['claude'], ADVISER_DEFAULTS['codex']
        self.assertEqual({item.name: (item.model, item.effort) for item in read_run(store, run_id).providers},
                         {'claude': (claude.model, claude.effort), 'codex': (codex.model, 'low')})
        pinned = Settings(context_root=self.root / 'context', projects=[Project(slug='example', roots=[project])],
                          providers=[Provider(name='codex', command=('codex',), model='example-model', effort='high')])
        with patch('agent_context.spawn_worker'):
            run_id = begin(Store(pinned), self.settings_path, project, 'Plan it.\nteam-codex-model: toolkit-default', turn='other').run_id
        self.assertEqual([(item.model, item.effort) for item in read_run(Store(pinned), run_id).providers], [(codex.model, 'high')])

    def test_set_pins_one_adviser_and_reset_follows_defaults_again(self) -> None:
        pinned = {item.name: item for item in set_adviser(self.settings_path, 'codex', model='example-model', effort='high').advisers}
        self.assertEqual((pinned['codex'].model, pinned['codex'].effort, pinned['codex'].follows_default), ('example-model', 'high', []))
        self.assertIn('Pinned to example-model', pinned['codex'].note)
        self.assertEqual(pinned['claude'].follows_default, ['model', 'effort'])
        self.assertEqual({item.name: item.model for item in load_settings(self.settings_path).providers},
                         {'claude': 'toolkit-default', 'codex': 'example-model'})
        partly = {item.name: item for item in set_adviser(self.settings_path, 'codex', model='toolkit-default').advisers}
        self.assertEqual((partly['codex'].model, partly['codex'].effort, partly['codex'].follows_default),
                         (ADVISER_DEFAULTS['codex'].model, 'high', ['model']))
        reset = set_adviser(self.settings_path, 'all', model='toolkit-default', effort='toolkit-default')
        self.assertEqual([item.follows_default for item in reset.advisers], [['model', 'effort']] * 2)
        self.assertEqual({(item.model, item.effort) for item in load_settings(self.settings_path).providers}, {('toolkit-default', 'toolkit-default')})
        with self.assertRaises(ValueError):
            set_adviser(self.settings_path, 'codex')

    def test_list_says_when_a_client_is_too_old_for_the_default(self) -> None:
        advisers = {item.name: item for item in list_advisers(load_settings(self.settings_path)).advisers}
        self.assertEqual((advisers['claude'].client, advisers['claude'].note), ('9.0.0', ''))
        needed = ADVISER_DEFAULTS['codex'].client
        self.assertEqual((advisers['codex'].client, advisers['codex'].client_needed), ('0.99.0', needed))
        self.assertIn(f'needs client {needed} or newer', advisers['codex'].note)
        with patch('agent_context.client_version', return_value=''):
            self.assertIn('did not report its release', list_advisers(load_settings(self.settings_path)).advisers[0].note)
        self.assertLess(release('0.99.0'), release('0.166.0'))

    def test_client_release_comes_only_from_a_successful_version_report(self) -> None:
        def client(body: str) -> tuple[str, ...]:
            script = self.root / f'client-{len(list(self.root.glob("client-*")))}.py'
            script.write_text('import sys\n' + body)
            return (sys.executable, str(script))

        with patch('agent_context.client_version', client_version):
            self.assertEqual(client_version(client('print("example-cli 1.2.3-beta (build 4.5)")')), '1.2.3')
            self.assertEqual(client_version(client('print("ready")')), '')
            self.assertEqual(client_version(client('print("requires system 13.4", file=sys.stderr)\nprint("1.2.3")\nsys.exit(1)')), '')
            self.assertEqual(client_version(client('print("warning 9.9.9", file=sys.stderr)\nprint("example-cli 0.4.0")')), '0.4.0')
            self.assertEqual(client_version((str(self.root / 'missing-client'),)), '')

    def test_check_calls_each_adviser_once_and_clears_a_stale_cooldown(self) -> None:
        store = Store(load_settings(self.settings_path))
        store.set_cooldown(Cooldown(identity='claude:earlier-model:max', kind='unavailable', detail='Earlier failure', until=time.time() + 1000))
        store.set_cooldown(Cooldown(identity='codex:earlier-model:max', kind='unavailable', detail='Earlier failure', until=time.time() + 1000))
        calls: list[tuple[str, str, str]] = []

        def answer(provider: Provider, prompt: str, directory: Path, **options: object) -> Outcome:
            calls.append((provider.name, provider.model, provider.effort))
            self.assertFalse(directory.resolve().is_relative_to(store.root))
            works = provider.name == 'claude'
            return Outcome(ok=works, kind='success' if works else 'unavailable', elapsed=1.26)

        with patch('agent_context.invoke', side_effect=answer):
            checks = {item.name: item for item in check_advisers(store, 'all').checks}
            self.assertEqual([item.name for item in check_advisers(store, 'codex').checks], ['codex'])
        self.assertEqual(calls[:2], [(name, default.model, default.effort) for name, default in ADVISER_DEFAULTS.items()])
        self.assertEqual((checks['claude'].ok, checks['claude'].note, checks['claude'].seconds), (True, '', 1.3))
        self.assertFalse(checks['codex'].ok)
        self.assertIn('upgrade the client', checks['codex'].note)
        self.assertIsNone(store.cooldown('claude:earlier-model:max'))
        self.assertIsNotNone(store.cooldown('codex:earlier-model:max'))
        self.assertEqual([item.name for item in store.local.iterdir() if item.is_dir()], [])

    def test_failed_check_names_its_own_cause(self) -> None:
        set_adviser(self.settings_path, 'codex', model='example-model')
        store = Store(load_settings(self.settings_path))
        for kind, expected in (('auth', 'Sign in'), ('quota', 'usage limit'), ('unavailable', 'may lack access'), ('timeout', '')):
            with self.subTest(kind=kind), patch('agent_context.invoke', return_value=Outcome(ok=False, kind=kind)):
                note = check_advisers(store, 'codex').checks[0].note
                self.assertIn(expected, note)
                self.assertNotIn('Pinned', note)
        providers = [item for item in store.settings.providers if item.name == 'claude']
        with patch('agent_context.invoke', return_value=Outcome(ok=True, kind='success')), self.assertRaises(ValueError):
            check_advisers(Store(store.settings.model_copy(update={'providers': providers})), 'codex')

    def test_command_line_changes_and_lists_advisers(self) -> None:
        def run(*arguments: str) -> tuple[int, dict[str, object]]:
            output = io.StringIO()
            with patch('sys.argv', ['agent-context', '--settings', str(self.settings_path), 'adviser', *arguments]), \
                    contextlib.redirect_stdout(output):
                status = main()
            return status, json.loads(output.getvalue())

        status, changed = run('set', 'codex', '--model', 'example-model', '--effort', 'high')
        self.assertEqual((status, changed['advisers'][1]['model'], changed['advisers'][1]['effort']), (0, 'example-model', 'high'))
        self.assertEqual(run('list')[1], changed)
        self.assertEqual(run('reset')[1]['advisers'][1]['follows_default'], ['model', 'effort'])
        with patch('agent_context.invoke', return_value=Outcome(ok=False, kind='auth')):
            status, checked = run('test', 'claude')
        self.assertEqual((status, checked['checks'][0]['note']), (1, 'Sign in to the client again.'))
        for arguments in (['set', 'codex', '--model', 'not a model'], ['set', 'codex', '--effort', 'High'],
                          ['set', 'other', '--model', 'x'], ['set', 'all', '--model', 'x'], ['set', 'codex']):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                    patch('sys.argv', ['agent-context', '--settings', str(self.settings_path), 'adviser', *arguments]):
                main()


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

    def test_registration_can_explicitly_select_context_only_or_advice(self) -> None:
        project = add_project(self.settings_path, 'example', self.repo, advisers=False)
        self.assertFalse(project.advisers)
        self.assertFalse(add_project(self.settings_path, 'example', self.repo).advisers)
        self.assertTrue(add_project(self.settings_path, 'example', self.repo, advisers=True).advisers)

    def test_registration_default_and_new_root_preserve_adviser_policy(self) -> None:
        self.assertTrue(add_project(self.settings_path, 'example', self.repo).advisers)
        add_project(self.settings_path, 'example', self.repo, advisers=False)
        other = self.root / 'another clone'
        other.mkdir()
        self.git(other, 'init', '-q')
        project = add_project(self.settings_path, 'example', other)
        self.assertFalse(project.advisers)
        self.assertEqual(project.roots, [self.repo, other])

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
