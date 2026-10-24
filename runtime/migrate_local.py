from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from tomlkit.items import Table

from context_store import atomic_text
from install_local import InstallReceipt, backup_paths, existing_text, managed_section

Digest = Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
Section = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')]
Nonempty = Annotated[str, StringConstraints(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Target(StrictModel):
    path: str
    expected_sha256: Digest


class HookTarget(Target):
    commands: list[Nonempty] = Field(min_length=1)


class InstructionTarget(Target):
    sections: list[Section] = Field(min_length=1)


class TomlTarget(InstructionTarget):
    table: list[Nonempty] = Field(min_length=1)
    key: Nonempty


class Manifest(StrictModel):
    version: Literal[1]
    hook_files: list[HookTarget] = Field(default_factory=list)
    instruction_files: list[InstructionTarget] = Field(default_factory=list)
    toml_sections: list[TomlTarget] = Field(default_factory=list)


class SavedFile(StrictModel):
    path: str
    backup: str
    before_sha256: Digest
    after_sha256: Digest
    mode: int = Field(ge=0, le=0o777)


class Receipt(StrictModel):
    version: Literal[1] = 1
    backup_directory: str
    backup_manifest_sha256: Digest
    files: list[SavedFile] = Field(min_length=1)


@dataclass
class Change:
    path: Path
    before: str
    after: str
    mode: int


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def checked_path(value: str | Path, *, directory: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError(f'Expected an absolute nonsymlink path: {path}')
    if directory:
        if path.exists() and not path.is_dir():
            raise ValueError(f'Expected a directory: {path}')
    elif not path.is_file():
        raise ValueError(f'Expected an existing regular file: {path}')
    return path


def file_text(path: Path) -> str:
    return checked_path(path).read_bytes().decode('utf-8')


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def parse_json(text: str) -> object:
    return json.loads(text, object_pairs_hook=unique_object)


def remove_sections(text: str, sections: list[str]) -> str:
    if len(set(sections)) != len(sections):
        raise ValueError('Duplicate managed section selection')
    for name in sections:
        managed_section(text, name, '')  # Reuse the installer's ownership-marker validation.
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


def remove_hooks(text: str, commands: list[str]) -> str:
    document = parse_json(text)
    if not isinstance(document, dict) or not isinstance(document.get('hooks', {}), dict):
        raise ValueError('Expected a JSON object with a hooks object')
    changed = False
    for event, groups in document.get('hooks', {}).items():
        if not isinstance(groups, list):
            raise ValueError(f'Expected a hook group list: {event}')
        preserved = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError(f'Invalid hook group: {event}')
            handlers = group['hooks']
            remaining = [handler for handler in handlers if not (
                isinstance(handler, dict) and handler.get('type') == 'command' and handler.get('command') in commands
            )]
            removed = len(remaining) != len(handlers)
            changed |= removed
            if not removed or remaining:
                preserved.append({**group, 'hooks': remaining})
        document['hooks'][event] = preserved
    return json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + '\n' if changed else text


def build_changes(manifest_path: Path) -> tuple[list[Change], list[str]]:
    manifest = Manifest.model_validate(parse_json(existing_text(checked_path(manifest_path))))
    targets = [*manifest.hook_files, *manifest.instruction_files, *manifest.toml_sections]
    grouped: dict[Path, list[Target]] = {}
    for target in targets:
        path = checked_path(target.path)
        if isinstance(target, TomlTarget):
            valid = path.suffix == '.toml' and target.table[0] == 'prompts'
        elif isinstance(target, HookTarget):
            valid = path.suffix == '.json'
        else:
            valid = path.name in {'AGENTS.md', 'CLAUDE.md'}
        if not valid or path == manifest_path:
            raise ValueError(f'Unsupported migration target: {path}')
        grouped.setdefault(path, []).append(target)
    changes, unchanged = [], []
    for path, selections in grouped.items():
        if len({target.expected_sha256 for target in selections}) != 1:
            raise ValueError(f'Conflicting expected hashes: {path}')
        if len(selections) > 1 and not all(isinstance(target, TomlTarget) for target in selections):
            raise ValueError(f'Duplicate migration target: {path}')
        before = file_text(path)
        after = before
        document = tomlkit.parse(before) if isinstance(selections[0], TomlTarget) else None
        selected_fields: set[tuple[str, ...]] = set()
        for target in selections:
            if isinstance(target, HookTarget):
                after = remove_hooks(after, target.commands)
            elif isinstance(target, TomlTarget):
                field = (*target.table, target.key)
                if field in selected_fields:
                    raise ValueError(f'Duplicate TOML prompt selection: {path}')
                selected_fields.add(field)
                table = document
                for key in target.table:
                    table = table.get(key) if table is not None else None
                    if not isinstance(table, Table):
                        raise ValueError(f'Expected an existing TOML prompt table: {path}')
                original = table.get(target.key)
                if not isinstance(original, str):
                    raise ValueError(f'Expected an existing TOML prompt string: {path}')
                cleaned = remove_sections(original, target.sections)
                if cleaned != original:
                    table[target.key] = cleaned
                    after = tomlkit.dumps(document)
            else:
                after = remove_sections(after, target.sections)
        if after == before:
            unchanged.append(str(path))
        else:
            if digest(before) != selections[0].expected_sha256:
                raise ValueError(f'Source hash changed: {path}')
            changes.append(Change(path, before, after, path.stat().st_mode & 0o777))
    return changes, unchanged


def plan(manifest_path: Path) -> dict[str, object]:
    changes, unchanged = build_changes(checked_path(manifest_path))
    return {'changed_files': [str(change.path) for change in changes], 'unchanged_files': unchanged,
            'changes': [{'path': str(change.path), 'before_sha256': digest(change.before),
                         'after_sha256': digest(change.after)} for change in changes]}


def verify_source(change: Change, expected: str) -> None:
    if digest(file_text(change.path)) != digest(expected) or change.path.stat().st_mode & 0o777 != change.mode:
        raise ValueError(f'Source hash or permissions changed: {change.path}')


def transact(changes: list[Change]) -> None:
    for change in changes:
        verify_source(change, change.before)
    touched: list[Change] = []
    try:
        for change in changes:
            verify_source(change, change.before)
            touched.append(change)
            atomic_text(change.path, change.after)
            change.path.chmod(change.mode)
        for change in changes:
            verify_source(change, change.after)
    except Exception as error:
        failures = []
        for change in reversed(touched):
            try:
                current = file_text(change.path)
                if current != change.before:
                    if current != change.after:
                        raise ValueError('File changed during rollback')
                    atomic_text(change.path, change.before)
                change.path.chmod(change.mode)
            except Exception:
                failures.append(str(change.path))
        if failures:
            raise RuntimeError(f'Rollback incomplete; preserve backups for: {", ".join(failures)}') from error
        raise


def apply(manifest_path: Path, backup_root: Path) -> dict[str, object]:
    manifest_path = checked_path(manifest_path)
    manifest_hash = digest(file_text(manifest_path))
    changes, unchanged = build_changes(manifest_path)
    root = checked_path(backup_root, directory=True)
    if not changes:
        return {'changed_files': [], 'unchanged_files': unchanged, 'backup_directory': None, 'receipt_path': None}
    destination = root / ('migration-' + uuid4().hex)
    for change in changes:
        verify_source(change, change.before)
    saved = backup_paths([change.path for change in changes], destination)
    files = []
    for change, record in zip(changes, saved.paths, strict=True):
        if record.backup is None or digest(file_text(Path(record.backup))) != digest(change.before):
            raise ValueError(f'Source changed while backing up: {change.path}')
        files.append(SavedFile(path=str(change.path), backup=record.backup, mode=change.mode,
                               before_sha256=digest(change.before), after_sha256=digest(change.after)))
    receipt = Receipt(backup_directory=str(destination), files=files,
                      backup_manifest_sha256=digest(file_text(destination / 'manifest.json')))
    receipt_path = destination / 'migration.json'
    receipt_text = receipt.model_dump_json(indent=2) + '\n'
    atomic_text(receipt_path, receipt_text)
    atomic_text(destination / 'migration.sha256', digest(receipt_text) + '\n')
    if digest(file_text(manifest_path)) != manifest_hash:
        raise ValueError('Migration manifest changed during validation')
    transact(changes)
    return {'changed_files': [str(change.path) for change in changes], 'unchanged_files': unchanged,
            'backup_directory': str(destination), 'receipt_path': str(receipt_path)}


def restore(receipt_path: Path) -> dict[str, object]:
    receipt_path = checked_path(receipt_path)
    text = file_text(receipt_path)
    directory = receipt_path.parent
    if receipt_path.name != 'migration.json' or file_text(directory / 'migration.sha256').strip() != digest(text):
        raise ValueError('Migration receipt integrity check failed')
    receipt = Receipt.model_validate(parse_json(text))
    if checked_path(receipt.backup_directory, directory=True) != directory:
        raise ValueError('Backup directory does not match the receipt')
    saved_text = file_text(directory / 'manifest.json')
    if digest(saved_text) != receipt.backup_manifest_sha256:
        raise ValueError('Backup manifest integrity check failed')
    saved = InstallReceipt.model_validate(parse_json(saved_text))
    if saved.backup_directory != str(directory) or len(saved.paths) != len(receipt.files):
        raise ValueError('Backup manifest does not match the receipt')
    changes = []
    seen = set()
    for index, (record, file) in enumerate(zip(saved.paths, receipt.files, strict=True)):
        path, backup = checked_path(file.path), checked_path(file.backup)
        if (path in seen or backup != directory / f'{index:02d}-{path.name}' or path.is_relative_to(directory)
                or record.path != file.path or record.backup != file.backup or record.mode != file.mode
                or not record.existed or record.link_target is not None):
            raise ValueError('Invalid backup path or source record')
        seen.add(path)
        original, current = file_text(backup), file_text(path)
        if digest(original) != file.before_sha256 or digest(current) != file.after_sha256:
            raise ValueError(f'Backup or post-apply source hash changed: {path}')
        changes.append(Change(path, current, original, file.mode))
    transact(changes)
    return {'restored_files': [str(change.path) for change in changes]}


def main() -> None:
    parser = argparse.ArgumentParser(description='Preview explicit local migration; writes require --apply.')
    parser.add_argument('manifest', type=Path, nargs='?')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-root', type=Path)
    parser.add_argument('--restore', type=Path)
    arguments = parser.parse_args()
    if arguments.restore:
        if arguments.manifest or arguments.apply or arguments.backup_root:
            parser.error('--restore cannot be combined with migration arguments')
        result = restore(arguments.restore)
    elif not arguments.manifest or (arguments.apply and not arguments.backup_root):
        parser.error('A manifest is required; --apply also requires --backup-root')
    else:
        result = apply(arguments.manifest, arguments.backup_root) if arguments.apply else plan(arguments.manifest)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
