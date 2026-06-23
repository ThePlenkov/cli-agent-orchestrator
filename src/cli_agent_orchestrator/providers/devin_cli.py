"""Devin CLI provider implementation.

This module provides the DevinCliProvider class for integrating with the
Devin CLI (https://cli.devin.ai/), Cognition AI's terminal-native coding
assistant.

The Devin CLI runs a fixed-layout TUI. The terminal layout the provider
parses is::

    > user message           <- user input (prefixed with "> ")
    Response text            <- agent reply
    ────────────────────     <- horizontal rule (fixed TUI chrome)
    #                        <- input prompt (NEVER disappears — fixed chrome)
    ────────────────────     <- horizontal rule
    Mode: ... Model: ...     <- status bar

Key challenges:
1. The ``#`` prompt is fixed TUI chrome — it never disappears during
   processing. Processing must be detected by spinner text
   (``Running tools``, ``esc to interrupt``) taking priority.
2. Ghost / autocomplete text appears after ``#`` (e.g. ``# may be``).
   The provider uses a relaxed prompt pattern (``^\\s*#``) gated by the
   status-bar visibility (``Mode:.*Model:``) so the prompt alone does
   not false-trigger PROCESSING.
3. **Stateless completed detection**: user-input lines are prefixed
   with ``> ``. The provider scans for ``^> .+`` to distinguish IDLE
   from COMPLETED without ephemeral latching flags.

Flags used at launch::

    devin --permission-mode dangerous --respect-workspace-trust false \\
          --prompt-file <temp_file>.md \\
          --config <temp_config>.json

- ``--permission-mode dangerous`` auto-approves tool calls so the agent
  does not block on per-tool approval prompts during orchestration.
- ``--respect-workspace-trust false`` skips the workspace-trust dialog
  the TUI shows on first run.
- ``--prompt-file <path>`` injects the agent profile markdown body as
  the system prompt.
- ``--config <path>`` injects the MCP server configuration (JSON).
"""

