"""Export explicitly configured instructions after disclosure validation."""

import hashlib
import os
import re
import stat
from pathlib import Path

from disclosure import PATTERNS, DisclosureError, DisclosureIndex, linked

MAX_INSTRUCTION_BYTES = 256 * 1024


def sanitized_instruction(index: DisclosureIndex, path: Path) -> tuple[str, int]:
    resolved = path.resolve(strict=True)
    if resolved.suffix.lower() != '.md':
        raise DisclosureError('instruction-file-type')
    if (resolved.is_relative_to(index.root.resolve())
            or resolved in {source.resolve() for source in index.source_files}
            or any(resolved.is_relative_to(root.resolve()) for root in index.private_roots)):
        raise DisclosureError('private-instruction-source')
    descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INSTRUCTION_BYTES:
            raise DisclosureError('instruction-file-size')
        raw = source.read(MAX_INSTRUCTION_BYTES + 1)
    if len(raw) > MAX_INSTRUCTION_BYTES:
        raise DisclosureError('instruction-file-size')
    private_matches = {category for category, _ in index.matches(raw)} - set(PATTERNS)
    if private_matches:
        raise DisclosureError(sorted(private_matches)[0])
    body = raw.decode('utf-8')
    count = 0
    for category, pattern in PATTERNS.items():
        body, changed = pattern.subn('[REDACTED ' + category + ']', body)
        count += changed
    body = body.rstrip() + '\n'
    index.scan(body)
    return body, count


def export_instruction_bundle(index: DisclosureIndex, sources: dict[str, Path], destination: Path) -> str:
    if not sources or len(sources) > 12 or any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*\.md', name) for name in sources):
        raise DisclosureError('instruction-source-list')
    destination = destination.expanduser().absolute()
    if linked(destination) or not destination.resolve().is_relative_to((index.root / 'local').resolve()):
        raise DisclosureError('instruction-destination')
    if destination.exists():
        raise DisclosureError('instruction-destination-exists')
    prepared = {name: sanitized_instruction(index, path) for name, path in sources.items()}
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    receipts = []
    try:
        for name, (body, redactions) in prepared.items():
            descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(body)
            receipts.append(f'{name}: {redactions} credential example redactions; sha256 {hashlib.sha256(body.encode()).hexdigest()}')
    except Exception:
        for name in prepared:
            target = destination / name
            if target.exists() and not target.is_symlink():
                target.unlink()
        destination.rmdir()
        raise
    return 'Sanitized instruction bundle: ' + str(destination) + '\n' + '\n'.join(receipts)
