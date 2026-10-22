import json
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch

import tomlkit

import install_local
from context_store import Capture, Cooldown, Memory, Settings, Store, atomic_text
from install_local import PreferenceSeed, build_plan, import_preferences, install, load_seed, managed_section


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.settings = Settings(context_root=self.home / 'context')
        atomic_text(self.home / '.config/agent-context/settings.json', self.settings.model_dump_json())
        self.claude = self.home / '.claude/settings.json'
        self.codex = self.home / '.codex/config.toml'
        self.hooks = self.home / '.codex/hooks.json'
        self.conductor = self.home / '.conductor/settings.toml'
        self.global_file = self.home / '.agents/AGENTS.md'
        self.marker = self.home / '.config/agent-context/preference-import.json'
        self.original_hook = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'existing-guard'}]}
        atomic_text(self.claude, json.dumps({'model': 'native-claude', 'effortLevel': 'high', 'hooks': {'PreToolUse': [self.original_hook], 'Stop': [{'hooks': [{'type': 'command', 'command': 'existing-stop'}]}]}}))
        atomic_text(self.codex, '# Retain this comment.\nmodel = "native-codex"\nmodel_reasoning_effort = "high"\n[features]\nexisting = true\n')
        atomic_text(self.hooks, json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'existing-codex-stop'}]}]}}))
        atomic_text(self.conductor, '[models.claude_code]\ndefault_model = "native-lead"\nreview_model = "native-review"\n[models.codex]\ndefault_thinking_level = "high"\n[prompts]\ngeneral = "Keep my existing instructions."\n')
        atomic_text(self.global_file, '# Personal rules\nKeep my existing rules.\n')
        self.prompt = 'I prefer concise responses.'
        self.seed = PreferenceSeed(prompt=self.prompt, memories=[self.memory()])
        self.seed_path = self.home / 'seed.json'
        atomic_text(self.seed_path, self.seed.model_dump_json())

    def memory(self, *, key: str = 'style', text: str = 'Use concise responses.') -> Memory:
        return Memory(key=key, kind='preference', scope='personal', text=text,
                      evidence=self.prompt, source='initial-user-preferences')

    def test_install_preserves_models_hooks_comments_and_rules(self) -> None:
        result = install(self.home, seed_path=self.seed_path)
        claude = json.loads(self.claude.read_text())
        self.assertEqual(claude['model'], 'native-claude')
        self.assertEqual(claude['effortLevel'], 'high')
        self.assertEqual(claude['hooks']['PreToolUse'][:-1], [self.original_hook])
        guard = claude['hooks']['PreToolUse'][-1]['hooks'][0]
        self.assertEqual(guard['command'], f'{self.home}/.local/bin/agent-context hook --client claude')
        self.assertEqual(guard['timeout'], 10)
        self.assertEqual(claude['hooks']['Stop'][0]['hooks'][0]['command'], 'existing-stop')
        codex = tomlkit.parse(self.codex.read_text())
        self.assertEqual(codex['model'], 'native-codex')
        self.assertEqual(codex['model_reasoning_effort'], 'high')
        self.assertTrue(codex['features']['existing'])
        self.assertTrue(codex['features']['hooks'])
        self.assertIn('# Retain this comment.', self.codex.read_text())
        conductor = tomlkit.parse(self.conductor.read_text())
        self.assertEqual(conductor['models']['claude_code']['default_model'], 'native-lead')
        self.assertEqual(conductor['models']['claude_code']['review_model'], 'native-review')
        self.assertEqual(conductor['models']['codex']['default_thinking_level'], 'high')
        self.assertIn('Keep my existing instructions.', conductor['prompts']['general'])
        self.assertIn('Keep my existing rules.', self.global_file.read_text())
        hooks = json.loads(self.hooks.read_text())['hooks']
        self.assertEqual(hooks['Stop'][0]['hooks'][0]['command'], 'existing-codex-stop')
        self.assertEqual(hooks['UserPromptSubmit'][-1]['hooks'][0]['additionalContextLimit'], 5000)
        self.assertEqual(hooks['SessionStart'][-1]['hooks'][0]['additionalContextLimit'], 5000)
        for event in ('PreToolUse', 'Stop'):
            self.assertNotIn('additionalContextLimit', hooks[event][-1]['hooks'][0])
        for handlers in claude['hooks'].values():
            for handler in handlers:
                for command in handler['hooks']:
                    self.assertNotIn('additionalContextLimit', command)
        self.assertEqual(result.imported_preferences, 1)
        self.assertEqual(os.readlink(self.home / '.agents/skills/context-workflow'), '../../.claude/skills/context-workflow')
        self.assertEqual(self.claude.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / '.context-refresh/refresh.sh').stat().st_mode & 0o777, 0o700)

    def test_reinstall_is_idempotent_and_preserves_kernels(self) -> None:
        install(self.home, seed_path=self.seed_path)
        kernel = self.home / 'context/local/personal.md'
        atomic_text(kernel, 'Updated personal identity and context.\n')
        before = {write.path: write.path.read_bytes() for write in build_plan(self.home).files}
        result = install(self.home, seed_path=self.seed_path)
        self.assertEqual(result.changed_files, [])
        self.assertEqual(result.imported_preferences, 0)
        self.assertEqual(kernel.read_text(), 'Updated personal identity and context.\n')
        self.assertEqual({filename: filename.read_bytes() for filename in before}, before)
        self.assertEqual(Store(self.settings).status(self.home).memory_count, 1)

    def test_malformed_markers_abort_before_writes(self) -> None:
        originals = [
            '<!-- agent-context-workflow:start -->\nmissing end',
            '<!-- agent-context-workflow:end -->\n<!-- agent-context-workflow:start -->',
            '<!-- agent-context-workflow:start -->a<!-- agent-context-workflow:end -->\n<!-- agent-context-workflow:start -->b<!-- agent-context-workflow:end -->',
        ]
        before = self.claude.read_bytes()
        for original in originals:
            with self.subTest(original=original):
                atomic_text(self.global_file, original)
                with self.assertRaises(ValueError):
                    install(self.home)
                self.assertEqual(self.claude.read_bytes(), before)
                self.assertEqual(self.global_file.read_text(), original)

    def test_managed_edit_preserves_surrounding_text(self) -> None:
        old = 'Before\n<!-- local:start -->\nold\n<!-- local:end -->\nAfter\n'
        self.assertEqual(managed_section(old, 'local', 'new'), 'Before\n<!-- local:start -->\nnew\n<!-- local:end -->\nAfter\n')

    def test_unexpected_file_and_skill_links_are_preserved(self) -> None:
        target = self.home / 'other-settings.json'
        atomic_text(target, '{}')
        self.claude.unlink()
        self.claude.symlink_to(target)
        with self.assertRaises(ValueError):
            install(self.home)
        self.assertEqual(target.read_text(), '{}')
        self.assertTrue(self.claude.is_symlink())
        self.claude.unlink()
        atomic_text(self.claude, '{}')
        alias = self.home / '.agents/skills/context-workflow'
        alias.parent.mkdir(parents=True)
        alias.symlink_to('unrelated-target')
        with self.assertRaises(ValueError):
            install(self.home)
        self.assertEqual(os.readlink(alias), 'unrelated-target')

    def test_partial_write_failure_restores_content_and_modes(self) -> None:
        self.claude.chmod(0o640)
        before = {filename: (filename.read_bytes(), filename.stat().st_mode & 0o777)
                  for filename in (self.claude, self.codex, self.hooks, self.conductor, self.global_file)}
        real_write = install_local.atomic_text

        def fail_on_conductor(filename: Path, text: str) -> None:
            if filename == self.conductor:
                raise OSError('Simulated unavailable configuration file')
            real_write(filename, text)

        with patch.object(install_local, 'atomic_text', side_effect=fail_on_conductor):
            with self.assertRaises(OSError):
                install(self.home, seed_path=self.seed_path)
        for filename, (content, mode) in before.items():
            self.assertEqual(filename.read_bytes(), content)
            self.assertEqual(filename.stat().st_mode & 0o777, mode)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.home / '.agents/skills/context-workflow').exists())

    def test_import_marker_failure_can_retry_without_reimporting(self) -> None:
        store = Store(self.settings)
        real_write = install_local.atomic_text

        def fail_on_marker(filename: Path, text: str) -> None:
            if filename == self.marker:
                raise OSError('Simulated marker failure')
            real_write(filename, text)

        with patch.object(install_local, 'atomic_text', side_effect=fail_on_marker):
            with self.assertRaises(OSError):
                import_preferences(store, self.marker, self.seed)
        self.assertEqual(store.status(self.home).memory_count, 1)
        with store.connection() as connection:
            connection.execute('DELETE FROM memory')
        self.assertEqual(import_preferences(store, self.marker, self.seed), 0)
        self.assertTrue(self.marker.exists())
        self.assertEqual(store.status(self.home).memory_count, 0)

    def test_failed_import_restores_configs_and_preserves_database(self) -> None:
        self.settings.max_memory = 1
        atomic_text(self.home / '.config/agent-context/settings.json', self.settings.model_dump_json())
        store = Store(self.settings)
        store.capture(Capture(run_id='newer', prompt='I prefer very detailed answers.'))
        store.remember(Memory(key='different', kind='preference', scope='personal', text='Use detailed answers.',
                              evidence='I prefer very detailed answers.', source='session:newer', run_id='newer'))
        before = self.claude.read_bytes()
        with self.assertRaisesRegex(ValueError, 'capacity'):
            install(self.home, seed_path=self.seed_path)
        self.assertEqual(self.claude.read_bytes(), before)
        self.assertEqual([memory.text for memory in store.memories('personal')], ['Use detailed answers.'])
        self.assertFalse(self.marker.exists())
        with store.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM preference_import').fetchone()[0], 0)
        self.assertFalse((self.home / '.agents/skills/context-workflow').is_symlink())

    def test_newer_preferences_and_duplicate_content_survive_import(self) -> None:
        store = Store(self.settings)
        store.capture(Capture(run_id='existing', prompt=self.prompt))
        first = self.memory(text='Keep the newer concise preference.').model_copy(update={'run_id': 'existing', 'source': 'session:existing'})
        second = self.memory(key='other', text='Use concise responses.').model_copy(update={'run_id': 'existing', 'source': 'session:existing'})
        store.remember_many([first, second])
        seed = PreferenceSeed(prompt=self.prompt, memories=[self.memory(), self.memory(key='duplicate')])
        self.assertEqual(import_preferences(store, self.marker, seed), 0)
        self.assertEqual({memory.key: memory.text for memory in store.memories('personal')}, {'style': first.text, 'other': second.text})

    def test_import_preserves_existing_cooldown(self) -> None:
        store = Store(self.settings)
        cooldown = Cooldown(identity='claude:claude-fable-5-1:max', kind='quota', detail='Current quota limit', until=time.time() + 1000)
        with store.connection() as connection:
            connection.execute('INSERT INTO cooldown VALUES (?, ?, ?)', (cooldown.identity, cooldown.until, cooldown.model_dump_json()))
        seed = self.seed.model_copy(update={'cooldowns': [cooldown.model_copy(update={'kind': 'account-policy', 'until': time.time() + 10})]})
        import_preferences(store, self.marker, seed)
        self.assertEqual(store.status(self.home).cooldowns, [cooldown])

    def test_seed_requires_unquoted_literal_preference_evidence(self) -> None:
        for prompt in ('Different words.', '> I prefer concise responses.', '<source>\nI prefer concise responses.\n</source>'):
            with self.subTest(prompt=prompt):
                atomic_text(self.seed_path, self.seed.model_copy(update={'prompt': prompt}).model_dump_json())
                with self.assertRaises(ValueError):
                    load_seed(self.seed_path)
        atomic_text(self.seed_path, self.seed.model_copy(update={'memories': [self.memory(), self.memory()]}).model_dump_json())
        with self.assertRaisesRegex(ValueError, 'duplicate keys'):
            load_seed(self.seed_path)

    def test_installed_skills_have_valid_names_and_quoted_descriptions(self) -> None:
        for name in ('context-workflow', 'project-context'):
            with self.subTest(name=name):
                skill = next(write for write in build_plan(self.home).files if write.path == self.home / f'.claude/skills/{name}/SKILL.md')
                header = skill.text.split('---', 2)[1]
                self.assertIn(f'\nname: {name}\n', header)
                description = next(line for line in header.splitlines() if line.startswith('description:'))
                self.assertTrue(description.startswith('description: "'))
                self.assertTrue(description.endswith('"'))
                self.assertNotIn('{{HOME}}', skill.text)

    def unrelated_job(self) -> Path:
        path = self.home / 'Library/LaunchAgents/local.unrelated-job.plist'
        atomic_text(path, plistlib.dumps({
            'Label': 'local.unrelated-job',
            'ProgramArguments': ['unrelated-command'],
        }).decode())
        return path

    def test_maintenance_activation_is_idempotent_and_reloads_only_changed_job(self) -> None:
        current = install_local.MAINTENANCE_LABEL
        domain = f'gui/{os.getuid()}'
        bootstrap = call('bootstrap', domain, str(self.home / f'Library/LaunchAgents/{current}.plist'))
        with patch.object(install_local, 'loaded_maintenance', return_value={current}), patch.object(
            install_local, 'maintenance_command', return_value=subprocess.CompletedProcess([], 0)
        ) as command:
            self.assertEqual(install_local.activate_maintenance(self.home), 'already loaded')
            command.assert_not_called()
            install_local.activate_maintenance(self.home, changed=True)
            self.assertEqual(command.call_args_list, [call('bootout', f'{domain}/{current}'), bootstrap])
        with patch.object(install_local, 'loaded_maintenance', return_value=set()), patch.object(
            install_local, 'maintenance_command', return_value=subprocess.CompletedProcess([], 0)
        ) as command:
            self.assertEqual(install_local.activate_maintenance(self.home), 'loaded')
            self.assertEqual(command.call_args_list, [bootstrap])

    def test_unrelated_job_is_preserved_during_installation_and_activation(self) -> None:
        unrelated = self.unrelated_job()
        before = unrelated.read_bytes()
        install(self.home)
        self.assertEqual(unrelated.read_bytes(), before)

        def activate(home: Path, *, changed: bool) -> str:
            self.assertEqual(home, self.home)
            self.assertEqual(unrelated.read_bytes(), before)
            return 'loaded'

        with patch.object(install_local, 'loaded_maintenance', return_value=set()), patch.object(
            install_local, 'activate_maintenance', side_effect=activate
        ):
            install(self.home, activate=True)
        self.assertEqual(unrelated.read_bytes(), before)

    def test_activation_failure_restores_files_before_jobs_and_keeps_preferences(self) -> None:
        unrelated = self.unrelated_job()
        unrelated_before = unrelated.read_bytes()
        self.claude.chmod(0o640)
        original = self.claude.read_bytes()
        loaded = {install_local.MAINTENANCE_LABEL}

        def restore(home: Path, previously_loaded: set[str]) -> None:
            self.assertEqual(home, self.home)
            self.assertEqual(previously_loaded, loaded)
            self.assertEqual(self.claude.read_bytes(), original)
            self.assertEqual(self.claude.stat().st_mode & 0o777, 0o640)
            self.assertEqual(unrelated.read_bytes(), unrelated_before)

        with patch.object(install_local, 'loaded_maintenance', return_value=loaded), patch.object(
            install_local, 'activate_maintenance', side_effect=RuntimeError('Activation failed')
        ), patch.object(install_local, 'restore_maintenance', side_effect=restore) as restored:
            with self.assertRaisesRegex(RuntimeError, 'Activation failed'):
                install(self.home, seed_path=self.seed_path, activate=True)
        restored.assert_called_once()
        self.assertEqual(Store(self.settings).status(self.home).memory_count, 1)

    def test_custom_unrelated_job_is_preserved(self) -> None:
        unrelated = self.unrelated_job()
        for key, value in [('Label', 'other.job'), ('ProgramArguments', ['other-command'])]:
            with self.subTest(key=key):
                self.unrelated_job()
                data = plistlib.loads(unrelated.read_bytes())
                data[key] = value
                atomic_text(unrelated, plistlib.dumps(data).decode())
                before = unrelated.read_bytes()
                with patch.object(install_local, 'loaded_maintenance', return_value=set()), patch.object(
                    install_local, 'activate_maintenance', return_value='loaded'
                ):
                    install(self.home)
                    self.assertEqual(unrelated.read_bytes(), before)
                    install(self.home, activate=True)
                self.assertEqual(unrelated.read_bytes(), before)

    def test_isolated_home_cannot_activate_jobs(self) -> None:
        with self.assertRaisesRegex(ValueError, 'current home'), patch.object(
            install_local, 'maintenance_command'
        ) as command:
            install_local.loaded_maintenance(self.home)
        command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
