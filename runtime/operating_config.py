"""Inspect effective operating settings and edit one validated leaf at a time."""

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from context_store import DEFAULT_COOLDOWNS, EFFORTS, MEMORY_KEY_PATTERN, RefreshSettings, RefreshSource, Settings, TopicWords, atomic_text, lock
from refresh_health import SOURCE_NAMES
from team_runtime import ADVISER_DEFAULTS, FOLLOW_DEFAULT, resolve_provider


class ConfigError(ValueError):
    """An actionable message containing no user-supplied values."""


class Option(BaseModel):
    value: str | int | float | bool | list[str] | None
    default: str | int | float | bool | list[str] | None
    source: str
    overridden: bool
    value_type: str = "string"
    bounds: dict = Field(default_factory=dict)


def setting_parts(key: str) -> list[str]:
    return key.split(".", 2) if key.startswith("lesson_topics.") else key.split(".")


def option_catalog(settings: Settings, raw: dict, requested: str | None = None) -> dict[str, Option]:
    entries = {}
    def add(key, value, default, schema):
        stored = raw
        for part in setting_parts(key):
            stored = stored.get(part) if isinstance(stored, dict) else None
        entries[key] = Option(value=value, default=default, overridden=value != default,
                              value_type=schema.get("type", "boolean" if isinstance(default, bool) else "string"),
                              source="settings.json" if stored is not None else "toolkit default",
                              bounds={k: v for k, v in schema.items() if k in {
                                  "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern", "enum",
                                  "minItems", "maxItems", "items"}})

    schema = Settings.model_json_schema()["properties"]
    lesson_keys = {(scope, key) for scope, rules in settings.lesson_topics.items() for key in rules}
    parts = setting_parts(requested or "")
    if len(parts) == 3 and parts[0] == "lesson_topics":
        _, scope, key = parts
        if (scope == "personal" or settings.project(scope) or scope in settings.lesson_topics) and re.fullmatch(MEMORY_KEY_PATTERN, key):
            lesson_keys.add((scope, key))
    for scope, key in sorted(lesson_keys):
        value = settings.lesson_topics.get(scope, {}).get(key)
        add(f"lesson_topics.{scope}.{key}", value, None, TypeAdapter(TopicWords).json_schema())
    for key, prop in schema.items():
        if prop.get("type") in {"integer", "number"}:
            add(key, getattr(settings, key), prop["default"], prop)
    for provider in ADVISER_DEFAULTS:
        add(f"adviser_concurrency.{provider}", settings.adviser_concurrency.get(provider, 1), 1,
            schema["adviser_concurrency"]["additionalProperties"])
    for kind, default in DEFAULT_COOLDOWNS.items():
        add(f"adviser_cooldowns.{kind}", settings.adviser_cooldowns.get(kind, default), default,
            schema["adviser_cooldowns"]["additionalProperties"])
    refresh = settings.refresh
    for key, prop in RefreshSettings.model_json_schema()["properties"].items():
        if prop.get("type") in {"integer", "number", "boolean", "string"} or key == "project":
            add(f"refresh.{key}", getattr(refresh, key), prop.get("default"), prop)
    for name in sorted(SOURCE_NAMES):
        source = refresh.sources.get(name, RefreshSource())
        add(f"refresh.sources.{name}.enabled", source.enabled, True, {})
        if name != "github":
            default = "linear-server" if name == "linear" else name
            add(f"refresh.sources.{name}.mcp_server", source.mcp_server or default, default,
                {"pattern": "^[A-Za-z0-9_-]{1,80}$"})
    return entries


def show_config(path: Path, key: str | None = None, project: str | None = None) -> dict:
    raw = json.loads(path.read_text())
    settings = Settings.model_validate(raw)
    catalog = option_catalog(settings, raw, key)
    if key:
        if key not in catalog:
            raise ConfigError("Unknown operating setting. Run config show for the supported keys.")
        return {key: catalog[key].model_dump()}
    scope = settings.project(project) if project else None
    if project and scope is None:
        raise ConfigError("Choose a registered project for the effective adviser settings.")
    advisers = {}
    for configured in settings.project_providers(scope):
        if configured.name not in ADVISER_DEFAULTS:
            continue
        effective = resolve_provider(configured)
        override = scope.adviser_overrides.get(configured.name) if scope else None
        fields = {}
        for field in ("model", "effort"):
            origin = "toolkit default" if getattr(configured, field) == FOLLOW_DEFAULT else "machine"
            if override and getattr(override, field) is not None:
                origin = "project" if getattr(override, field) != FOLLOW_DEFAULT else "toolkit default selected by project"
            default = getattr(ADVISER_DEFAULTS[configured.name], field)
            fields[field] = Option(value=getattr(effective, field), default=default, source=origin,
                                   overridden=getattr(effective, field) != default,
                                   bounds={"enum": list(EFFORTS)} if field == "effort" else {}).model_dump()
        advisers[configured.name] = fields
    result = {"settings": {key: value.model_dump() for key, value in catalog.items()}, "advisers": advisers}
    if scope:
        result["adviser_access"] = scope.adviser_access
    return result


def change_config(path: Path, key: str, argument: str | None) -> dict:
    with lock(path.parent / "install.lock"):
        raw = json.loads(path.read_text())
        settings = Settings.model_validate(raw)
        option = option_catalog(settings, raw, key).get(key)
        if option is None:
            raise ConfigError("Unknown operating setting. See config show. Use adviser set for model or effort.")
        if argument is None and option.source == "toolkit default":
            return {key: option.model_dump()}
        parent = raw
        *ancestors, leaf = setting_parts(key)
        for part in ancestors:
            parent = parent.setdefault(part, {})
        if argument is None:
            parent.pop(leaf, None)
        else:
            try:
                if option.value_type in {"integer", "number", "boolean", "array"}:
                    expected = {"integer": int, "number": float, "boolean": bool, "array": list}[option.value_type]
                    value = json.loads(argument)
                    if expected is float and type(value) is int:
                        value = float(value)
                    if type(value) is not expected:
                        raise ValueError
                else:
                    value = argument
                if key == "refresh.github_repo" and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
                    raise ValueError
                if key == "refresh.github_user" and not re.fullmatch(r"[A-Za-z0-9-]+", value):
                    raise ValueError
                if key == "refresh.project" and settings.project(value) is None:
                    raise ValueError
                parent[leaf] = value
            except (ValueError, TypeError):
                raise ConfigError("Invalid value. Check the setting type, bounds, or registered project.") from None
        try:
            Settings.model_validate(raw)
        except ValueError:
            if option.value_type == "array":
                raise ConfigError("A lesson rule needs one to 32 lowercase topic words. Check config show for the character bounds.") from None
            raise ConfigError("Setting rejected. Check config show for bounds and use a distinct MCP server per enabled source.") from None
        atomic_text(path, json.dumps(raw, indent=2, allow_nan=False) + "\n")
        # Return the change observed under this lock, including its effective default.
        return show_config(path, key)
