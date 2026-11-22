from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import tomlkit
from pydantic import BaseModel, JsonValue, RootModel
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from team_runtime import ADVISER_DEFAULTS, FOLLOW_DEFAULT
from context_store import CAPTURE_CHARS, Capture, Cooldown, Memory, Settings, Store, atomic_text, captured_user_text, clean, digest, linked_path, lock, persist_memories, validate_memory_capture

HOME_DIR = Path.home()
TEMPLATES = Path(__file__).parent / 'templates'
IMPORT_KEY = 'initial-preferences-v1'
MAINTENANCE_LABEL = 'local.agent-context-maintenance'


class JsonDocument(RootModel[dict[str, JsonValue]]):
    pass


class BackupRecord(BaseModel):
    path: str
    existed: bool
    backup: str | None = None
    link_target: str | None = None
    mode: int | None = None


class InstallReceipt(BaseModel):
    created_at: str
    backup_directory: str
    paths: list[BackupRecord]


class FileWrite(BaseModel):
    path: Path
    text: str
    mode: int = 0o600


class LinkWrite(BaseModel):
    path: Path
    target: str


class InstallPlan(BaseModel):
    files: list[FileWrite]
    links: list[LinkWrite]
    settings: Settings


class PreferenceSeed(BaseModel):
    prompt: str
    memories: list[Memory]
    cooldowns: list[Cooldown] = []


class InstallResult(BaseModel):
    backup_directory: str
    changed_files: list[str]
    imported_preferences: int
    maintenance: str
    notes: list[str] = []


class ImportReceipt(BaseModel):
    import_id: str
    imported_preferences: int
    completed_at: float


def read_json(path: Path) -> JsonDocument:
    content = existing_text(path)
    return JsonDocument.model_validate_json(content) if content else JsonDocument({})


def write_json(path: Path, document: JsonDocument) -> None:
    atomic_text(path, document.model_dump_json(indent=2) + '\n')


def backup_paths(paths: list[Path], destination: Path) -> InstallReceipt:
    destination.mkdir(parents=True, mode=0o700)
    records: list[BackupRecord] = []
    for index, path in enumerate(paths):
        if path.is_symlink():
            records.append(BackupRecord(path=str(path), existed=True, link_target=os.readlink(path)))
        elif path.exists():
            backup = destination / f'{index:02d}-{path.name}'
            if path.name == 'state.sqlite3':
                source = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
                target = sqlite3.connect(backup)
                try:
                    source.backup(target)
                finally:
                    source.close()
                    target.close()
            else:
                shutil.copy2(path, backup)
            backup.chmod(0o600)
            records.append(BackupRecord(path=str(path), existed=True, backup=str(backup), mode=path.stat().st_mode & 0o777))
        else:
            records.append(BackupRecord(path=str(path), existed=False))
    receipt = InstallReceipt(
        created_at=datetime.now(timezone.utc).isoformat(),
        backup_directory=str(destination),
        paths=records,
    )
    atomic_text(destination / 'manifest.json', receipt.model_dump_json(indent=2) + '\n')
    return receipt


def managed_section(original: str, name: str, content: str) -> str:
    start = f'<!-- {name}:start -->'
    end = f'<!-- {name}:end -->'
    block = f'{start}\n{content.rstrip()}\n{end}'
    if re.search(r'<!--\s*' + re.escape(name) + r'\b', original):
        markers = re.findall(r'<!--\s*' + re.escape(name) + r'\b.*?-->', original, re.DOTALL)
        if markers != [start, end] or original.count(start) != 1 or original.count(end) != 1:
            raise ValueError(f'Ambiguous managed section: {name}')
        before, rest = original.split(start, 1)
        _, after = rest.split(end, 1)
        return before + block + after
    return (original.rstrip() + '\n\n' if original.strip() else '') + block + '\n'


def existing_text(path: Path) -> str:
    if linked_path(path):
        raise ValueError(f'Expected a canonical file: {path}')
    return path.read_text() if path.exists() else ''


def remove_sections(text: str, sections: list[str]) -> str:
    if len(set(sections)) != len(sections):
        raise ValueError('Duplicate managed section selection')
    for name in sections:
        managed_section(text, name, '')
        start, end = f'<!-- {name}:start -->', f'<!-- {name}:end -->'
        if start not in text:
            continue
        before, remainder = text.split(start, 1)
        content, after = remainder.split(end, 1)
        if (before and not before.endswith('\n')) or (after and not after.startswith(('\n', '\r\n'))):
            raise ValueError(f'Managed markers must occupy their own lines: {name}')
        if not content.startswith(('\n', '\r\n')) or not content.endswith('\n'):
            raise ValueError(f'Managed markers must occupy their own lines: {name}')
        if re.search(r'<!--\s*[\w-]+:(?:start|end)\b', content):
            raise ValueError(f'Nested managed section: {name}')
        text = before + (after[2:] if after.startswith('\r\n') else after[1:] if after.startswith('\n') else after)
    return text


