"""The public config commands edit only the requested validated operating settings."""

import concurrent.futures
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_context import main
from context_store import atomic_text, load_settings
from operating_config import ConfigError, change_config, show_config


class OperatingConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "settings.json"
        atomic_text(self.path, json.dumps({"context_root": str(self.root / "store"), "future_option": ["preserve"],
            "providers": [{"name": "codex", "command": ["private-executable"], "model": "machine-model", "effort": "max"}],
            "projects": [{"slug": "sample", "adviser_overrides": {"codex": {"effort": "high"}}}]}))

    def test_show_describes_bounds_and_project_precedence_without_command_or_private_extras(self):
        result = show_config(self.path, project="sample")
        item = result["settings"]["refresh.max_window_days"]
        self.assertEqual((item["value"], item["source"], item["bounds"]), (7, "toolkit default", {"minimum": 1, "maximum": 90}))
        self.assertEqual(result["advisers"]["codex"]["effort"]["source"], "project")
        self.assertEqual(result["advisers"]["codex"]["model"]["source"], "machine")
        self.assertNotIn("private-executable", json.dumps(result))
        self.assertNotIn("future_option", json.dumps(result))

    def test_set_reset_validate_preserve_unknown_keys_and_do_not_lose_concurrent_updates(self):
        values = {"adviser_concurrency.codex": "2", "refresh.incremental_budget_usd": "1.5",
                  "refresh.sources.notion.enabled": "false", "adviser_cooldowns.auth": "600"}
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(change_config, self.path, key, value) for key, value in values.items()]
            for future in futures:
                future.result()
        self.assertEqual(load_settings(self.path).adviser_concurrency, {"codex": 2})
        self.assertNotIn("notion", load_settings(self.path).refresh.enabled_sources())
        self.assertEqual(show_config(self.path, "refresh.incremental_budget_usd")["refresh.incremental_budget_usd"]["value"], 1.5)
        before = self.path.read_bytes()
        for key, val in (("refresh.max_window_days", "0"), ("adviser_concurrency.codex", "true"),
                         ("refresh.sources.fake.enabled", "true"), ("providers", "[]"), ("refresh.project", "missing")):
            with self.assertRaises(ConfigError):
                change_config(self.path, key, val)
        self.assertEqual(before, self.path.read_bytes())
        changed = change_config(self.path, "adviser_concurrency.codex", None)["adviser_concurrency.codex"]
        self.assertEqual((changed["value"], changed["source"], changed["overridden"]), (1, "toolkit default", False))
        self.assertEqual(json.loads(self.path.read_text())["future_option"], ["preserve"])

    def test_command_entry(self):
        output = io.StringIO()
        with patch("sys.argv", ["agent-context", "--settings", str(self.path), "config", "set", "adviser_wait", "300"]), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())["adviser_wait"]["value"], 300)

    def test_scoped_lesson_topics_support_dotted_keys_and_atomic_reset(self):
        key = "lesson_topics.sample.meter.care"
        self.assertEqual(show_config(self.path, key)[key]["source"], "toolkit default")
        change_config(self.path, key, '["calibration","gauge"]')
        self.assertEqual(load_settings(self.path).lesson_topics, {"sample": {"meter.care": ["calibration", "gauge"]}})
        self.assertEqual(show_config(self.path)["settings"][key]["source"], "settings.json")
        before = self.path.read_bytes()
        for name, value in ((key, '[""]'), (key, "[]"), (key, '"gauge"'), ("lesson_topics.unknown.x", '["gauge"]')):
            with self.assertRaises(ConfigError):
                change_config(self.path, name, value)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(change_config(self.path, key, None)[key]["value"])
        self.assertNotIn("meter.care", load_settings(self.path).lesson_topics.get("sample", {}))
