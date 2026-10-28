"""Best-effort disclosure checks at the supported clients' tool boundary."""

import base64
import binascii
import html
import json
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

from pydantic import JsonValue

from disclosure import DisclosureError, DisclosureIndex, RawScanner, linked, regular

MAX_BLOB = 8 * 1024 * 1024
MAX_HEAD_BYTES = 32 * 1024 * 1024
HEAD_SCAN_SECONDS = 4.0
PATHS = re.compile(r"(?:^|[\s\"'=(@])((?:/|~/|\./|\.\./)[^\s\"'<>;|)&]+)")
SHELL_OPERATORS = re.compile(r"[\n\r;|&<>`]|\$[({]")
PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete|Move to) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)


def denial(category: str) -> str:
    safe = re.sub(r'[^a-z0-9-]', '-', category.lower())[:64]
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'Local disclosure guard blocked this operation ({safe}).'}})


def strings(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for child in value for text in strings(child)]
    if isinstance(value, dict):
        return [text for key, child in value.items() for text in [key, *strings(child)]]
    return []


def decoded_strings(value: JsonValue) -> list[str]:
    pending = [(text, 0) for text in strings(value)]
    seen: set[str] = set()
    size = 0
    while pending:
        text, depth = pending.pop()
        if text in seen:
            continue
        seen.add(text)
        size += len(text.encode())
        if depth > 8 or size > MAX_BLOB:
            raise DisclosureError('argument-scan-limit')
        variants = [unquote(text), html.unescape(text)]
        variants.append(re.sub(r'\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})',
                               lambda match: chr(int(match.group(1) or match.group(2), 16)), text))
        if text.lstrip().startswith(('"', '{', '[')):
            try:
                variants.extend(strings(json.loads(text)))
            except (ValueError, RecursionError):
                pass
        for token in re.findall(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/])', text):
            try:
                decoded = base64.b64decode(token, validate=True).decode('utf-8')
                if decoded.isprintable() or '\n' in decoded:
                    variants.append(decoded)
            except (ValueError, UnicodeError, binascii.Error):
                pass
        pending.extend((variant, depth + 1) for variant in variants if variant != text and variant not in seen)
    return list(seen)


def path_at(value: str, cwd: Path) -> Path:
    path = Path(value.removeprefix('@')).expanduser()
    return path if path.is_absolute() else cwd / path


def private(path: Path, index: DisclosureIndex) -> bool:
    if linked(path):
        return False
    resolved = path.resolve()
    return (resolved.is_relative_to(index.root.resolve())
            or resolved in {source.resolve() for source in index.source_files}
            or any(resolved.is_relative_to(root.resolve()) for root in index.private_roots))


def shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        raise DisclosureError('unparsed-command') from None


def private_command(command: str, cwd: Path, index: DisclosureIndex) -> bool:
    if SHELL_OPERATORS.search(command) or '$' in command:
        return False
    words = shell_words(command)
    if not words:
        return False
    executable = words[0]
    cli = str(Path.home() / '.local/bin/agent-context')
    if executable in {'agent-context', cli} and len(words) > 1:
        offset = 1
        if words[offset] == '--settings' or words[offset].startswith('--settings='):
            if words[offset] == '--settings':
                if len(words) <= offset + 1:
                    return False
                settings = path_at(words[offset + 1], cwd)
                offset += 2
            else:
                settings = path_at(words[offset].split('=', 1)[1], cwd)
                offset += 1
            canonical = Path.home() / '.config/agent-context/settings.json'
            if linked(settings) or settings.resolve() != canonical.resolve():
                return False
        if len(words) <= offset or words[offset] not in {'load', 'status', 'collect', 'checkpoint', 'remember', 'refresh', 'finish', 'privacy', 'cleanup', 'cooldown', 'forget', 'review', 'begin', 'project'}:
            return False
        if any(word == '--settings' or word.startswith('--settings=') for word in words[offset + 1:]):
            return False
        file_options = {'--file', '--answer-file', '--request-file', '--prompt-file', '--evidence'}
        for offset, word in enumerate(words):
            if word in file_options and (offset + 1 >= len(words) or not private(path_at(words[offset + 1], cwd), index)):
                return False
            if any(word.startswith(option + '=') for option in file_options):
                if not private(path_at(word.split('=', 1)[1], cwd), index):
                    return False
        return True
    if executable not in {'cat', '/bin/cat', 'head', '/usr/bin/head', 'tail', '/usr/bin/tail'}:
        return False
    arguments = words[1:]
    if Path(executable).name in {'head', 'tail'} and arguments[:1] == ['-n']:
        if len(arguments) < 3 or not arguments[1].isdigit():
            return False
        arguments = arguments[2:]
    return bool(arguments) and all(not word.startswith('-') and private(path_at(word, cwd), index) for word in arguments)