def template(name: str, home: Path, context_root: Path | None = None) -> str:
    return ((TEMPLATES / name).read_text().replace('{{HOME}}', str(home))
            .replace('{{CONTEXT_ROOT}}', str(context_root or home / 'context')))


def retire_conductor_prompt(home: Path, context_root: Path, runtime: Path) -> FileWrite | None:
    path = home / '.conductor/settings.toml'
    previous = runtime / 'templates/conductor-prompt.txt'
    if linked_path(path) or linked_path(previous) or not path.is_file() or not previous.is_file():
        return None
    start, end = '<!-- agent-context-workflow:start -->', '<!-- agent-context-workflow:end -->'
    try:
        source = existing_text(previous)
        if '{{HOME}}' not in source:
            return None
        manifest = json.loads(existing_text(runtime / 'installation.json'))
        if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
            return None
        if manifest['files'].get('templates/conductor-prompt.txt') != digest(source):
            return None
        original = existing_text(path)
        if start not in original:
            return None
        document = tomlkit.parse(original)
        prompts = document.get('prompts')
        general = prompts.get('general') if isinstance(prompts, Table) else None
        if not isinstance(general, str) or general.count(start) != 1 or general.count(end) != 1:
            return None
        expected = source.replace('{{HOME}}', str(home)).replace('{{CONTEXT_ROOT}}', str(context_root))
        if f'{start}\n{expected.rstrip()}\n{end}' not in general:
            return None
        prompts['general'] = remove_sections(general, ['agent-context-workflow'])
    except (OSError, ValueError, tomlkit.exceptions.TOMLKitError):
        return None
    return FileWrite(path=path, text=tomlkit.dumps(document), mode=path.stat().st_mode & 0o777)


def table_at(document: TOMLDocument | Table, key: str) -> Table:
    if key not in document:
        document[key] = tomlkit.table()
    value = document[key]
    if not isinstance(value, Table):
        raise ValueError(f'Expected TOML table: {key}')
    return value


def shared_instruction_link(path: Path, canonical: list[Path]) -> bool:
    # A client file that links to another managed instruction file shares its section.
    return (path.is_symlink() and not linked_path(path.parent)
            and path.resolve() in {target.resolve() for target in canonical})


def merge_hooks(document: JsonDocument, client: Literal['claude', 'codex'], cli: Path) -> JsonDocument:
    result = document.model_copy(deep=True)
    hooks = result.root.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Hooks must be a JSON object')
    command = f'{shlex.quote(str(cli))} hook --client {client}'
    for event in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'Stop'):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f'{event} hooks must be a list')
        preserved: list[JsonValue] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('hooks'), list):
                raise ValueError(f'Invalid {event} hook group')
            handlers = entry['hooks']
            remaining = [handler for handler in handlers if not isinstance(handler, dict) or handler.get('command') != command]
            if len(remaining) == len(handlers):
                preserved.append(entry)
            elif remaining:
                preserved.append({**entry, 'hooks': remaining})
        handler: dict[str, JsonValue] = {'type': 'command', 'command': command, 'timeout': 10}
        if client == 'codex' and event in {'SessionStart', 'UserPromptSubmit'}:
            handler['additionalContextLimit'] = 5000
        hooks[event] = [*preserved, {'hooks': [handler]}]
    return result


