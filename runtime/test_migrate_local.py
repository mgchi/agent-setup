import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

import migrate_local
from context_store import atomic_text
from migrate_local import apply, plan, restore


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.hooks = self.home / '.client/hooks.json'
        self.instructions = self.home / '.agents/AGENTS.md'
        self.toml = self.home / '.editor/settings.toml'
        self.manifest = self.home / 'migration.json'
        self.backups = self.home / 'private-backups'
        self.command = 'obsolete-helper hook --client sample'
        self.section = '<!-- old-context:start -->\nOld generated rules.\n<!-- old-context:end -->\n'
        self.other_hook = {'type': 'command', 'command': 'personal-guard', 'timeout': 3}
        self.data = {'model': 'user-choice', 'custom': {'keep': True}, 'hooks': {
            'SessionStart': [{'matcher': '*', 'extra': 'keep', 'hooks': [
                {'type': 'command', 'command': self.command}, self.other_hook,
                {'type': 'command', 'command': self.command + ' --extra'},
                {'type': 'prompt', 'command': self.command, 'prompt': 'Keep this prompt.'},
            ]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': self.command}]}],
            'Other': [{'matcher': 'custom', 'hooks': [self.other_hook]}],
        }}
        atomic_text(self.hooks, json.dumps(self.data) + '\n')
        atomic_text(self.instructions, '# Personal rules\n' + self.section + 'Keep these rules.\n')
        atomic_text(self.toml, '# Keep this comment.\nmodel = "selected-model"\n[prompts]\n' +
                    'general = ' + json.dumps('Before.\n' + self.section + 'After.\n') + '\n' +
                    'other = ' + json.dumps(self.section) + '\n[tools]\nenabled = true\n')
        self.instructions.chmod(0o640)
        self.originals = {path: (path.read_bytes(), path.stat().st_mode & 0o777)
                          for path in (self.hooks, self.instructions, self.toml)}
        self.write_manifest()

    def target(self, path: Path, **values: object) -> dict[str, object]:
        return {'path': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **values}

    def write_manifest(self, document: dict[str, object] | None = None) -> None:
        document = document or {
            'version': 1,
            'hook_files': [self.target(self.hooks, commands=[self.command])],
            'instruction_files': [self.target(self.instructions, sections=['old-context'])],
            'toml_sections': [self.target(self.toml, table=['prompts'], key='general', sections=['old-context'])],
        }
        atomic_text(self.manifest, json.dumps(document) + '\n')

    def assert_originals(self) -> None:
        for path, (content, mode) in self.originals.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_preview_changes_nothing_and_apply_preserves_unrelated_configuration(self) -> None:
        preview = plan(self.manifest)
        self.assertEqual(set(preview['changed_files']), {str(path) for path in self.originals})
        self.assertNotIn('Old generated rules.', json.dumps(preview))
        self.assert_originals()
        self.assertFalse(self.backups.exists())
        result = apply(self.manifest, self.backups)
        data = json.loads(self.hooks.read_text())
        self.assertEqual(data['model'], self.data['model'])
        self.assertEqual(data['custom'], self.data['custom'])
        self.assertEqual(data['hooks']['SessionStart'], [{**self.data['hooks']['SessionStart'][0],
                                                       'hooks': self.data['hooks']['SessionStart'][0]['hooks'][1:]}])
        self.assertEqual(data['hooks']['Stop'], [])
        self.assertEqual(data['hooks']['Other'], self.data['hooks']['Other'])
        self.assertEqual(self.instructions.read_text(), '# Personal rules\nKeep these rules.\n')
        self.assertEqual(self.instructions.stat().st_mode & 0o777, 0o640)
        parsed = tomlkit.parse(self.toml.read_text())
        self.assertEqual(parsed['prompts']['general'], 'Before.\nAfter.\n')
        self.assertEqual(parsed['prompts']['other'], self.section)
        self.assertEqual(parsed['model'], 'selected-model')
        self.assertTrue(parsed['tools']['enabled'])
        self.assertIn('# Keep this comment.', self.toml.read_text())
        self.assertEqual(Path(result['backup_directory']).stat().st_mode & 0o777, 0o700)
        restore(Path(result['receipt_path']))
        self.assert_originals()

    def test_repeated_apply_skips_backups_and_preserves_new_clean_user_edits(self) -> None:
        apply(self.manifest, self.backups)
        files = sorted(self.backups.rglob('*'))
        atomic_text(self.hooks, self.hooks.read_text().replace('user-choice', 'new-user-choice'))
        repeated = apply(self.manifest, self.backups)
        self.assertEqual(repeated['changed_files'], [])
        self.assertIsNone(repeated['backup_directory'])
        self.assertIsNone(repeated['receipt_path'])
        self.assertEqual(sorted(self.backups.rglob('*')), files)
        self.assertEqual(json.loads(self.hooks.read_text())['model'], 'new-user-choice')

    def test_noop_apply_does_not_create_backup_root(self) -> None:
        self.write_manifest({'version': 1})
        self.assertEqual(apply(self.manifest, self.backups)['changed_files'], [])
        self.assertFalse(self.backups.exists())

    def test_hash_mismatch_or_missing_hash_rejects_all_writes(self) -> None:
        for operation in ('mismatch', 'missing'):
            with self.subTest(operation=operation):
                self.write_manifest()
                data = json.loads(self.manifest.read_text())
                if operation == 'mismatch':
                    data['toml_sections'][0]['expected_sha256'] = '0' * 64
                else:
                    del data['toml_sections'][0]['expected_sha256']
                self.write_manifest(data)
                with self.assertRaises(ValueError):
                    apply(self.manifest, self.backups)
                self.assert_originals()
                self.assertFalse(self.backups.exists())

    def test_partial_apply_failure_rolls_back_content_and_modes(self) -> None:
        real_write = migrate_local.atomic_text

        def fail_last(path: Path, text: str) -> None:
            if path == self.toml:
                raise OSError('Simulated configuration failure')
            real_write(path, text)

        with patch.object(migrate_local, 'atomic_text', side_effect=fail_last):
            with self.assertRaisesRegex(OSError, 'Simulated'):
                apply(self.manifest, self.backups)
        self.assert_originals()

    def test_change_during_backup_rejects_transaction(self) -> None:
        real_backup = migrate_local.backup_paths

        def concurrent_edit(paths: list[Path], destination: Path):
            saved = real_backup(paths, destination)
            atomic_text(self.toml, self.toml.read_text() + '# A later edit.\n')
            return saved

        with patch.object(migrate_local, 'backup_paths', side_effect=concurrent_edit):
            with self.assertRaisesRegex(ValueError, 'changed'):
                apply(self.manifest, self.backups)
        self.assertEqual(self.hooks.read_bytes(), self.originals[self.hooks][0])
        self.assertEqual(self.instructions.read_bytes(), self.originals[self.instructions][0])
        self.assertIn('# A later edit.', self.toml.read_text())

    def test_rollback_preserves_concurrent_user_edit_and_reports_incomplete_recovery(self) -> None:
        real_write = migrate_local.atomic_text

        def concurrent_edit_then_failure(path: Path, text: str) -> None:
            if path == self.toml:
                real_write(self.hooks, '{"user": "concurrent edit"}\n')
                raise OSError('Simulated configuration failure')
            real_write(path, text)

        with patch.object(migrate_local, 'atomic_text', side_effect=concurrent_edit_then_failure):
            with self.assertRaisesRegex(RuntimeError, 'Rollback incomplete'):
                apply(self.manifest, self.backups)
        self.assertEqual(self.hooks.read_text(), '{"user": "concurrent edit"}\n')
        self.assertEqual(self.instructions.read_bytes(), self.originals[self.instructions][0])
        self.assertEqual(self.instructions.stat().st_mode & 0o777, self.originals[self.instructions][1])
        self.assertEqual(self.toml.read_bytes(), self.originals[self.toml][0])
        self.assertTrue(any(self.backups.glob('*/migration.json')))

    def test_restore_rejects_later_edits_before_writing_any_file(self) -> None:
        result = apply(self.manifest, self.backups)
        atomic_text(self.toml, self.toml.read_text() + '# A later edit.\n')
        before = {path: path.read_bytes() for path in self.originals}
        with self.assertRaisesRegex(ValueError, 'post-apply'):
            restore(Path(result['receipt_path']))
        self.assertEqual({path: path.read_bytes() for path in self.originals}, before)

    def test_restore_rejects_backup_receipt_and_manifest_tampering(self) -> None:
        result = apply(self.manifest, self.backups)
        receipt_path = Path(result['receipt_path'])
        receipt = json.loads(receipt_path.read_text())
        artifacts = [receipt_path, receipt_path.parent / 'manifest.json', Path(receipt['files'][-1]['backup'])]
        before = {path: path.read_bytes() for path in self.originals}
        for artifact in artifacts:
            with self.subTest(artifact=artifact.name):
                original = artifact.read_bytes()
                artifact.write_bytes(original + b'\nchanged')
                with self.assertRaises(ValueError):
                    restore(receipt_path)
                self.assertEqual({path: path.read_bytes() for path in self.originals}, before)
                artifact.write_bytes(original)

    def test_restore_rejects_redirected_backup_even_if_receipt_checksum_updated(self) -> None:
        result = apply(self.manifest, self.backups)
        receipt_path = Path(result['receipt_path'])
        receipt = json.loads(receipt_path.read_text())
        outside = self.home / 'outside.md'
        outside.write_bytes(Path(receipt['files'][0]['backup']).read_bytes())
        receipt['files'][0]['backup'] = str(outside)
        atomic_text(receipt_path, json.dumps(receipt) + '\n')
        atomic_text(receipt_path.parent / 'migration.sha256', migrate_local.digest(receipt_path.read_text()) + '\n')
        with self.assertRaisesRegex(ValueError, 'backup path'):
            restore(receipt_path)

    def test_restore_rejects_redirected_source_even_if_receipt_checksum_updated(self) -> None:
        result = apply(self.manifest, self.backups)
        receipt_path = Path(result['receipt_path'])
        receipt = json.loads(receipt_path.read_text())
        outside = self.home / 'unrelated.json'
        outside.write_bytes(self.hooks.read_bytes())
        before = outside.read_bytes()
        receipt['files'][0]['path'] = str(outside)
        atomic_text(receipt_path, json.dumps(receipt) + '\n')
        atomic_text(receipt_path.parent / 'migration.sha256', migrate_local.digest(receipt_path.read_text()) + '\n')
        with self.assertRaisesRegex(ValueError, 'backup path'):
            restore(receipt_path)
        self.assertEqual(outside.read_bytes(), before)

    def test_partial_restore_failure_rolls_back_to_migrated_files(self) -> None:
        result = apply(self.manifest, self.backups)
        before = {path: path.read_bytes() for path in self.originals}
        real_write = migrate_local.atomic_text

        def fail_last(path: Path, text: str) -> None:
            if path == self.toml:
                raise OSError('Simulated restore failure')
            real_write(path, text)

        with patch.object(migrate_local, 'atomic_text', side_effect=fail_last):
            with self.assertRaisesRegex(OSError, 'Simulated'):
                restore(Path(result['receipt_path']))
        self.assertEqual({path: path.read_bytes() for path in self.originals}, before)

    def test_rejects_source_links_parent_links_and_backup_root_links(self) -> None:
        source = self.home / 'original.json'
        self.hooks.rename(source)
        self.hooks.symlink_to(source)
        with self.assertRaisesRegex(ValueError, 'nonsymlink'):
            plan(self.manifest)
        self.hooks.unlink()
        source.rename(self.hooks)
        alias = self.home / 'alias'
        alias.symlink_to(self.hooks.parent, target_is_directory=True)
        self.write_manifest({'version': 1, 'hook_files': [self.target(alias / 'hooks.json', commands=[self.command])]})
        with self.assertRaisesRegex(ValueError, 'nonsymlink'):
            plan(self.manifest)
        self.write_manifest()
        self.backups.symlink_to(self.home / 'actual-backups', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'nonsymlink'):
            apply(self.manifest, self.backups)
        self.assert_originals()

    def test_restore_rejects_source_or_backup_symlink(self) -> None:
        result = apply(self.manifest, self.backups)
        receipt_path = Path(result['receipt_path'])
        receipt = json.loads(receipt_path.read_text())
        for path in (self.hooks, Path(receipt['files'][0]['backup'])):
            with self.subTest(path=path.name):
                target = self.home / 'alias-target'
                path.rename(target)
                path.symlink_to(target)
                with self.assertRaisesRegex(ValueError, 'nonsymlink'):
                    restore(receipt_path)
                path.unlink()
                target.rename(path)

    def test_malformed_or_nested_markers_abort_before_any_writes(self) -> None:
        cases = [
            '<!-- old-context:start -->\nMissing end.\n',
            '<!-- old-context:end -->\n<!-- old-context:start -->\n',
            self.section + self.section,
            '<!-- old-context :start -->\nBad marker.\n<!-- old-context:end -->\n',
            'Before ' + self.section,
            '<!-- old-context:start -->\n<!-- other:start -->\nUser rules.\n<!-- other:end -->\n<!-- old-context:end -->\n',
        ]
        for text in cases:
            with self.subTest(text=text):
                atomic_text(self.instructions, text)
                self.write_manifest()
                with self.assertRaises(ValueError):
                    apply(self.manifest, self.backups)
                self.assertEqual(self.instructions.read_text(), text)
                self.assertEqual(self.hooks.read_bytes(), self.originals[self.hooks][0])
                self.assertFalse(self.backups.exists())

    def test_exact_matching_preserves_similar_commands_and_sections(self) -> None:
        text = json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': self.command + ' '},
            {'type': 'command', 'command': 'prefix ' + self.command},
        ]}]}})
        self.assertEqual(migrate_local.remove_hooks(text, [self.command]), text)
        other = '<!-- unrelated:start -->\nOther content.\n<!-- unrelated:end -->\n'
        self.assertEqual(migrate_local.remove_sections(other + self.section, ['old-context']), other)

    def test_multiple_toml_fields_and_crlf_instruction_bytes_round_trip(self) -> None:
        self.instructions.write_bytes(('# Title\r\n' + self.section.replace('\n', '\r\n') + 'Keep.\r\n').encode())
        self.write_manifest()
        manifest = json.loads(self.manifest.read_text())
        manifest['toml_sections'].append(self.target(self.toml, table=['prompts'], key='other', sections=['old-context']))
        self.write_manifest(manifest)
        original = self.instructions.read_bytes()
        result = apply(self.manifest, self.backups)
        self.assertEqual(self.instructions.read_bytes(), b'# Title\r\nKeep.\r\n')
        self.assertEqual(tomlkit.parse(self.toml.read_text())['prompts']['other'], '')
        restore(Path(result['receipt_path']))
        self.assertEqual(self.instructions.read_bytes(), original)

    def test_rejects_unsupported_targets_and_non_prompt_toml_fields(self) -> None:
        file = self.home / 'resource.sh'
        atomic_text(file, self.section)
        cases = [
            {'instruction_files': [self.target(file, sections=['old-context'])]},
            {'toml_sections': [self.target(self.toml, table=['tools'], key='enabled', sections=['old-context'])]},
            {'toml_sections': [self.target(self.toml, table=['prompts'], key='missing', sections=['old-context'])]},
            {'instruction_files': [self.target(self.instructions, sections=['old-context']),
                                   self.target(self.instructions, sections=['old-context'])]},
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                self.write_manifest({'version': 1, **entries})
                with self.assertRaises(ValueError):
                    apply(self.manifest, self.backups)
                self.assert_originals()
                self.assertEqual(file.read_text(), self.section)
                self.assertFalse(self.backups.exists())

    def test_invalid_json_groups_and_duplicate_keys_fail_closed(self) -> None:
        for text in ('{"hooks": {"Stop": {}}}', '{"hooks": {"Stop": [{}]}}', '{"hooks": {}, "hooks": {}}'):
            with self.subTest(text=text):
                atomic_text(self.hooks, text)
                self.write_manifest()
                with self.assertRaises(ValueError):
                    apply(self.manifest, self.backups)
                self.assertFalse(self.backups.exists())


if __name__ == '__main__':
    unittest.main()
