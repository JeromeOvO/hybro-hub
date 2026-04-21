"""Tests for hub CLI agent start command.

Covers: _CLI_ADAPTERS registration, config assembly for claude-code/codex,
working_dir defaulting, custom binary paths, and adapter loading.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from hub.cli import main, _CLI_ADAPTERS


@pytest.fixture(autouse=True)
def mock_a2a_adapter():
    """Mock the a2a_adapter package so local imports inside agent_start() succeed.

    agent_start() does `from a2a_adapter import serve_agent` and
    `from a2a_adapter.loader import load_adapter` inside a try/except.
    Since a2a_adapter is not installed in hybro-hub's dev deps, we inject
    fake modules into sys.modules.
    """
    mock_serve = MagicMock()
    mock_load = MagicMock(return_value=MagicMock())

    mod_a2a = ModuleType("a2a_adapter")
    mod_a2a.serve_agent = mock_serve

    mod_loader = ModuleType("a2a_adapter.loader")
    mod_loader.load_adapter = mock_load

    saved = {}
    for name in ("a2a_adapter", "a2a_adapter.loader"):
        saved[name] = sys.modules.get(name)

    sys.modules["a2a_adapter"] = mod_a2a
    sys.modules["a2a_adapter.loader"] = mod_loader

    yield {"load_adapter": mock_load, "serve_agent": mock_serve}

    # Restore original state
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


# ──── Test: _CLI_ADAPTERS contains new entries ────


class TestCLIAdapters:
    def test_claude_code_registered(self):
        assert "claude-code" in _CLI_ADAPTERS

    def test_codex_registered(self):
        assert "codex" in _CLI_ADAPTERS

    def test_all_adapters_have_required_keys(self):
        for name, info in _CLI_ADAPTERS.items():
            assert "description" in info, f"{name} missing description"
            assert "install_hint" in info, f"{name} missing install_hint"


# ──── Test: agent start claude-code ────


class TestAgentStartClaudeCode:
    def test_claude_code_default_working_dir(self, tmp_path, mock_a2a_adapter):
        """claude-code without --working-dir uses cwd."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["agent", "start", "claude-code"])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["adapter"] == "claude-code"
        assert "working_dir" in config
        assert config["name"] == "Claude Code Agent"
        mock_a2a_adapter["serve_agent"].assert_called_once()

    def test_claude_code_explicit_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", str(tmp_path),
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["working_dir"] == str(tmp_path)

    def test_claude_code_custom_path(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", str(tmp_path),
            "--claude-path", "/opt/bin/claude",
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["claude_path"] == "/opt/bin/claude"

    def test_claude_code_custom_name_and_port(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", str(tmp_path),
            "--name", "My Claude",
            "--port", "9010",
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["name"] == "My Claude"
        _, kwargs = mock_a2a_adapter["serve_agent"].call_args
        assert kwargs["port"] == 9010

    def test_claude_code_with_timeout(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", str(tmp_path),
            "--timeout", "300",
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["timeout"] == 300

    def test_claude_code_output_shows_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", str(tmp_path),
        ])

        assert result.exit_code == 0
        assert "Working dir:" in result.output
        assert str(tmp_path) in result.output

    def test_claude_code_no_claude_path_in_config(self, tmp_path, mock_a2a_adapter):
        """When --claude-path is not given, claude_path should not be in config."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", str(tmp_path),
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert "claude_path" not in config


# ──── Test: agent start codex ────


class TestAgentStartCodex:
    def test_codex_default_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["agent", "start", "codex"])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["adapter"] == "codex"
        assert config["name"] == "Codex Agent"
        mock_a2a_adapter["serve_agent"].assert_called_once()

    def test_codex_explicit_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "codex",
            "--working-dir", str(tmp_path),
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["working_dir"] == str(tmp_path)

    def test_codex_custom_path(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "codex",
            "--working-dir", str(tmp_path),
            "--codex-path", "/opt/bin/codex",
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["codex_path"] == "/opt/bin/codex"

    def test_codex_with_timeout(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "codex",
            "--working-dir", str(tmp_path),
            "--timeout", "600",
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert config["timeout"] == 600

    def test_codex_output_shows_working_dir(self, tmp_path, mock_a2a_adapter):
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "codex",
            "--working-dir", str(tmp_path),
        ])

        assert result.exit_code == 0
        assert "Working dir:" in result.output

    def test_codex_no_codex_path_in_config(self, tmp_path, mock_a2a_adapter):
        """When --codex-path is not given, codex_path should not be in config."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "codex",
            "--working-dir", str(tmp_path),
        ])

        assert result.exit_code == 0, result.output
        config = mock_a2a_adapter["load_adapter"].call_args[0][0]
        assert "codex_path" not in config


# ──── Test: working_dir validation ────


class TestWorkingDirValidation:
    def test_claude_code_bad_working_dir(self, tmp_path, mock_a2a_adapter):
        """claude-code with nonexistent --working-dir fails before load_adapter."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", str(tmp_path / "nonexistent"),
        ])
        assert result.exit_code != 0
        assert "Working directory does not exist" in result.output
        mock_a2a_adapter["load_adapter"].assert_not_called()

    def test_codex_bad_working_dir(self, tmp_path, mock_a2a_adapter):
        """codex with nonexistent --working-dir fails before load_adapter."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "codex",
            "--working-dir", str(tmp_path / "nonexistent"),
        ])
        assert result.exit_code != 0
        assert "Working directory does not exist" in result.output
        mock_a2a_adapter["load_adapter"].assert_not_called()


# ──── Test: adapter loading errors ────


class TestAdapterLoadingErrors:
    def test_import_error(self, mock_a2a_adapter):
        mock_a2a_adapter["load_adapter"].side_effect = ImportError("no module")
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "claude-code",
            "--working-dir", "/tmp",
        ])
        assert result.exit_code != 0
        assert "Error" in result.output

    def test_value_error(self, mock_a2a_adapter):
        mock_a2a_adapter["load_adapter"].side_effect = ValueError("bad config")
        runner = CliRunner()
        result = runner.invoke(main, [
            "agent", "start", "codex",
            "--working-dir", "/tmp",
        ])
        assert result.exit_code != 0
        assert "Error" in result.output


# ──── Test: invalid adapter type rejected ────


class TestInvalidAdapter:
    def test_unknown_adapter_rejected(self):
        runner = CliRunner()
        result = runner.invoke(main, ["agent", "start", "unknown-adapter"])
        assert result.exit_code != 0