def build_plan(home: Path) -> InstallPlan:
    home = home.expanduser().resolve()
    runtime = home / '.local/share/agent-context'
    cli = home / '.local/bin/agent-context'
    config_path = home / '.config/agent-context/settings.json'
    settings_text = existing_text(config_path)
    settings = Settings.model_validate_json(settings_text) if settings_text else Settings(context_root=home / 'context')
    context_root = settings.context_root
    if settings.refresh.resource_root is None:
        settings.refresh.resource_root = home / '.context-refresh'
    files: list[FileWrite] = []
    files.append(FileWrite(path=config_path, text=settings.model_dump_json(indent=2) + '\n'))
    for source in sorted(Path(__file__).parent.glob('*.py')):
        if not source.name.startswith('test_'):
            files.append(FileWrite(path=runtime / source.name, text=source.read_text()))
    for source in sorted(TEMPLATES.iterdir()):
        if source.is_file():
            files.append(FileWrite(path=runtime / 'templates' / source.name, text=source.read_text()))
    revision = subprocess.run(['git', '-C', str(TEMPLATES.parent), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5)
    source_status = subprocess.run(['git', '-C', str(TEMPLATES.parent), 'status', '--porcelain'], capture_output=True, text=True, timeout=5)
    manifest = {'revision': revision.stdout.strip() if revision.returncode == 0 else '',
                'source_dirty': bool(source_status.stdout.strip()),
                'files': {str(write.path.relative_to(runtime)): digest(write.text) for write in files if write.path.is_relative_to(runtime)}}
    files.append(FileWrite(path=runtime / 'installation.json', text=json.dumps(manifest, indent=2) + '\n'))
    launcher = ('#!/bin/sh\nset -eu\numask 077\n'
                f'exec {shlex.quote(str(runtime / ".venv/bin/python"))} {shlex.quote(str(runtime / "agent_context.py"))} --settings {shlex.quote(str(config_path))} "$@"\n')
    files.append(FileWrite(path=cli, text=launcher, mode=0o700))

    claude_path = home / '.claude/settings.json'
    claude = merge_hooks(read_json(claude_path), 'claude', cli)
    files.append(FileWrite(path=claude_path, text=claude.model_dump_json(indent=2) + '\n'))

    codex_path = home / '.codex/config.toml'
    codex = tomlkit.parse(existing_text(codex_path))
    table_at(codex, 'features')['hooks'] = True
    files.append(FileWrite(path=codex_path, text=tomlkit.dumps(codex)))
    hooks_path = home / '.codex/hooks.json'
    codex_hooks = merge_hooks(read_json(hooks_path), 'codex', cli)
    files.append(FileWrite(path=hooks_path, text=codex_hooks.model_dump_json(indent=2) + '\n'))

    retired_prompt = retire_conductor_prompt(home, context_root, runtime)
    if retired_prompt:
        files.append(retired_prompt)

    instruction_paths = [home / relative for relative in ('.agents/AGENTS.md', '.codex/AGENTS.md', '.claude/CLAUDE.md')]
    canonical_instructions = [path for path in instruction_paths if not linked_path(path)]
    for global_path in instruction_paths:
        if shared_instruction_link(global_path, canonical_instructions):
            continue
        global_text = managed_section(existing_text(global_path), 'agent-context-workflow', template('global-instructions.txt', home, context_root))
        files.append(FileWrite(path=global_path, text=global_text))
    skill_path = home / '.claude/skills/context-workflow/SKILL.md'
    files.append(FileWrite(path=skill_path, text=template('workflow-skill.md', home, context_root)))
    project_path = home / '.claude/skills/project-context/SKILL.md'
    if not project_path.exists():
        files.append(FileWrite(path=project_path, text=template('project-skill.md', home, context_root)))

    personal_path = context_root / 'local/personal.md'
    if not personal_path.exists():
        files.append(FileWrite(path=personal_path, text=template('personal-kernel.md', home, context_root)))
    refresh_root = settings.refresh.resource_root
    resources = [('refresh-prompt.md', 'prompt.md'), ('gatherer-common.md', 'agents/common.md')]
    resources.extend((f'gatherer-{provider}.md', f'agents/{provider}.md') for provider in ('linear', 'notion', 'slack', 'granola'))
    for source, target in resources:
        path = refresh_root / target
        if not path.exists():
            files.append(FileWrite(path=path, text=template(source, home, context_root)))
    shell = ('#!/bin/sh\nset -eu\numask 077\n'
             f'export PATH={shlex.quote(str(home / ".local/bin"))}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n'
             'unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SSE_PORT\n'
             f'exec {shlex.quote(str(runtime / ".venv/bin/python"))} {shlex.quote(str(runtime / "refresh_context.py"))} --settings {shlex.quote(str(config_path))} "$@"\n')
    files.append(FileWrite(path=refresh_root / 'refresh.sh', text=shell, mode=0o700))
    plist = {
        'Label': MAINTENANCE_LABEL,
        'ProgramArguments': [str(cli), 'cleanup'],
        'StartCalendarInterval': {'Hour': 4, 'Minute': 15},
        'RunAtLoad': True,
        'StandardOutPath': str(context_root / 'local/maintenance.out'),
        'StandardErrorPath': str(context_root / 'local/maintenance.err'),
        'EnvironmentVariables': {'PATH': f'{home}/.local/bin:/opt/homebrew/bin:/usr/bin:/bin'},
    }
    files.append(FileWrite(path=home / f'Library/LaunchAgents/{MAINTENANCE_LABEL}.plist', text=plistlib.dumps(plist).decode()))
    links = [LinkWrite(path=home / '.agents/skills/context-workflow', target='../../.claude/skills/context-workflow')]
    project_alias = home / '.agents/skills/project-context'
    if not project_alias.exists() and not project_alias.is_symlink():
        links.append(LinkWrite(path=project_alias, target='../../.claude/skills/project-context'))
    for write in files:
        existing_text(write.path)
    for link in links:
        if linked_path(link.path.parent):
            raise ValueError(f'Skill parent must be a canonical directory: {link.path.parent}')
        if link.path.exists() and not link.path.is_symlink():
            raise ValueError(f'Preserve existing skill directory: {link.path}')
        if link.path.is_symlink() and os.readlink(link.path) != link.target:
            raise ValueError(f'Preserve existing skill alias: {link.path}')
    return InstallPlan(files=files, links=links, settings=settings)


def load_seed(path: Path | None) -> PreferenceSeed | None:
    if path is None:
        return None
    seed = PreferenceSeed.model_validate_json(existing_text(path))
    if len(seed.prompt) > CAPTURE_CHARS:
        raise ValueError('Preference seed exceeds capture budget')
    keys = {(memory.scope, memory.key) for memory in seed.memories}
    if len(keys) != len(seed.memories):
        raise ValueError('Preference seed contains duplicate keys')
    direct = captured_user_text(Capture(run_id='seed', prompt=clean(seed.prompt, CAPTURE_CHARS)))
    for memory in seed.memories:
        if memory.kind != 'preference' or memory.evidence not in direct:
            raise ValueError('Imported preferences need literal user evidence')
    return seed


def import_preferences(store: Store, marker: Path, seed: PreferenceSeed | None) -> int:
    if seed is None:
        return 0
    existing_text(marker)
    imported = 0
    with store.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT payload FROM preference_import WHERE id=?', (IMPORT_KEY,)).fetchone()
        if row:
            receipt = ImportReceipt.model_validate_json(row[0])
        else:
            import_id = 'initial-preferences-' + uuid4().hex[:12]
            capture = Capture(run_id=import_id, prompt=clean(seed.prompt, CAPTURE_CHARS), preference_import=True)
            existing = [Memory.model_validate_json(row[0]) for row in connection.execute('SELECT payload FROM memory')]
            keys = {(memory.scope, memory.key) for memory in existing}
            contents = {(memory.scope, memory.kind, memory.text) for memory in existing}
            memories = [memory.model_copy(update={'run_id': import_id, 'source': f'session:{import_id}'})
                        for memory in seed.memories if (memory.scope, memory.key) not in keys
                        and (memory.scope, memory.kind, memory.text) not in contents]
            for memory in memories:
                if memory.kind != 'preference':
                    raise ValueError('Initial imports accept only standing preferences')
                validate_memory_capture(memory, capture)
            persist_memories(connection, memories, store.settings.max_memory)
            connection.execute('INSERT INTO capture VALUES (?, ?, ?)', (capture.run_id, capture.created, capture.model_dump_json()))
            connection.execute('DELETE FROM capture WHERE run_id IN (SELECT run_id FROM capture ORDER BY created DESC LIMIT -1 OFFSET ?)', (store.settings.max_capture,))
            for cooldown in seed.cooldowns:
                connection.execute('INSERT OR IGNORE INTO cooldown VALUES (?, ?, ?)', (cooldown.identity, cooldown.until, cooldown.model_dump_json()))
            imported = len(memories)
            receipt = ImportReceipt(import_id=import_id, imported_preferences=imported, completed_at=time.time())
            connection.execute('INSERT INTO preference_import VALUES (?, ?)', (IMPORT_KEY, receipt.model_dump_json()))
    atomic_text(marker, receipt.model_dump_json(indent=2) + '\n')
    return imported


def adviser_notes(settings: Settings) -> list[str]:
    """Say which saved adviser models no longer match the toolkit defaults."""
    notes: list[str] = []
    for provider in settings.providers:
        default = ADVISER_DEFAULTS.get(provider.name)
        if default and provider.model not in (FOLLOW_DEFAULT, default.model):
            notes.append(f'The {provider.name} adviser is pinned to {provider.model}; the default is now {default.model}. '
                         f'Run "agent-context adviser reset {provider.name}" to follow defaults.')
    return notes


def maintenance_command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=10)


