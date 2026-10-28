"""Local exact-match and credential checks for outgoing data."""

import codecs
import hashlib
import re
import stat
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, Field

WINDOW = 24
CHUNK = 65536
MAX_TOKEN = 1024 * 1024
WORDS = re.compile(r"[^\W_]+", re.UNICODE)
PATTERNS = {
    "provider-token": re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})\b"),
    "cloud-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "credential-assignment": re.compile(r"(?i)\b(?:api[_-]?key|(?:access|auth|refresh)[_-]?token|client[_-]?secret|(?:aws[_-]?)?secret[_-]?(?:access[_-]?)?key|password|passwd)\s*[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9+/_.=-]{16,})(?:[\"'\s,;]|$)"),
    "bearer-token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/=-]{24,}"),
    "authenticated-url": re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis)://[^\s/:]+:[^\s/@]{6,}@"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
}


class DisclosureError(ValueError):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"Disclosure blocked ({category}).")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def linked(path: Path) -> bool:
    absolute = path.expanduser().absolute()
    aliases = {Path('/tmp'), Path('/var'), Path('/etc')}
    return any(part.is_symlink() and part not in aliases for part in (absolute, *absolute.parents))


def regular(path: Path) -> None:
    if linked(path):
        raise DisclosureError('linked-file')
    try:
        mode = path.stat().st_mode
    except OSError:
        raise DisclosureError('unreadable-file') from None
    if not stat.S_ISREG(mode):
        raise DisclosureError('nonregular-file')


def fingerprints(tokens: list[str]) -> set[str]:
    result: set[str] = set()
    for offset in range(len(tokens) - WINDOW + 1):
        text = ' '.join(tokens[offset:offset + WINDOW]).casefold()
        if len(text) >= 120:
            result.add(digest_bytes(text.encode()))
    return result


def secret_matches(text: str) -> set[tuple[str, str]]:
    return {(category, digest_bytes(match.group().encode()))
            for category, pattern in PATTERNS.items() for match in pattern.finditer(text)}


class DisclosureIndex(BaseModel):
    root: Path
    private_roots: list[Path] = Field(default_factory=list)
    source_files: list[Path] = Field(default_factory=list)
    words: set[str] = Field(default_factory=set)
    files: set[str] = Field(default_factory=set)

    def scan(self, text: str) -> None:
        scanner = RawScanner(self)
        scanner.feed(text.encode(), final=True)

    def scan_file(self, path: Path) -> None:
        regular(path)
        scanner = RawScanner(self)
        with path.open('rb') as source:
            while chunk := source.read(CHUNK):
                scanner.feed(chunk)
            scanner.feed(b'', final=True)

    def matches(self, data: bytes) -> set[tuple[str, str]]:
        scanner = RawScanner(self, matching=True)
        scanner.feed(data, final=True)
        return scanner.matches


class RawScanner:
    def __init__(self, index: DisclosureIndex, *, collecting: bool = False,
                 matching: bool = False) -> None:
        self.index = index
        self.collecting = collecting
        self.matching = matching
        self.matches: set[tuple[str, str]] = set()
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.hash = hashlib.sha256()
        self.carry = ''
        self.previous: list[str] = []
        self.secret_tail = ''
        self.done = False
        self.size = 0

    def feed(self, data: bytes, *, final: bool = False) -> None:
        if self.done:
            return
        # Scan in fixed chunks even when the caller supplies one large buffer.
        for offset in range(0, len(data), CHUNK):
            self._feed(data[offset:offset + CHUNK], False)
        if final:
            self._feed(b'', True)

    def _feed(self, data: bytes, final: bool) -> None:
        self.hash.update(data)
        self.size += len(data)
        decoded = self.decoder.decode(data, final=final)
        secret_text = self.secret_tail + decoded
        if self.matching:
            self.matches.update(secret_matches(secret_text))
        elif not self.collecting:
            for category, pattern in PATTERNS.items():
                if pattern.search(secret_text):
                    raise DisclosureError(category)
        self.secret_tail = secret_text[-4096:]
        text = self.carry + decoded
        unfinished = None
        if not final:
            unfinished = re.search(r'[^\W_]+\Z', text, re.UNICODE)
        self.carry = unfinished.group() if unfinished else ''
        if len(self.carry) > MAX_TOKEN:
            raise DisclosureError('scan-limit')
        complete = text[:-len(self.carry)] if self.carry else text
        tokens = self.previous + WORDS.findall(complete)
        found = fingerprints(tokens)
        if self.collecting:
            self.index.words.update(found)
        elif self.matching:
            self.matches.update(('private-context', value) for value in found & self.index.words)
        elif found & self.index.words:
            raise DisclosureError('private-context')
        self.previous = tokens[-(WINDOW - 1):]
        if final:
            self.done = True
            fingerprint = self.hash.hexdigest()
            if self.size and self.collecting:
                self.index.files.add(fingerprint)
            elif self.size and fingerprint in self.index.files:
                if self.matching:
                    self.matches.add(('private-file', fingerprint))
                else:
                    raise DisclosureError('private-file')


def build_index(root: Path, memories: Iterable[str] = (), *, source_files: Iterable[Path] = (),
                private_roots: Iterable[Path] = (), context_dirs: Iterable[Path] = ()) -> DisclosureIndex:
    if linked(root):
        raise DisclosureError('linked-context')
    index = DisclosureIndex(root=root.expanduser().resolve(), source_files=list(source_files), private_roots=list(private_roots))
    workspaces = root / 'workspaces'
    if linked(workspaces):
        raise DisclosureError('linked-context')
    roots = [root]
    for project in [*sorted(workspaces.glob('*')), *context_dirs]:
        if linked(project):
            raise DisclosureError('linked-context')
        if project.is_dir() and project not in roots:
            roots.append(project)
    files: set[Path] = set()
    for source in index.source_files:
        regular(source)
        files.add(source)
    for private_root in index.private_roots:
        if linked(private_root) or not private_root.is_dir():
            raise DisclosureError('invalid-private-root')
        for path in private_root.rglob('*'):
            if '.git' in path.relative_to(private_root).parts:
                continue
            if linked(path):
                raise DisclosureError('linked-private-source')
            if path.is_file():
                files.add(path)
    for context in roots:
        files.update(context.glob('*.md'))
        for folder in ('projects', 'customers', 'meetings', 'local'):
            directory = context / folder
            if linked(directory):
                raise DisclosureError('linked-context')
            files.update(directory.glob('*.md'))
    for path in sorted(files):
        regular(path)
        scanner = RawScanner(index, collecting=True)
        with path.open('rb') as source:
            while chunk := source.read(CHUNK):
                scanner.feed(chunk)
            scanner.feed(b'', final=True)
    for memory in memories:
        RawScanner(index, collecting=True).feed(memory.encode(), final=True)
    return index
