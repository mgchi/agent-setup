from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from evaluate_context import Answer, Answers, Review, fixture, main, run_trial, score, task_text
from team_runtime import Outcome, Provider


class EvaluationTests(TestCase):
    def test_keys_are_withheld_and_choices_are_counterbalanced(self):
        first, _ = fixture(41)
        second, _ = fixture(42)
        self.assertNotIn('expected', task_text(first))
        self.assertNotIn('needs_context', task_text(first))
        self.assertNotEqual([c.expected for c in first], [c.expected for c in second])
        gold = Answers(answers=[Answer(id=c.id, choice=c.expected) for c in first])
        self.assertEqual(score(first, gold, context_available=True),
                         {'correct': 6, 'justified_abstention': 0, 'unsupported_correct_guess': 0, 'wrong': 0})
        baseline = score(first, gold, context_available=False)
        self.assertEqual(baseline['unsupported_correct_guess'], 3)

    def test_abstention_without_context_is_safe_and_not_counted_as_actionable(self):
        cases, _ = fixture(41)
        answers = Answers(answers=[Answer(id=c.id, choice=(next(k for k, v in c.choices.items()
            if v == 'Insufficient evidence') if c.needs_context else c.expected)) for c in cases])
        baseline = score(cases, answers, context_available=False)
        self.assertEqual(baseline['correct'], 3)
        self.assertEqual(baseline['justified_abstention'], 3)
        self.assertEqual(baseline['wrong'], 0)
        self.assertEqual(score(cases, answers, context_available=True)['wrong'], 3)

    def test_duplicate_missing_and_invalid_choices_fail(self):
        cases, _ = fixture(41)
        gold = [Answer(id=c.id, choice=c.expected) for c in cases]
        for values in (gold[:-1], gold + gold[:1], [Answer(id=gold[0].id, choice='Z'), *gold[1:]]):
            with self.assertRaises(ValueError):
                score(cases, Answers(answers=values), context_available=True)

    def test_live_trial_uses_only_fixture_and_counts_full_advice_time(self):
        cases, context = fixture(41)
        lead = Provider(name='claude', command=('unused',), model='example', effort='max')
        peer = lead.model_copy(update={'name': 'codex'})
        seen = []

        def fake(provider, prompt, directory, *, schema, **kwargs):
            seen.append(prompt)
            self.assertNotIn('90 days in an unrelated project', prompt)
            self.assertNotIn('"expected"', prompt)
            if 'PROJECT EVIDENCE:' in prompt:
                self.assertIn(context.strip(), prompt)
            else:
                self.assertNotIn(context.strip(), prompt)
            if schema['title'] == 'Review':
                text = Review(findings=[]).model_dump_json()
            else:
                text = Answers(answers=[Answer(id=c.id, choice=c.expected) for c in cases]).model_dump_json()
            return Outcome(ok=True, kind='success', text=text, elapsed=2, model='observed')

        with patch('evaluate_context.invoke', side_effect=fake):
            result = run_trial(lead, peer, 41, 10)
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(result['rows']), 4)
        advice = next(r for r in result['rows'] if r['condition'] == 'runtime_plus_advice')
        self.assertEqual(advice['elapsed_seconds'], 6)
        self.assertEqual(advice['additional_elapsed_seconds'], 4)

    def test_missing_or_cross_project_packet_fails_before_model_calls(self):
        provider = Provider(name='claude', command=('unused',), model='example', effort='max')
        for text in ('', 'Attachment retention is 90 days in an unrelated project.'):
            with self.subTest(text=text), patch('evaluate_context.Store.load', return_value=SimpleNamespace(text=text)), \
                    patch('evaluate_context.invoke') as invoke, self.assertRaises(ValueError):
                run_trial(provider, provider.model_copy(update={'name': 'codex'}), 41, 10)
            invoke.assert_not_called()

    def test_invalid_runtime_candidate_records_skipped_advice(self):
        provider = Provider(name='claude', command=('unused',), model='example', effort='max')
        with patch('evaluate_context.invoke', return_value=Outcome(ok=True, text='{}')) as invoke:
            result = run_trial(provider, provider.model_copy(update={'name': 'codex'}), 41, 10)
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(result['rows'][-1]['kind'], 'skipped-no-runtime-candidate')

    def test_provider_failure_is_visible_and_does_not_start_a_review(self):
        provider = Provider(name='claude', command=('unused',), model='example', effort='max')
        with patch('evaluate_context.invoke', return_value=Outcome(ok=False, kind='unavailable')) as invoke:
            result = run_trial(provider, provider.model_copy(update={'name': 'codex'}), 41, 10)
        self.assertTrue(result['incomplete'])
        self.assertEqual(invoke.call_count, 1)
        self.assertFalse(result['rows'][0]['ok'])

    def test_terminal_failures_during_review_and_revision_mark_trial_incomplete(self):
        cases, _ = fixture(41)
        provider = Provider(name='claude', command=('unused',), model='example', effort='max')
        answer = Outcome(ok=True, text=Answers(answers=[Answer(id=c.id, choice=c.expected) for c in cases]).model_dump_json())
        review = Outcome(ok=True, text=Review(findings=[]).model_dump_json())
        for phase in ('review', 'revision'):
            for kind in ('auth', 'quota', 'account-policy', 'disclosure', 'unavailable', 'exit'):
                with self.subTest(phase=phase, kind=kind):
                    outcomes = [answer] * 3 + ([review] if phase == 'revision' else []) + [Outcome(ok=False, kind=kind)]
                    with patch('evaluate_context.invoke', side_effect=outcomes) as invoke:
                        result = run_trial(provider, provider.model_copy(update={'name': 'codex'}), 41, 10)
                    self.assertTrue(result['incomplete'])
                    self.assertFalse(result['rows'][-1]['ok'])
                    self.assertEqual(invoke.call_count, len(outcomes))

    def test_incomplete_trial_prevents_later_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'providers': [
                {'name': name, 'command': ['unused'], 'model': 'example', 'effort': 'max'}
                for name in ('claude', 'codex')]}))
            result = {'seed': 41, 'rows': [{'ok': False}], 'incomplete': True}
            args = ['evaluate_context.py', '--live', '--settings', str(settings), '--output', str(root / 'report.json'), '--trials', '4']
            with patch('sys.argv', args), patch('evaluate_context.run_trial', return_value=result) as trial, redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
            self.assertEqual(trial.call_count, 2)
            self.assertEqual({call.args[2] for call in trial.call_args_list}, {41})

    def test_cli_requires_opt_in_and_bounds_before_loading_settings_or_calling_models(self):
        invalid = [['--live', '--timeout=' + value] for value in ('0', '-1', 'nan', 'inf', '-inf', '601')]
        invalid += [[], ['--live', '--trials', '5']]
        for flags in invalid:
            with self.subTest(flags=flags), patch('sys.argv', ['evaluate_context.py', '--output', 'unused.json', *flags]), \
                    patch('evaluate_context.invoke') as invoke, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                invoke.assert_not_called()

    def test_output_preflight_precedes_calls_and_worker_error_preserves_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'providers': [
                {'name': name, 'command': ['unused'], 'model': 'example', 'effort': 'max'}
                for name in ('claude', 'codex')]}))
            args = ['evaluate_context.py', '--live', '--settings', str(settings), '--output', str(root)]
            with patch('sys.argv', args), patch('evaluate_context.run_trial') as trial, self.assertRaises(OSError):
                main()
            trial.assert_not_called()
            report = root / 'report.json'
            args[-1] = str(report)

            def run(lead, peer, seed, timeout):
                self.assertTrue(report.exists())
                if lead.name == 'claude':
                    raise ValueError('Do not expose this error text')
                return {'lead': lead.name, 'seed': seed, 'rows': [{'ok': True}]}

            with patch('sys.argv', args), patch('evaluate_context.run_trial', side_effect=run) as trial, redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
            self.assertEqual(trial.call_count, 2)
            saved = json.loads(report.read_text())
            self.assertEqual(len(saved['trials']), 2)
            self.assertEqual({t['lead'] for t in saved['trials']}, {'claude', 'codex'})
            self.assertNotIn('Do not expose', report.read_text())