def loaded_maintenance(home: Path) -> set[str]:
    if home != Path.home().resolve():
        raise ValueError('Launch agents can only be activated for the current home')
    domain = f'gui/{os.getuid()}'
    return {label for label in (MAINTENANCE_LABEL,)
            if maintenance_command('print', f'{domain}/{label}').returncode == 0}


def require_maintenance(*args: str) -> None:
    result = maintenance_command(*args)
    if result.returncode:
        raise RuntimeError(f'Maintenance {args[0]} failed (exit {result.returncode})')


def restore_maintenance(home: Path, previously_loaded: set[str]) -> None:
    current = loaded_maintenance(home)
    domain = f'gui/{os.getuid()}'
    for label in (MAINTENANCE_LABEL,):
        if label in current:
            require_maintenance('bootout', f'{domain}/{label}')
        if label in previously_loaded:
            require_maintenance('bootstrap', domain, str(home / f'Library/LaunchAgents/{label}.plist'))


def activate_maintenance(home: Path, *, changed: bool = False) -> str:
    loaded = loaded_maintenance(home)
    domain = f'gui/{os.getuid()}'
    status = 'already loaded'
    if MAINTENANCE_LABEL not in loaded or changed:
        if MAINTENANCE_LABEL in loaded:
            require_maintenance('bootout', f'{domain}/{MAINTENANCE_LABEL}')
        require_maintenance('bootstrap', domain, str(home / f'Library/LaunchAgents/{MAINTENANCE_LABEL}.plist'))
        status = 'loaded'
    return status


