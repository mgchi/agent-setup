"""Explicit, bounded instruction export without private source disclosure."""

import tempfile
import unittest
from pathlib import Path

from disclosure import DisclosureError, build_index
from instruction_copies import export_instruction_bundle


class InstructionCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.private = self.root / 'context'
        self.private.mkdir()
        self.source = self.root / 'guide.md'
        self.source.write_text('Use the existing project components.\n')
        self.index = build_index(self.private)

    def test_selected_regular_instruction_is_exported_with_receipt(self) -> None:
        destination = self.private / 'local/copies'
        receipt = export_instruction_bundle(self.index, {'guide.md': self.source}, destination)
        self.assertEqual((destination / 'guide.md').read_text(), self.source.read_text())
        self.assertIn('sha256', receipt)
        self.assertEqual((destination / 'guide.md').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(DisclosureError):
            export_instruction_bundle(self.index, {'guide.md': self.source}, destination)

    def test_private_source_and_unsafe_names_cannot_be_exported(self) -> None:
        secret = self.private / 'context.md'
        secret.write_text('Private owner guidance remains private.\n')
        for sources in ({'guide.md': secret}, {'../guide.md': self.source}, {}):
            with self.subTest(sources=list(sources)), self.assertRaises(DisclosureError):
                export_instruction_bundle(self.index, sources, self.private / 'local/copies')

    def test_credential_examples_are_redacted_and_private_overlap_is_rejected(self) -> None:
        token = 'sk-' + 'x' * 30
        self.source.write_text('Example: ' + token + '\n')
        destination = self.private / 'local/copies'
        export_instruction_bundle(self.index, {'guide.md': self.source}, destination)
        self.assertNotIn(token, (destination / 'guide.md').read_text())
        private_text = ' '.join(f'private-word-{i}' for i in range(60))
        (self.private / 'context.md').write_text(private_text)
        self.source.write_text(private_text)
        with self.assertRaises(DisclosureError):
            export_instruction_bundle(build_index(self.private), {'guide.md': self.source}, self.private / 'local/other')

    def test_linked_private_source_and_external_destination_are_rejected(self) -> None:
        secret = self.private / 'context.md'
        secret.write_text('Private owner guidance.\n')
        link = self.root / 'linked.md'
        link.symlink_to(secret)
        with self.assertRaises(DisclosureError):
            export_instruction_bundle(self.index, {'guide.md': link}, self.private / 'local/copies')
        with self.assertRaises(DisclosureError):
            export_instruction_bundle(self.index, {'guide.md': self.source}, self.root / 'outside')


if __name__ == '__main__':
    unittest.main()
