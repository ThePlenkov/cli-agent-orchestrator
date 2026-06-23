"""Unit tests for Devin CLI provider."""

from __future__ import annotations

import shlex
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.devin_cli import DevinCliProvider


# =============================================================================
# Sample terminal outputs — calibrated against the Devin CLI TUI layout.
# =============================================================================

DEVIN_IDLE_OUTPUT = """
──────────────────────────────────────────────────────────
Welcome to Devin! Ask me anything.
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""

DEVIN_PROCESSING_RUNNING_TOOLS_OUTPUT = """
──────────────────────────────────────────────────────────
> Summarize the project
──────────────────────────────────────────────────────────
Welcome to Devin! Ask me anything.
──────────────────────────────────────────────────────────
Running tools: read_file src/main.py
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""

DEVIN_PROCESSING_ESC_OUTPUT = """
──────────────────────────────────────────────────────────
> Summarize the project
──────────────────────────────────────────────────────────
Working on your request... (esc to interrupt)
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""

DEVIN_COMPLETED_OUTPUT = """
──────────────────────────────────────────────────────────
> Summarize the project
──────────────────────────────────────────────────────────
The project is a Python CLI tool that orchestrates multiple
AI coding agents across tmux sessions.
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""

DEVIN_ERROR_OUTPUT = """
──────────────────────────────────────────────────────────
> Summarize the project
──────────────────────────────────────────────────────────
Error: Failed to authenticate with Devin API
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""

# TUI-up but no ``#`` prompt yet — half-initialised pane. Should
# report UNKNOWN rather than false-positive IDLE/COMPLETED.
DEVIN_TUI_PARTIAL_OUTPUT = """
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""

