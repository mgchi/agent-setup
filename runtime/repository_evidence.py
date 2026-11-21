"""Materialize only the committed text files selected for this review."""

import os
import re
from pathlib import Path

from context_store import Project, git, linked_path, sensitive_path
from disclosure import DisclosureError, DisclosureIndex
from tool_privacy import git_bytes, private


def selected_files(root: Path, project: Project, paths: list[Path], index: DisclosureIndex) -> str:
    if project.adviser_access != "selected-files":
        raise DisclosureError("repository-evidence-disabled")
    if not 1 <= len(paths) <= 8:
        raise DisclosureError("repository-evidence-count")
    commit = git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise DisclosureError("repository-evidence-needs-commit")
    sections = []
    remaining = 18000
    for path in dict.fromkeys(paths):
        name = path.as_posix()
        if (not path.parts or path.is_absolute() or any(part.startswith(".") for part in path.parts)
                or any(ord(char) < 32 for char in name) or sensitive_path(name)
                or any(linked_path(base / path) or private(base / path, index) for base in {root, *project.roots})):
            raise DisclosureError("repository-evidence-path")
        entry = git_bytes(root, ["--literal-pathspecs", "ls-tree", "-l", "-z", commit, "--", name])
        if entry.count(b"\0") != 1:
            raise DisclosureError("repository-evidence-needs-tracked-file")
        metadata, filename = entry.removesuffix(b"\0").split(b"\t", 1)
        mode, kind, oid, size = metadata.split()
        if filename != os.fsencode(name) or kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise DisclosureError("repository-evidence-file-type")
        heading = f"Committed file {name} at {commit}:\n"
        remaining -= len(heading.encode()) + (2 if sections else 0)
        if int(size) > remaining:
            raise DisclosureError("repository-evidence-size")
        data = git_bytes(root, ["cat-file", "blob", oid.decode("ascii")])
        if len(data) != int(size) or b"\x00" in data:
            raise DisclosureError("repository-evidence-file-type")
        try:
            body = data.decode("utf-8")
        except UnicodeError:
            raise DisclosureError("repository-evidence-file-type") from None
        index.scan(body)
        sections.append(heading + body)
        remaining -= len(data)
    text = "\n\n".join(sections)
    index.scan(text)
    return text
