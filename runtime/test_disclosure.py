"""Synthetic disclosure checks at streaming boundaries."""

import tempfile
import unittest
from pathlib import Path

from disclosure import CHUNK, MAX_TOKEN, DisclosureError, DisclosureIndex, RawScanner, build_index


class DisclosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.phrase = " ".join(f"confidentialrecord{number:03d}" for number in range(48))
        (self.root / "brief.md").write_text(self.phrase)
        self.index = build_index(self.root)

    def test_context_crosses_stream_and_utf8_boundaries(self) -> None:
        data = ("public résumé " + self.phrase + " end").encode()
        scanner = RawScanner(self.index)
        with self.assertRaisesRegex(DisclosureError, "private-context"):
            for value in data:
                scanner.feed(bytes([value]))
            scanner.feed(b"", final=True)

    def test_credentials_cross_read_boundaries_and_follow_large_prefixes(self) -> None:
        secret = "sk-" + "a" * 30
        data = ("public " * (CHUNK // 7) + " " + secret).encode()
        split = data.index(secret.encode()) + 9
        scanner = RawScanner(DisclosureIndex(root=self.root))
        scanner.feed(data[:split])
        with self.assertRaisesRegex(DisclosureError, "provider-token"):
            scanner.feed(data[split:], final=True)

    def test_whole_file_scan_checks_beyond_an_excerpt(self) -> None:
        path = self.root / "outgoing.txt"
        path.write_text("safe public content " * 5000 + self.phrase)
        with self.assertRaisesRegex(DisclosureError, "private-context"):
            self.index.scan_file(path)

    def test_short_exact_copy_uses_file_fingerprint(self) -> None:
        (self.root / "short.md").write_text("Synthetic internal label")
        index = build_index(self.root)
        with self.assertRaisesRegex(DisclosureError, "private-file"):
            index.scan("Synthetic internal label")
        index.scan("Unrelated public summary")

    def test_symlink_sources_and_outputs_are_rejected(self) -> None:
        linked = self.root / "linked.md"
        linked.symlink_to(self.root / "brief.md")
        with self.assertRaisesRegex(DisclosureError, "linked-file"):
            self.index.scan_file(linked)
        with self.assertRaisesRegex(DisclosureError, "linked-file"):
            build_index(self.root)

    def test_long_unfinished_token_is_bounded(self) -> None:
        with self.assertRaisesRegex(DisclosureError, "scan-limit"):
            self.index.scan("z" * (MAX_TOKEN + 1))

    def test_project_kernels_and_nested_topics_are_indexed(self) -> None:
        for relative in ("workspaces/example/context.md", "workspaces/example/projects/topic.md"):
            with self.subTest(path=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                text = " ".join(f"projectrecord{number:03d}" for number in range(48))
                path.write_text(text)
                with self.assertRaisesRegex(DisclosureError, "private-context|private-file"):
                    build_index(self.root).scan(text)
                path.unlink()

    def test_symlinked_workspace_container_is_rejected(self) -> None:
        destination = self.root / "external"
        destination.mkdir()
        (self.root / "workspaces").symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(DisclosureError, "linked-context"):
            build_index(self.root)

    def test_symlinked_project_directory_is_rejected(self) -> None:
        destination = self.root / "external"
        destination.mkdir()
        workspaces = self.root / "workspaces"
        workspaces.mkdir()
        (workspaces / "example").symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(DisclosureError, "linked-context"):
            build_index(self.root)


if __name__ == "__main__":
    unittest.main()