def install(home: Path = HOME_DIR, *, seed_path: Path | None = None, activate: bool = False) -> InstallResult:
    home = home.expanduser().resolve()
    marker = home / '.config/agent-context/preference-import.json'
    with lock(home / '.config/agent-context/install.lock'):
        plan = build_plan(home)
        seed = load_seed(seed_path)
        existing_text(marker)
        if seed and len(seed.memories) > plan.settings.max_memory:
            raise ValueError('Preference seed exceeds memory budget')
        changed = [write for write in plan.files if not write.path.exists() or existing_text(write.path) != write.text or write.path.stat().st_mode & 0o777 != write.mode]
        new_links = [link for link in plan.links if not link.path.is_symlink()]
        destination = home / '.config/agent-context/backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:6])
        previously_loaded = loaded_maintenance(home) if activate else set()
        paths = [write.path for write in changed] + [link.path for link in new_links]
        if seed and not marker.exists():
            paths.extend([plan.settings.context_root / 'local/state.sqlite3', marker])
        receipt = backup_paths(paths, destination) if paths else None
        activation_started = False
        maintenance = 'installed; activation not requested'
        try:
            for write in changed:
                atomic_text(write.path, write.text)
                write.path.chmod(write.mode)
            for link in new_links:
                link.path.parent.mkdir(parents=True, exist_ok=True)
                link.path.symlink_to(link.target)
            store = Store(plan.settings)
            imported = import_preferences(store, marker, seed)
            store.flush_outbox()
            store.prune()
            if activate:
                activation_started = True
                maintenance = activate_maintenance(home, changed=any(write.path.name == f'{MAINTENANCE_LABEL}.plist' for write in changed))
        except Exception:
            for record in reversed(receipt.paths if receipt else []):
                if Path(record.path).name == 'state.sqlite3':
                    continue
                path = Path(record.path)
                if record.link_target is not None:
                    if path.is_symlink():
                        path.unlink()
                    path.symlink_to(record.link_target)
                elif record.backup:
                    shutil.copy2(record.backup, path)
                    if record.mode is not None:
                        path.chmod(record.mode)
                elif not record.existed:
                    path.unlink(missing_ok=True)
            if activation_started:
                restore_maintenance(home, previously_loaded)
            raise
        return InstallResult(backup_directory=str(destination) if receipt else '', changed_files=[str(path) for path in paths],
                             imported_preferences=imported, maintenance=maintenance, notes=adviser_notes(plan.settings))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--home', type=Path, default=HOME_DIR)
    parser.add_argument('--seed', type=Path)
    parser.add_argument('--activate', action='store_true')
    parser.add_argument('--brief', action='store_true')
    arguments = parser.parse_args()
    if not arguments.apply:
        plan = build_plan(arguments.home)
        load_seed(arguments.seed)
        print(json.dumps({'validated_files': [str(write.path) for write in plan.files], 'skill_aliases': [str(link.path) for link in plan.links]}, indent=2))
        return
    result = install(arguments.home, seed_path=arguments.seed, activate=arguments.activate)
    if arguments.brief:
        cli = shlex.quote(str(arguments.home / '.local/bin/agent-context'))
        print('Agent Setup installed.')
        print(f'To register a project, run {cli} project add my-project /path/to/project --brief')
        for note in result.notes:
            print(note)
    else:
        print(result.model_dump_json(indent=2))


if __name__ == '__main__':
    main()