def private_operation(index: DisclosureIndex, cwd: Path, tool: str, payload: JsonValue) -> bool:
    if not isinstance(payload, dict):
        return False
    name = tool.lower().split('__')[-1].split('.')[-1]
    target = payload.get('file_path', payload.get('path'))
    if name in {'read', 'read_file', 'write', 'write_file', 'edit', 'edit_file'}:
        allowed = {'file_path', 'path', 'offset', 'limit'}
        if name in {'write', 'write_file'}:
            allowed = {'file_path', 'path', 'content'}
        elif name in {'edit', 'edit_file'}:
            allowed = {'file_path', 'path', 'old_string', 'new_string', 'old_str', 'new_str', 'replace_all'}
        return (set(payload) <= allowed and not {'file_path', 'path'} <= set(payload)
                and isinstance(target, str) and private(path_at(target, cwd), index))
    command = payload.get('command', payload.get('cmd'))
    if name in {'bash', 'exec_command', 'shell_command'} and isinstance(command, str):
        return private_command(command, cwd, index)
    patch = payload.get('patch', payload.get('input', command))
    if name == 'apply_patch' and isinstance(patch, str):
        targets = [first or second for first, second in PATCH_PATH.findall(patch)]
        return bool(targets) and '*** Delete File:' not in patch and all(private(path_at(value, cwd), index) for value in targets)
    return False


def references(texts: list[str], cwd: Path) -> set[Path]:
    paths: set[Path] = set()
    for text in texts:
        candidates = [match.group(1).rstrip(',') for match in PATHS.finditer(text)]
        candidates.extend(first or second for first, second in PATCH_PATH.findall(text))
        # Whole string values preserve spaces in structured attachment paths.
        if len(text) < 4096 and '\n' not in text:
            candidates.append(text)
        try:
            lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|<>()')
            lexer.whitespace_split = True
            lexer.commenters = ''
            words = list(lexer)
        except ValueError:
            words = []
        candidates.extend(word for word in words if not word.startswith('-'))
        for offset, word in enumerate(words):
            if word in {'--body-file', '--file', '--input', '--data-binary', '--data', '-d', '-F', '--form', '--upload-file', '-T', '--attach'}:
                if offset + 1 < len(words):
                    candidates.append(words[offset + 1].split('=@', 1)[-1])
            elif word.startswith('@') or '=@' in word:
                candidates.append(word.split('=@', 1)[-1])
            elif re.match(r'--(?:body-file|file|input|upload-file|attach)=', word):
                candidates.append(word.split('=', 1)[1])
        for candidate in candidates:
            if not candidate or '\x00' in candidate or '://' in candidate or len(candidate) > 4096:
                continue
            try:
                path = path_at(candidate, cwd)
                if path.exists() or path.is_symlink():
                    paths.add(path)
            except (OSError, RuntimeError, ValueError):
                continue
    return paths


def git_bytes(cwd: Path, args: list[str], *, missing: bool = False, timeout: float = 5) -> bytes:
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(['git', '-C', str(cwd), *args], stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=timeout, check=False)
            if result.returncode:
                if missing:
                    return b''
                raise DisclosureError('git-scan-failed')
            output.seek(0)
            data = output.read(MAX_BLOB + 1)
            if len(data) > MAX_BLOB:
                raise DisclosureError('git-scan-limit')
            return data
        except (OSError, subprocess.SubprocessError):
            raise DisclosureError('git-scan-failed') from None