import json
import logging
import re
import shlex
import shutil
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for Devin CLI provider-specific errors."""

    pass


# =============================================================================
# Regex Patterns — verified from Devin CLI TUI probe fixtures.
# =============================================================================

# ANSI escape code pattern for stripping terminal colors.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Processing indicators — Devin renders one of these strings while the
# agent is working on a turn. Spinner text takes priority over the
# fixed-chrome ``#`` prompt because the prompt NEVER disappears during
# processing (it is a permanent part of the TUI layout).
PROCESSING_PATTERNS = [
    r"Running tools",
    r"esc to interrupt",
    r"Running:",
    r"Executing:",
    r"Reading file",
    r"Writing to",
    r"Editing file",
]

# Status-bar text — Devin renders ``Mode: <mode>  Model: <model>`` at
# the bottom of every TUI frame regardless of state. Used as a
# "TUI-is-up" gate so the relaxed ``#`` prompt pattern does not
# false-trigger on a half-initialised pane.
STATUS_BAR_PATTERN = r"Mode:.*Model:"

# Idle prompt — the ``#`` character at the start of a line. Ghost /
# autocomplete text appears after ``#`` (e.g. ``# may be``), so the
# pattern is deliberately relaxed (no end-anchor) and only used in
# combination with the status-bar visibility check.
IDLE_PROMPT_PATTERN = r"^\s*#"

# User-input line — user-typed messages are echoed with a ``> ``
# prefix. The provider uses this prefix to distinguish IDLE (no user
# input yet this session) from COMPLETED (the agent has finished a
# turn) without relying on ephemeral latching flags.
USER_INPUT_PATTERN = r"^>\s+\S"

# Horizontal-rule separator — a contiguous run of box-drawing
# horizontal characters (U+2500-U+257F). Used as the bottom-of-response
# boundary in extract_last_message_from_script.
SEPARATOR_PATTERN = r"^[\u2500-\u257F]{10,}\s*$"

# Generic error patterns for detecting failure states in terminal output.
ERROR_PATTERN = (
    r"(?:Error:|ERROR:|Traceback \(most recent call last\):|"
    r"ConnectionError:|APIError:|Failed to authenticate|"
    r"command not found)"
)


class DevinCliProvider(BaseProvider):
    """Provider for Devin CLI (``devin``).

    Manages the lifecycle of a Devin CLI REPL session inside a tmux
    window: initialization, status detection, response extraction, and
    cleanup.

    Attributes:
        terminal_id: Unique identifier for this terminal instance.
        session_name: Name of the tmux session containing this terminal.
        window_name: Name of the tmux window for this terminal.
        _agent_profile: Optional Devin agent name (e.g. ``"developer"``).
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize Devin CLI provider state.

        Args:
            terminal_id: Unique identifier for this terminal.
            session_name: Name of the tmux session.
            window_name: Name of the tmux window.
            agent_profile: Optional Devin agent name (e.g. ``"developer"``).
            allowed_tools: Optional list of CAO tool names the agent is
                allowed to use. Devin CLI does not expose a native
                ``--disallowedTools`` flag, so restrictions are enforced
                softly via the ``SECURITY_PROMPT`` (see
                :data:`cli_agent_orchestrator.constants.SECURITY_PROMPT`).
            skill_prompt: Optional skill catalog text built by the service
                layer. Appended to the system prompt at launch.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        # Temp paths the provider has created under the CAO tmp dir.
        # ``cleanup()`` deletes every entry in this list so the
        # per-session files (system prompt + MCP config) do not
        # accumulate on disk.
        self._tmp_paths: list[Path] = []

    @property
    def paste_enter_count(self) -> int:
        """Devin CLI submits on a single Enter after bracketed paste."""
        return 1

    @property
    def paste_submit_delay(self) -> float:
        """Devin CLI's TUI renderer needs a slightly longer pause."""
        return 0.5

    def _cao_tmp_dir(self) -> Path:
        """Resolve the CAO tmp directory and create it on demand.

        Honours the ``CAO_TMP_DIR`` env var so tests can redirect
        temp output to ``/tmp/cao_test`` instead of polluting the
        user's ``~/.aws/cli-agent-orchestrator/tmp``. Defaults to
        ``~/.aws/cli-agent-orchestrator/tmp`` for production.
        """
        import os

        cao_tmp = Path(
            os.environ.get(
                "CAO_TMP_DIR", str(Path.home() / ".aws" / "cli-agent-orchestrator" / "tmp")
            )
        )
        cao_tmp.mkdir(parents=True, exist_ok=True)
        return cao_tmp

    def _register_tmp_path(self, path: Path) -> None:
        """Track a per-session temp path so ``cleanup()`` can remove it."""
        if path in self._tmp_paths:
            return
        self._tmp_paths.append(path)

    def _build_devin_command(self) -> str:
        """Build the ``devin`` launch command.

        Flags used:
        - ``--permission-mode dangerous`` auto-approves tool calls so
          the agent does not block on per-tool approval prompts during
          orchestration.
        - ``--respect-workspace-trust false`` skips the workspace-trust
          dialog the TUI shows on first run in a directory.
        - ``--prompt-file <path>`` injects the agent profile markdown
          body as the system prompt.
        - ``--config <path>`` injects MCP server configuration.

        Returns a properly escaped shell command string suitable for
        :func:`tmux_client.send_keys`.
        """
        devin_bin = shutil.which("devin")
        if not devin_bin:
            raise ProviderError(
                "Devin CLI not found: 'devin' is not on $PATH. "
                "Install from https://cli.devin.ai/"
            )

        command_parts = ["devin", "--permission-mode", "dangerous",
                         "--respect-workspace-trust", "false"]

        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as exc:
                raise ProviderError(
                    f"Failed to load agent profile '{self._agent_profile}': {exc}"
                )

        # System prompt injection via --prompt-file. Devin CLI takes
        # a markdown file path rather than inline text.
        system_prompt = ""
        if profile is not None and profile.system_prompt:
            system_prompt = profile.system_prompt
        system_prompt = self._apply_skill_prompt(system_prompt)

        if system_prompt:
            prompt_path = self._cao_tmp_dir() / f"{self.terminal_id}-system-prompt.md"
            prompt_path.write_text(system_prompt, encoding="utf-8")
            self._register_tmp_path(prompt_path)
            command_parts.extend(["--prompt-file", str(prompt_path)])

        # MCP server injection via --config. We translate the CAO
        # agent profile's ``mcpServers`` map into a minimal JSON
        # config and forward CAO_TERMINAL_ID into every server's env
        # so MCP tools (cao-mcp-server, ops-mcp-server) can identify
        # the current terminal for handoff / assign operations.
        if profile is not None and profile.mcpServers:
            config_path = self._write_mcp_config(profile.mcpServers)
            command_parts.extend(["--config", config_path])

        return shlex.join(command_parts)

    def _write_mcp_config(self, mcp_servers) -> str:
        """Materialise an MCP config JSON file for the session's MCP servers.

        The synthesised file lives under the CAO tmp dir keyed by the
        terminal id and is registered in ``self._tmp_paths`` so
        ``cleanup()`` deletes it when the session ends.

        Args:
            mcp_servers: The agent profile's ``mcpServers`` map. Keys
                are server names; values are either plain dicts (from
                YAML) or Pydantic models (from programmatic install).

        Returns:
            Absolute path to the config JSON file.
        """
        config_path = self._cao_tmp_dir() / f"{self.terminal_id}-mcp-config.json"

        servers: dict = {}
        for server_name, server_config in mcp_servers.items():
            if isinstance(server_config, dict):
                servers[server_name] = dict(server_config)
            else:
                servers[server_name] = server_config.model_dump(exclude_none=True)
            env = servers[server_name].get("env", {})
            if "CAO_TERMINAL_ID" not in env:
                env["CAO_TERMINAL_ID"] = self.terminal_id
                servers[server_name]["env"] = env

        config_path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
        self._register_tmp_path(config_path)
        return str(config_path)

    async def initialize(self) -> bool:
        """Initialize the Devin CLI provider by starting ``devin``.

        Steps:
        1. Wait for the shell prompt to appear in the tmux window.
        2. Send the ``devin`` command with the configured agent
           profile and MCP config.
        3. Wait for Devin to reach IDLE / COMPLETED state.

        Returns:
            True if initialization was successful.

        Raises:
            TimeoutError: If shell or Devin CLI initialization times out.
            ProviderError: If the ``devin`` binary is not on $PATH.
        """
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = self._build_devin_command()

        # Arm the StatusMonitor stickiness gate so the launching
        # command can drive a fresh PROCESSING transition past any
        # stale ready latch. Mirrors the Cursor / OpenCode pattern.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Wait for Devin CLI to fully initialize. Accept both IDLE
        # and COMPLETED — some versions render a startup message that
        # get_status() interprets as a completed response.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=30.0,
        ):
            raise TimeoutError("Devin CLI initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, output: Optional[str]) -> TerminalStatus:
        """Detect Devin CLI status from terminal output.

        Status detection checks patterns in priority order:

        1. Empty / None output → UNKNOWN.
        2. ERROR — matched error patterns in the buffer.
        3. PROCESSING — spinner / interrupt text matches one of
           :data:`PROCESSING_PATTERNS`. The Devin TUI ``#`` prompt is
           fixed chrome and NEVER disappears during processing, so
           the spinner text is the only reliable processing signal.
        4. WAITING_USER_ANSWER — interactive prompt / picker dialog.
        5. IDLE / COMPLETED — ``#`` prompt + status bar (``Mode:...
           Model:``) at the bottom of the buffer. Distinguished
           statelessly: ``> user input`` lines mark COMPLETED, their
           absence marks IDLE.

        Args:
            output: Raw terminal output (rolling buffer, up to ~8KB).

        Returns:
            Current TerminalStatus.
        """
        if not output:
            return TerminalStatus.UNKNOWN

        # Strip the RAW pipe-pane escapes so structural checks see
        # clean, line-oriented text.
        clean = strip_terminal_escapes(output)

        # ── 1. ERROR detection (highest priority) ─────────────────────
        if re.search(ERROR_PATTERN, clean):
            return TerminalStatus.ERROR

        # ── 2. PROCESSING — spinner text (the only reliable signal) ───
        # The Devin TUI's ``#`` prompt is fixed chrome and is ALWAYS
        # visible regardless of agent state, so the prompt alone
        # cannot distinguish IDLE from PROCESSING. Spinner text
        # ("Running tools", "esc to interrupt", etc.) takes priority
        # and is the authoritative processing signal.
        for pattern in PROCESSING_PATTERNS:
            if re.search(pattern, clean):
                return TerminalStatus.PROCESSING

        # ── 3. TUI-up gate ─────────────────────────────────────────────
        # The status bar ("Mode: ... Model: ...") is rendered on every
        # TUI frame regardless of state. We require it to be visible
        # before trusting the relaxed ``#`` prompt pattern, so a
        # half-initialised TUI does not false-trigger.
        status_bar_visible = re.search(STATUS_BAR_PATTERN, clean) is not None

        # ── 4. IDLE / COMPLETED ───────────────────────────────────────
        # Statelessly distinguished by the presence of a ``> user
        # input`` line in the buffer: at least one user input line
        # means the agent has finished at least one turn (COMPLETED);
        # none yet means a fresh spawn (IDLE).
        if status_bar_visible and re.search(IDLE_PROMPT_PATTERN, clean, re.MULTILINE):
            has_user_input = any(
                re.match(USER_INPUT_PATTERN, line) for line in clean.splitlines()
            )
            if has_user_input:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        return TerminalStatus.UNKNOWN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last assistant response from terminal output.

        The Devin TUI renders::

            > user message
            Response text
            ────────────────────
            #
            ────────────────────
            Mode: ... Model: ...

        The assistant response is the text between the LAST ``> user
        input`` line and the horizontal-rule separator that precedes
        the ``#`` prompt. The horizontal rule is a contiguous run of
        box-drawing horizontal characters (U+2500-U+257F).

        Raises:
            ValueError: When no response boundary is detected.
        """
        clean = re.sub(ANSI_CODE_PATTERN, "", script_output)
        lines = clean.splitlines()

        # Find the line index of the LAST ``> user input`` line.
        # The user input may span multiple physical lines (a pasted
        # long prompt), so we capture everything from the last user
        # line onward until the next horizontal-rule separator.
        last_user_line_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if re.match(USER_INPUT_PATTERN, line):
                last_user_line_idx = i

        if last_user_line_idx is None:
            raise ValueError(
                "No Devin CLI response found - no '> user input' boundary detected"
            )

        # Walk forward from the user input line. Skip the closing
        # horizontal rule of the user input box (the very first
        # separator after the user line), then collect response lines
        # until the NEXT separator (which precedes the ``#`` prompt).
        response_lines: list[str] = []
        separator_re = re.compile(SEPARATOR_PATTERN)
        past_user_box = False
        for line in lines[last_user_line_idx + 1 :]:
            if separator_re.match(line):
                if not past_user_box:
                    # First separator = closing rule of user input box.
                    past_user_box = True
                    continue
                # Second separator = bottom of response region.
                break
            response_lines.append(line)

        # Trim trailing blank lines but preserve internal blank lines
        # (the assistant response is multi-line).
        while response_lines and not response_lines[-1].strip():
            response_lines.pop()

        response = "\n".join(response_lines).strip()

        if not response:
            raise ValueError("Empty Devin CLI response - no content found after user input")

        return response

    def exit_cli(self) -> str:
        """Get the command to exit Devin CLI.

        Devin CLI exits on ``/exit`` (slash command). This matches
        the convention used by the other providers.
        """
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Devin CLI provider state.

        Resets the initialised flag and removes every per-session temp
        file the provider has created under the CAO tmp dir (system
        prompt file, MCP config JSON).

        Errors during cleanup are logged and swallowed — the session
        is already going away at this point and we do not want to mask
        the original error path with a transient ``OSError`` from a
        stale file.
        """
        import shutil

        self._initialized = False
        for path in self._tmp_paths:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "DevinCliProvider cleanup: failed to remove %s: %s",
                    path,
                    exc,
                )
        self._tmp_paths = []