# Ghost / autocomplete text after ``#`` — the relaxed prompt pattern
# must still match this and not be mistaken for processing.
DEVIN_IDLE_GHOST_AUTOCOMPLETE_OUTPUT = """
──────────────────────────────────────────────────────────
Welcome to Devin! Ask me anything.
──────────────────────────────────────────────────────────
# may be
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""


# =============================================================================
# Status detection tests
# =============================================================================


class TestDevinCliProviderStatus:
    """Tests for DevinCliProvider.get_status."""

    def test_idle_when_no_user_input_yet(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.get_status(DEVIN_IDLE_OUTPUT) == TerminalStatus.IDLE

    def test_completed_when_user_input_present(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.get_status(DEVIN_COMPLETED_OUTPUT) == TerminalStatus.COMPLETED

    def test_processing_when_running_tools_text_visible(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        assert (
            provider.get_status(DEVIN_PROCESSING_RUNNING_TOOLS_OUTPUT)
            == TerminalStatus.PROCESSING
        )

    def test_processing_when_esc_to_interrupt_visible(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.get_status(DEVIN_PROCESSING_ESC_OUTPUT) == TerminalStatus.PROCESSING

    def test_error_detection(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.get_status(DEVIN_ERROR_OUTPUT) == TerminalStatus.ERROR

    def test_empty_output_returns_unknown(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.get_status("") == TerminalStatus.UNKNOWN
        assert provider.get_status(None) == TerminalStatus.UNKNOWN

    def test_no_tui_chrome_returns_unknown(self):
        """Without status-bar + prompt visible, the relaxed ``#`` pattern
        is intentionally NOT trusted — the provider reports UNKNOWN."""
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.get_status("some random text without TUI chrome") == TerminalStatus.UNKNOWN

    def test_partial_tui_returns_unknown(self):
        """Half-initialised TUI (status bar but no ``#`` prompt) reports
        UNKNOWN rather than false-positive IDLE/COMPLETED."""
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.get_status(DEVIN_TUI_PARTIAL_OUTPUT) == TerminalStatus.UNKNOWN

    def test_ghost_autocomplete_after_prompt_still_idle(self):
        """Ghost / autocomplete text after ``#`` must not trigger PROCESSING."""
        provider = DevinCliProvider("t1", "s1", "w1")
        # No ``> user input`` line present → IDLE; relaxed ``#`` pattern
        # plus status bar is sufficient.
        assert provider.get_status(DEVIN_IDLE_GHOST_AUTOCOMPLETE_OUTPUT) == TerminalStatus.IDLE

    def test_processing_takes_priority_over_completed(self):
        """Spinner text + ``#`` prompt + ``> user input`` line:
        spinner wins (PROCESSING), not COMPLETED."""
        # Already covered by test_processing_when_running_tools_text_visible,
        # but re-assert the priority order explicitly.
        provider = DevinCliProvider("t1", "s1", "w1")
        assert (
            provider.get_status(DEVIN_PROCESSING_RUNNING_TOOLS_OUTPUT)
            == TerminalStatus.PROCESSING
        )


# =============================================================================
# Response extraction tests
# =============================================================================


class TestDevinCliProviderExtraction:
    """Tests for DevinCliProvider.extract_last_message_from_script."""

    def test_extract_response(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        result = provider.extract_last_message_from_script(DEVIN_COMPLETED_OUTPUT)
        assert "Python CLI tool" in result
        assert "orchestrates" in result

    def test_extract_response_handles_multiline(self):
        output = """
──────────────────────────────────────────────────────────
> Summarize the project
──────────────────────────────────────────────────────────
The project is a Python CLI tool that orchestrates multiple
AI coding agents across tmux sessions.

It supports 7 built-in providers including Claude Code,
Codex, Gemini, and others.
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""
        provider = DevinCliProvider("t1", "s1", "w1")
        result = provider.extract_last_message_from_script(output)
        assert "Python CLI tool" in result
        assert "7 built-in providers" in result
        assert "──────────" not in result

    def test_extract_returns_last_response_when_multiple(self):
        """When the buffer contains multiple user turns, the LAST response
        is extracted (stateless — anchored on the last ``> user input``)."""
        output = """
──────────────────────────────────────────────────────────
> First question
──────────────────────────────────────────────────────────
First answer.
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
> Second question
──────────────────────────────────────────────────────────
Second answer is the one we want.
──────────────────────────────────────────────────────────
#
──────────────────────────────────────────────────────────
Mode: chat  Model: devin-1
"""
        provider = DevinCliProvider("t1", "s1", "w1")
        result = provider.extract_last_message_from_script(output)
        assert "Second answer" in result
        assert "First answer" not in result

    def test_extract_raises_without_user_input(self):
        provider = DevinCliProvider("t1", "s1", "w1")
        with pytest.raises(ValueError, match="No Devin CLI response"):
            provider.extract_last_message_from_script("nothing here")

    def test_extract_raises_when_no_response_content(self):
        """A ``> user input`` line with no following separator produces
        no response region → empty response → ValueError."""
        output = """
> Summarize the project
"""
        provider = DevinCliProvider("t1", "s1", "w1")
        with pytest.raises(ValueError, match="Empty Devin CLI response"):
            provider.extract_last_message_from_script(output)


# =============================================================================
# Command construction tests
# =============================================================================


class TestDevinCliProviderCommand:
    """Tests for DevinCliProvider._build_devin_command."""

    @patch("cli_agent_orchestrator.providers.devin_cli.shutil.which")
    @patch("cli_agent_orchestrator.providers.devin_cli.load_agent_profile")
    def test_command_basic_flags(self, mock_profile, mock_which):
        mock_which.return_value = "/usr/local/bin/devin"
        mock_profile.return_value = None

        provider = DevinCliProvider("t1", "s1", "w1")
        parts = shlex.split(provider._build_devin_command())

        assert parts[0] == "devin"
        assert "--permission-mode" in parts
        assert "dangerous" in parts
        assert "--respect-workspace-trust" in parts
        assert "false" in parts

    @patch("cli_agent_orchestrator.providers.devin_cli.shutil.which")
    def test_command_raises_when_binary_missing(self, mock_which):
        mock_which.return_value = None
        provider = DevinCliProvider("t1", "s1", "w1")
        from cli_agent_orchestrator.providers.devin_cli import ProviderError

        with pytest.raises(ProviderError, match="Devin CLI not found"):
            provider._build_devin_command()

    def test_paste_enter_count_is_one(self):
        """Devin CLI submits on a single Enter after bracketed paste."""
        provider = DevinCliProvider("t1", "s1", "w1")
        assert provider.paste_enter_count == 1


# =============================================================================
# Registration / provider manager tests
# =============================================================================


class TestDevinCliProviderRegistration:
    """Tests that DevinCliProvider is properly registered."""

    def test_provider_type_in_enum(self):
        from cli_agent_orchestrator.models.provider import ProviderType

        assert ProviderType.DEVIN_CLI.value == "devin_cli"

    def test_provider_type_in_providers_list(self):
        from cli_agent_orchestrator.constants import PROVIDERS

        assert "devin_cli" in PROVIDERS

    def test_in_workspace_access_set(self):
        from cli_agent_orchestrator.cli.commands.launch import (
            PROVIDERS_REQUIRING_WORKSPACE_ACCESS,
        )

        assert "devin_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS

    def test_in_provider_binaries(self):
        """Verify devin_cli is present in the api/main.py provider_binaries dict."""
        import inspect

        from cli_agent_orchestrator.api import main as api_main

        src = inspect.getsource(api_main.list_providers_endpoint)
        assert '"devin_cli": "devin"' in src

    def test_in_provider_source_labels(self):
        """Verify devin_cli is registered in the provider_source_labels mapping
        so profile scanning tags it with the 'devin' source label."""
        import inspect

        from cli_agent_orchestrator.utils import agent_profiles

        src = inspect.getsource(agent_profiles.list_agent_profiles)
        assert '"devin_cli": "devin"' in src

    def test_tool_mapping_present(self):
        from cli_agent_orchestrator.utils.tool_mapping import (
            ALL_NATIVE_TOOLS,
            TOOL_MAPPING,
            get_disallowed_tools,
        )

        assert "devin_cli" in TOOL_MAPPING
        assert "devin_cli" in ALL_NATIVE_TOOLS
        # Mapping values match the spec: fs_read->Read, fs_write->Write,
        # execute_bash->Bash.
        assert "Read" in TOOL_MAPPING["devin_cli"]["fs_read"]
        assert "Write" in TOOL_MAPPING["devin_cli"]["fs_write"]
        assert "Bash" in TOOL_MAPPING["devin_cli"]["execute_bash"]
        # Disallowed-tools computation exercises the mapping end-to-end.
        disallowed = get_disallowed_tools("devin_cli", ["fs_read"])
        assert "Write" in disallowed
        assert "Bash" in disallowed
        assert "Read" not in disallowed

    def test_default_agent_dir_registered(self):
        from cli_agent_orchestrator.services.settings_service import get_agent_dirs

        dirs = get_agent_dirs()
        assert "devin_cli" in dirs


class TestDevinCliProviderManager:
    """Test that ProviderManager can create a DevinCliProvider."""

    def test_manager_creates_devin_provider(self):
        from cli_agent_orchestrator.providers.manager import ProviderManager
        from cli_agent_orchestrator.models.provider import ProviderType

        manager = ProviderManager()
        provider = manager.create_provider(
            ProviderType.DEVIN_CLI.value,
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )
        assert isinstance(provider, DevinCliProvider)
        assert manager.get_provider("t1") is provider