def head_matches(index: DisclosureIndex, cwd: Path) -> set[tuple[str, str]]:
    deadline = time.monotonic() + HEAD_SCAN_SECONDS
    matches: set[tuple[str, str]] = set()
    entries = git_bytes(cwd, ['ls-tree', '-r', '-l', '-z', 'HEAD'], missing=True,
                        timeout=HEAD_SCAN_SECONDS).split(b'\0')
    blobs: dict[bytes, int] = {}
    total = 0
    for entry in entries:
        if not entry:
            continue
        metadata, _ = entry.split(b'\t', 1)
        mode, kind, oid, raw_size = metadata.split()
        if kind != b'blob' or mode not in {b'100644', b'100755'}:
            continue
        size = int(raw_size)
        if size < 0 or size > MAX_BLOB or not re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', oid):
            raise DisclosureError('git-scan-limit')
        if oid not in blobs:
            total += size
            blobs[oid] = size
        if total > MAX_HEAD_BYTES or len(blobs) > 20000:
            raise DisclosureError('git-scan-limit')
    if not blobs:
        return matches
    with tempfile.TemporaryFile() as output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DisclosureError('git-scan-time-limit')
        try:
            result = subprocess.run(['git', '-C', str(cwd), 'cat-file', '--batch'],
                                    input=b'\n'.join(blobs) + b'\n', stdout=output,
                                    stderr=subprocess.DEVNULL, timeout=remaining, check=False)
        except (OSError, subprocess.SubprocessError):
            raise DisclosureError('git-scan-failed') from None
        if result.returncode or output.tell() > total + len(blobs) * 128:
            raise DisclosureError('git-scan-failed')
        output.seek(0)
        for oid, size in blobs.items():
            header = output.readline(128).split()
            if header != [oid, b'blob', str(size).encode()]:
                raise DisclosureError('git-scan-incomplete')
            scanner = RawScanner(index, matching=True)
            unread = size
            while unread:
                if time.monotonic() >= deadline:
                    raise DisclosureError('git-scan-time-limit')
                chunk = output.read(min(unread, 65536))
                if not chunk:
                    raise DisclosureError('git-scan-incomplete')
                scanner.feed(chunk)
                unread -= len(chunk)
            scanner.feed(b'', final=True)
            matches.update(scanner.matches)
            if output.read(1) != b'\n':
                raise DisclosureError('git-scan-incomplete')
        if output.read(1) or time.monotonic() >= deadline:
            raise DisclosureError('git-scan-incomplete')
    return matches


