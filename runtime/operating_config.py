"""Inspect effective operating settings and edit one validated leaf at a time."""

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from context_store import DEFAULT_COOLDOWNS, EFFORTS, RefreshSettings, RefreshSource, Settings, atomic_text, lock
from refresh_health import SOURCE_NAMES
from team_runtime import ADVISER_DEFAULTS, FOLLOW_DEFAULT, resolve_provider


class ConfigError(ValueError):
    """An actionable message containing no user-supplied values."""


class Option(BaseModel):
    value: str | int | float | bool | None
    default: str | int | float | bool | None
    source: str
    overridden: bool
    value_type: str = "string"
    bounds: dict = Field(default_factory=dict)


def option_catalog(settings: Settings, raw: dict) -> dict[str, Option]:
    entries = {}
    def add(key, value, default, schema):
        stored = raw
        for part in key.split("."):
            stored = stored.get(part) if isinstance(stored, dict) else None
        entries[key] = Option(value=value, default=default, overridden=value != default,
                              value_type=schema.get("type", "boolean" if isinstance(default, bool) else "string"),
                              source="settings.json" if stored is not None else "toolkit default",
                              bounds={k: v for k, v in schema.items() if k in {
                                  "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern", "enum"}})

    schema = Settings.model_json_schema()["properties"]
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
    catalog = option_catalog(settings, raw)
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
    return {"settings": {key: value.model_dump() for key, value in catalog.items()}, "advisers": advisers}


def change_config(path: Path, key: str, argument: str | None) -> dict:
    with lock(path.parent / "install.lock"):
        raw = json.loads(path.read_text())
        settings = Settings.model_validate(raw)
        option = option_catalog(settings, raw).get(key)
        if option is None:
            raise ConfigError("Unknown operating setting. See config show. Use adviser set for model or effort.")
        if argument is None and option.source == "toolkit default":
            return {key: option.model_dump()}
        parent = raw
        *ancestors, leaf = key.split(".")
        for part in ancestors:
            parent = parent.setdefault(part, {})
        if argument is None:
            parent.pop(leaf, None)
        else:
            try:
                if option.value_type in {"integer", "number", "boolean"}:
                    expected = {"integer": int, "number": float, "boolean": bool}[option.value_type]
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
            raise ConfigError("Setting rejected. Check config show for bounds and use a distinct MCP server per enabled source.") from None
        atomic_text(path, json.dumps(raw, indent=2, allow_nan=False) + "\n")
        # Return the change observed under this lock, including its effective default.
        return show_config(path, key)
