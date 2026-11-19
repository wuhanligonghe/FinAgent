from contextlib import redirect_stdout
import io
import os
from unittest.mock import Mock, patch

from finagent.cli import main
from finagent.config import find_config, load_settings

from .support import ServiceCase


class StartupTests(ServiceCase):
    def test_financial_proxy_is_independent_of_model_proxy(self):
        config = self.root / "finagent.toml"
        config.write_text('', encoding="utf-8")
        (self.root / ".env").write_text("FINAGENT_MODEL_PROXY=http://127.0.0.1:17897\nFINAGENT_DATA_PROXY=http://127.0.0.1:9000\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(config)
            self.assertEqual(settings.data_proxy_url, "http://127.0.0.1:9000")
            self.assertEqual(settings.model.proxy_url, "http://127.0.0.1:17897")

    def test_model_proxy_is_loaded_from_dotenv_without_shell_proxy_variables(self):
        config = self.root / "finagent.toml"
        config.write_text('[model]\nname="synthetic"\n', encoding="utf-8")
        (self.root / ".env").write_text("FINAGENT_MODEL_PROXY=http://127.0.0.1:17897\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_settings(config).model.proxy_url, "http://127.0.0.1:17897")
        with patch.dict(os.environ, {"FINAGENT_MODEL_PROXY": "http://127.0.0.1:9999"}, clear=True):
            self.assertEqual(load_settings(config).model.proxy_url, "http://127.0.0.1:9999")

    def test_config_and_env_are_found_from_project_subdirectory(self):
        project = self.root / "project"
        nested = project / "src" / "finagent"
        nested.mkdir(parents=True)
        config = project / "finagent.toml"
        config.write_text('data_dir="runtime"\n[model]\nname="startup-fixture"\n', encoding="utf-8")
        (project / ".env").write_text("FINAGENT_API_KEY=synthetic-startup-key\n", encoding="utf-8")
        with patch("pathlib.Path.cwd", return_value=nested), patch.dict(os.environ, {}, clear=True):
            settings = load_settings()
            self.assertEqual(find_config(), config)
            self.assertEqual(settings.data_dir, project / "runtime")
            self.assertEqual(settings.model.api_key.get_secret_value(), "synthetic-startup-key")

    def test_nearest_config_wins_and_explicit_path_overrides_discovery(self):
        parent = self.root / "finagent.toml"
        nested = self.root / "nested"
        nested.mkdir()
        local = nested / "finagent.toml"
        for path in (parent, local):
            path.write_text("", encoding="utf-8")
        with patch("pathlib.Path.cwd", return_value=nested):
            self.assertEqual(find_config(), local)
            self.assertEqual(find_config(parent), parent)
            with self.assertRaises(ValueError):
                find_config(self.root / "missing.toml")

    def test_bare_command_opens_persistent_host_and_closes_resources(self):
        with patch("finagent.cli.load_settings", return_value=self.settings), \
             patch("finagent.cli.SessionHost") as host, \
             patch("finagent.cli.run_terminal", return_value=0) as terminal, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main([]), 0)
        terminal.assert_called_once()
        host.return_value.close.assert_called_once()