def verified_private_repository(index: DisclosureIndex, cwd: Path) -> bool:
    expected = index.private_github_repositories.get(str(cwd.resolve()))
    if not expected:
        return False
    root = git_bytes(cwd, ['rev-parse', '--show-toplevel']).decode().strip()
    remotes = git_bytes(cwd, ['remote']).decode().splitlines()
    urls = git_bytes(cwd, ['remote', 'get-url', '--push', '--all', 'origin']).decode().splitlines()
    allowed = {f'https://github.com/{expected}', f'https://github.com/{expected}.git',
               f'git@github.com:{expected}', f'git@github.com:{expected}.git'}
    if linked(cwd) or Path(root).resolve() != cwd.resolve() or remotes != ['origin'] or not urls or not set(urls) <= allowed:
        raise DisclosureError('private-repository-destination')
    try:
        result = subprocess.run(['gh', 'api', '--hostname', 'github.com', f'repos/{expected}', '--jq', '.private'],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        raise DisclosureError('private-repository-unverified') from None
    if result.returncode or result.stdout.strip() != 'true':
        raise DisclosureError('private-repository-unverified')
    return True


def staged(index: DisclosureIndex, cwd: Path) -> None:
    private_destination = verified_private_repository(index, cwd)
    scanner = DisclosureIndex(root=index.root) if private_destination else index
    names = git_bytes(cwd, ['diff', '--cached', '--name-only', '--diff-filter=ACMRTUXB', '-z']).split(b'\0')
    baseline: set[tuple[str, str]] | None = None
    for raw_name in names:
        if not raw_name:
            continue
        name = raw_name.decode('utf-8', errors='strict')
        entries = git_bytes(cwd, ['ls-files', '--stage', '-z', '--', name]).split(b'\0')
        for entry in entries:
            if not entry:
                continue
            metadata, _ = entry.split(b'\t', 1)
            mode, oid, stage = metadata.split()
            if mode not in {b'100644', b'100755'} or stage != b'0':
                raise DisclosureError('linked-or-unmerged-staged-file')
            found = scanner.matches(git_bytes(cwd, ['cat-file', 'blob', oid.decode()]))
            if not found:
                continue
            if private_destination:
                raise DisclosureError(sorted(found)[0][0])
            previous = git_bytes(cwd, ['show', f'HEAD:{name}'], missing=True)
            fresh = found - index.matches(previous)
            if fresh:
                if baseline is None:
                    baseline = head_matches(index, cwd)
                fresh -= baseline
            if fresh:
                raise DisclosureError(sorted(fresh)[0][0])


def staged_commands(index: DisclosureIndex, cwd: Path, texts: list[str]) -> None:
    checked: set[Path] = set()
    for text in texts:
        if not re.search(r'\bgit\b[^\n]*\b(?:commit|push)\b', text):
            continue
        if re.search(r'\bcd\b|\$|`|GIT_|--git-dir|--work-tree', text):
            raise DisclosureError('ambiguous-git-directory')
        inspected = False
        for command in re.split(r'[\n;|&]+', text):
            words = shell_words(command)
            for position, word in enumerate(words):
                if word not in {'git', '/usr/bin/git', '/opt/homebrew/bin/git'}:
                    continue
                arguments = words[position + 1:]
                target = cwd
                if words[:position] not in ([], ['command']):
                    raise DisclosureError('ambiguous-git-command')
                while arguments and arguments[0].startswith('-'):
                    option = arguments.pop(0)
                    if option == '-C':
                        if not arguments:
                            raise DisclosureError('ambiguous-git-directory')
                        target = path_at(arguments.pop(0), target)
                    elif option.startswith('-C'):
                        target = path_at(option[2:], target)
                    elif option not in {'--no-pager', '--paginate', '--no-optional-locks',
                                        '--no-replace-objects', '--literal-pathspecs',
                                        '--glob-pathspecs', '--noglob-pathspecs', '--icase-pathspecs'}:
                        raise DisclosureError('ambiguous-git-command')
                if arguments and arguments[0] in {'commit', 'push'}:
                    inspected = True
                    if arguments[0] == 'push' and str(target.resolve()) in index.private_github_repositories:
                        positional = []
                        for argument in arguments[1:]:
                            if argument.startswith('-'):
                                if argument not in {'-u', '--set-upstream', '--dry-run'}:
                                    raise DisclosureError('private-repository-push-arguments')
                            else:
                                positional.append(argument)
                        if positional and (positional[0] != 'origin' or any(
                                not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./:+-]*', ref) or '://' in ref
                                for ref in positional[1:])):
                            raise DisclosureError('private-repository-push-arguments')
                    if target not in checked:
                        staged(index, target)
                        checked.add(target)
        if not inspected:
            raise DisclosureError('ambiguous-git-command')


def guard(index: DisclosureIndex, cwd: Path, tool_name: str, tool_input: JsonValue) -> str:
    try:
        if isinstance(tool_input, dict) and isinstance(tool_input.get('workdir'), str):
            cwd = path_at(tool_input['workdir'], cwd)
        texts = decoded_strings(tool_input)
        if private_operation(index, cwd, tool_name, tool_input):
            credentials = DisclosureIndex(root=index.root)
            for text in texts:
                credentials.scan(text)
            return ''
        for text in texts:
            index.scan(text)
        for path in references(texts, cwd):
            if private(path, index):
                raise DisclosureError('private-path-egress')
            if path.is_dir() and not linked(path):
                continue
            regular(path)
            scanner = RawScanner(index)
            with path.open('rb') as source:
                while chunk := source.read(65536):
                    scanner.feed(chunk)
                scanner.feed(b'', final=True)
        name = tool_name.lower().split('__')[-1].split('.')[-1]
        if name in {'bash', 'exec_command', 'shell_command', 'exec', 'js', 'run_code', 'run'}:
            staged_commands(index, cwd, texts)
        return ''
    except DisclosureError as error:
        return denial(error.category)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return denial('tool-validation')
