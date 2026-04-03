"""Tests for headless (non-interactive) provider configuration."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConcreteProvider(BaseProvider):
    """Minimal concrete provider for testing the base no-op implementation."""

    def initialize(self) -> bool:
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        return self._status

    def get_idle_pattern_for_log(self) -> str:
        return r"\[test\]>"

    def extract_last_message_from_script(self, script_output: str) -> str:
        return ""

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        pass


# ---------------------------------------------------------------------------
# BaseProvider – default no-op
# ---------------------------------------------------------------------------


class TestBaseProviderConfigureHeadless:
    """BaseProvider.configure_headless() must be a no-op by default."""

    def test_configure_headless_is_noop(self, tmp_path):
        provider = _ConcreteProvider("t1", "s1", "w1")
        # Should not raise and should not write any files
        provider.configure_headless(tmp_path)
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# ClaudeCodeProvider
# ---------------------------------------------------------------------------


class TestClaudeCodeProviderConfigureHeadless:
    """Tests for ClaudeCodeProvider.configure_headless()."""

    def test_creates_claude_json_when_absent(self, tmp_path):
        """Creates ~/.claude/.claude.json with expected keys when file does not exist."""
        fake_home = tmp_path
        claude_dir = fake_home / ".claude"

        with (
            patch("cli_agent_orchestrator.providers.claude_code.Path.home", return_value=fake_home),
            patch(
                "cli_agent_orchestrator.providers.claude_code.subprocess.check_output",
                return_value=b"1.2.3 (claude-code)",
            ),
        ):
            provider = ClaudeCodeProvider("t1", "s1", "w1")
            provider.configure_headless(tmp_path / "workspace")

        claude_json = claude_dir / ".claude.json"
        assert claude_json.exists()
        data = json.loads(claude_json.read_text())
        assert data["hasCompletedOnboarding"] is True
        assert data["lastOnboardingVersion"] == "1.2.3"
        assert data["theme"] == "dark"

    def test_preserves_existing_keys(self, tmp_path):
        """Existing keys in .claude.json are preserved; headless keys are merged."""
        fake_home = tmp_path
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        existing = {"customKey": "customValue", "theme": "light"}
        (claude_dir / ".claude.json").write_text(json.dumps(existing))

        with (
            patch("cli_agent_orchestrator.providers.claude_code.Path.home", return_value=fake_home),
            patch(
                "cli_agent_orchestrator.providers.claude_code.subprocess.check_output",
                return_value=b"2.0.0",
            ),
        ):
            provider = ClaudeCodeProvider("t1", "s1", "w1")
            provider.configure_headless(tmp_path / "workspace")

        data = json.loads((claude_dir / ".claude.json").read_text())
        # Pre-existing key must still be present
        assert data["customKey"] == "customValue"
        # theme is overwritten by headless config
        assert data["theme"] == "dark"
        assert data["hasCompletedOnboarding"] is True

    def test_handles_subprocess_failure_gracefully(self, tmp_path):
        """Falls back to 'unknown' version when claude --version is unavailable."""
        fake_home = tmp_path
        claude_dir = fake_home / ".claude"

        with (
            patch("cli_agent_orchestrator.providers.claude_code.Path.home", return_value=fake_home),
            patch(
                "cli_agent_orchestrator.providers.claude_code.subprocess.check_output",
                side_effect=FileNotFoundError("claude not found"),
            ),
        ):
            provider = ClaudeCodeProvider("t1", "s1", "w1")
            provider.configure_headless(tmp_path / "workspace")

        data = json.loads((claude_dir / ".claude.json").read_text())
        assert data["lastOnboardingVersion"] == "unknown"

    def test_handles_corrupt_existing_json(self, tmp_path):
        """Starts fresh if the existing .claude.json cannot be parsed."""
        fake_home = tmp_path
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / ".claude.json").write_text("not valid json {{{{")

        with (
            patch("cli_agent_orchestrator.providers.claude_code.Path.home", return_value=fake_home),
            patch(
                "cli_agent_orchestrator.providers.claude_code.subprocess.check_output",
                return_value=b"1.0.0",
            ),
        ):
            provider = ClaudeCodeProvider("t1", "s1", "w1")
            provider.configure_headless(tmp_path / "workspace")

        data = json.loads((claude_dir / ".claude.json").read_text())
        assert data["hasCompletedOnboarding"] is True


# ---------------------------------------------------------------------------
# CodexProvider
# ---------------------------------------------------------------------------


class TestCodexProviderConfigureHeadless:
    """Tests for CodexProvider.configure_headless()."""

    def test_creates_config_toml_when_absent(self, tmp_path):
        """Creates ~/.codex/config.toml with a trusted workspace section."""
        fake_home = tmp_path
        workspace = tmp_path / "my-project"

        with patch("cli_agent_orchestrator.providers.codex.Path.home", return_value=fake_home):
            provider = CodexProvider("t1", "s1", "w1")
            provider.configure_headless(workspace)

        codex_config = fake_home / ".codex" / "config.toml"
        assert codex_config.exists()
        content = codex_config.read_text()
        assert str(workspace) in content
        assert 'trust_level = "trusted"' in content

    def test_appends_workspace_when_missing(self, tmp_path):
        """Appends a new workspace trust section to an existing config."""
        fake_home = tmp_path
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        existing_content = "[general]\nmodel = \"o4-mini\"\n"
        (codex_dir / "config.toml").write_text(existing_content)

        workspace = tmp_path / "new-project"

        with patch("cli_agent_orchestrator.providers.codex.Path.home", return_value=fake_home):
            provider = CodexProvider("t1", "s1", "w1")
            provider.configure_headless(workspace)

        content = (codex_dir / "config.toml").read_text()
        # Existing content preserved
        assert "[general]" in content
        assert 'model = "o4-mini"' in content
        # New workspace section added
        assert str(workspace) in content
        assert 'trust_level = "trusted"' in content

    def test_does_not_duplicate_existing_workspace(self, tmp_path):
        """Does not duplicate workspace entry if already trusted."""
        fake_home = tmp_path
        workspace = tmp_path / "existing-project"
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        existing_content = (
            f'\n[projects."{workspace}"]\ntrust_level = "trusted"\n'
        )
        (codex_dir / "config.toml").write_text(existing_content)

        with patch("cli_agent_orchestrator.providers.codex.Path.home", return_value=fake_home):
            provider = CodexProvider("t1", "s1", "w1")
            provider.configure_headless(workspace)

        content = (codex_dir / "config.toml").read_text()
        # Workspace entry should appear exactly once
        assert content.count(str(workspace)) == 1

    def test_does_not_match_substring_paths(self, tmp_path):
        """Does not treat a path that starts with workspace as a match."""
        fake_home = tmp_path
        workspace = tmp_path / "project"
        other_workspace = tmp_path / "project-v2"
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        # Only other_workspace is trusted, not workspace
        existing_content = (
            f'\n[projects."{other_workspace}"]\ntrust_level = "trusted"\n'
        )
        (codex_dir / "config.toml").write_text(existing_content)

        with patch("cli_agent_orchestrator.providers.codex.Path.home", return_value=fake_home):
            provider = CodexProvider("t1", "s1", "w1")
            provider.configure_headless(workspace)

        content = (codex_dir / "config.toml").read_text()
        # Both workspace sections must be present
        assert f'[projects."{workspace}"]' in content
        assert f'[projects."{other_workspace}"]' in content
