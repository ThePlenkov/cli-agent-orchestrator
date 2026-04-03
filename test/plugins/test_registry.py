"""Tests for the plugin registry."""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.plugins.registry import (
    PluginManifest,
    PluginRegistry,
    get_registry,
    reset_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_YAML = textwrap.dedent(
    """\
    provider:
      type: mock_provider
      class: cli_agent_orchestrator.providers.base:BaseProvider
      binary: mock-cli
      label: Mock Provider
      requires_workspace: true
    tool_mapping:
      execute_bash:
        - mock_shell
      fs_read:
        - mock_read
      fs_write:
        - mock_write
      fs_list:
        - mock_list
      fs_*:
        - mock_read
        - mock_write
        - mock_list
    agent_dir: ~/.mock-provider/agents
    """
)


def _write_plugin(tmp_path: Path, subdir: str, content: str) -> Path:
    """Write a plugin.yaml into *tmp_path/<subdir>/plugin.yaml*."""
    plugin_dir = tmp_path / subdir
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = plugin_dir / "plugin.yaml"
    manifest.write_text(content, encoding="utf-8")
    return plugin_dir


# ---------------------------------------------------------------------------
# PluginManifest unit tests
# ---------------------------------------------------------------------------


class TestPluginManifest:
    def test_parses_all_fields(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        manifest = PluginManifest(data, tmp_path)

        assert manifest.type == "mock_provider"
        assert manifest.class_path == "cli_agent_orchestrator.providers.base:BaseProvider"
        assert manifest.binary == "mock-cli"
        assert manifest.label == "Mock Provider"
        assert manifest.requires_workspace is True
        assert manifest.agent_dir == "~/.mock-provider/agents"
        assert "execute_bash" in manifest.tool_mapping

    def test_defaults_label_to_type_when_missing(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        del data["provider"]["label"]
        manifest = PluginManifest(data, tmp_path)
        assert manifest.label == "mock_provider"

    def test_requires_workspace_defaults_false(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        del data["provider"]["requires_workspace"]
        manifest = PluginManifest(data, tmp_path)
        assert manifest.requires_workspace is False

    def test_agent_dir_none_when_missing(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        del data["agent_dir"]
        manifest = PluginManifest(data, tmp_path)
        assert manifest.agent_dir is None

    def test_load_provider_class_raises_on_bad_format(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        data["provider"]["class"] = "no_colon_here"
        manifest = PluginManifest(data, tmp_path)
        with pytest.raises(ValueError, match="module.path:ClassName"):
            manifest.load_provider_class()

    def test_load_provider_class_raises_on_import_error(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        data["provider"]["class"] = "nonexistent.module:SomeClass"
        manifest = PluginManifest(data, tmp_path)
        with pytest.raises(ModuleNotFoundError):
            manifest.load_provider_class()


# ---------------------------------------------------------------------------
# PluginRegistry — discovery tests
# ---------------------------------------------------------------------------


class TestPluginRegistryDiscovery:
    def test_empty_when_plugins_dir_missing(self, tmp_path: Path):
        registry = PluginRegistry(tmp_path / "nonexistent")
        assert registry.get_plugin_types() == []

    def test_empty_when_plugins_dir_is_empty(self, tmp_path: Path):
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []

    def test_loads_valid_plugin(self, tmp_path: Path):
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        registry = PluginRegistry(tmp_path)
        assert "mock_provider" in registry.get_plugin_types()

    def test_loads_multiple_plugins(self, tmp_path: Path):
        _write_plugin(tmp_path, "plugin_a", _VALID_YAML.replace("mock_provider", "plugin_a"))
        _write_plugin(tmp_path, "plugin_b", _VALID_YAML.replace("mock_provider", "plugin_b"))
        registry = PluginRegistry(tmp_path)
        types = registry.get_plugin_types()
        assert "plugin_a" in types
        assert "plugin_b" in types

    def test_ignores_directories_without_plugin_yaml(self, tmp_path: Path):
        (tmp_path / "no_manifest").mkdir()
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []

    def test_ignores_files_in_plugins_dir(self, tmp_path: Path):
        (tmp_path / "stray_file.yaml").write_text("hello: world")
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []


# ---------------------------------------------------------------------------
# PluginRegistry — malformed manifest handling
# ---------------------------------------------------------------------------


class TestPluginRegistryMalformedManifest:
    def test_skips_non_dict_yaml(self, tmp_path: Path):
        _write_plugin(tmp_path, "bad_plugin", "- this is a list, not a dict\n")
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []

    def test_skips_missing_provider_type(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        del data["provider"]["type"]
        _write_plugin(tmp_path, "no_type", yaml.dump(data))
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []

    def test_skips_missing_provider_class(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        del data["provider"]["class"]
        _write_plugin(tmp_path, "no_class", yaml.dump(data))
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []

    def test_skips_invalid_yaml_syntax(self, tmp_path: Path):
        _write_plugin(tmp_path, "bad_yaml", "provider: [unclosed bracket\n")
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []

    def test_empty_plugin_yaml(self, tmp_path: Path):
        _write_plugin(tmp_path, "empty", "")
        registry = PluginRegistry(tmp_path)
        assert registry.get_plugin_types() == []


# ---------------------------------------------------------------------------
# PluginRegistry — accessor methods
# ---------------------------------------------------------------------------


class TestPluginRegistryAccessors:
    def _registry(self, tmp_path: Path) -> PluginRegistry:
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        return PluginRegistry(tmp_path)

    def test_get_manifest_returns_manifest(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        manifest = registry.get_manifest("mock_provider")
        assert manifest is not None
        assert manifest.type == "mock_provider"

    def test_get_manifest_returns_none_for_unknown(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        assert registry.get_manifest("unknown") is None

    def test_get_binaries(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        assert registry.get_binaries() == {"mock_provider": "mock-cli"}

    def test_get_workspace_providers(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        assert "mock_provider" in registry.get_workspace_providers()

    def test_get_tool_mappings(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        mappings = registry.get_tool_mappings()
        assert "mock_provider" in mappings
        assert "execute_bash" in mappings["mock_provider"]

    def test_get_agent_dirs(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        dirs = registry.get_agent_dirs()
        assert dirs.get("mock_provider") == "~/.mock-provider/agents"

    def test_get_source_labels(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        labels = registry.get_source_labels()
        assert labels.get("mock_provider") == "Mock Provider"

    def test_get_provider_class_returns_none_for_unknown(self, tmp_path: Path):
        registry = self._registry(tmp_path)
        assert registry.get_provider_class("unknown") is None

    def test_get_provider_class_returns_none_on_import_error(self, tmp_path: Path):
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        data["provider"]["class"] = "nonexistent.module:Foo"
        _write_plugin(tmp_path, "bad_import", yaml.dump(data))
        registry = PluginRegistry(tmp_path)
        # Should return None (not raise)
        assert registry.get_provider_class("mock_provider") is None


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


class TestSingleton:
    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_get_registry_returns_same_instance(self, tmp_path: Path):
        with patch("cli_agent_orchestrator.plugins.registry.PLUGINS_DIR", tmp_path, create=True):
            with patch(
                "cli_agent_orchestrator.constants.PLUGINS_DIR",
                tmp_path,
            ):
                r1 = get_registry()
                r2 = get_registry()
                assert r1 is r2

    def test_reset_registry_forces_reinit(self, tmp_path: Path):
        with patch("cli_agent_orchestrator.plugins.registry.PLUGINS_DIR", tmp_path, create=True):
            with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
                r1 = get_registry()
                reset_registry()
                r2 = get_registry()
                assert r1 is not r2


# ---------------------------------------------------------------------------
# Integration: verify plugin appears in downstream registries
# ---------------------------------------------------------------------------


class TestPluginIntegration:
    """End-to-end check that a plugin.yaml makes the provider visible in all 7 locations."""

    def setup_method(self):
        reset_registry()

    def teardown_method(self):
        reset_registry()

    def test_plugin_type_in_all_providers(self, tmp_path: Path):
        """Plugin type must be included when enumerating all available providers."""
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.constants import PROVIDERS
            from cli_agent_orchestrator.plugins.registry import get_registry

            all_providers = PROVIDERS + get_registry().get_plugin_types()
            assert "mock_provider" in all_providers

    def test_plugin_tool_mapping_used_in_get_disallowed_tools(self, tmp_path: Path):
        """get_disallowed_tools() must use plugin tool mappings."""
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            blocked = get_disallowed_tools("mock_provider", ["@cao-mcp-server"])
            # All native mock tools should be blocked
            assert "mock_shell" in blocked
            assert "mock_read" in blocked

    def test_plugin_agent_dir_in_settings_service(self, tmp_path: Path):
        """get_agent_dirs() must include plugin-declared agent directories."""
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.services.settings_service import get_agent_dirs

            dirs = get_agent_dirs()
            assert "mock_provider" in dirs
            assert dirs["mock_provider"] == "~/.mock-provider/agents"

    def test_plugin_label_in_agent_profiles(self, tmp_path: Path, tmp_path_factory):
        """list_agent_profiles() must use plugin source labels."""
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        # Create a fake agent dir with a .md file so the scan finds something
        agent_dir = tmp_path_factory.mktemp("agents")
        (agent_dir / "test-agent.md").write_text("---\nname: test-agent\n---\nHello")

        import yaml

        data = yaml.safe_load(_VALID_YAML)
        data["agent_dir"] = str(agent_dir)
        _write_plugin(tmp_path, "mock_provider2", yaml.dump(data).replace("mock_provider", "mock_provider2"))

        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.plugins.registry import get_registry

            labels = get_registry().get_source_labels()
            assert "mock_provider2" in labels

    def test_plugin_binary_in_providers_listing(self, tmp_path: Path):
        """get_binaries() must include plugin-declared binary names."""
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.plugins.registry import get_registry

            binaries = get_registry().get_binaries()
            assert binaries.get("mock_provider") == "mock-cli"

    def test_plugin_workspace_requirement(self, tmp_path: Path):
        """Providers declaring requires_workspace must appear in workspace set."""
        _write_plugin(tmp_path, "mock_provider", _VALID_YAML)
        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.plugins.registry import get_registry

            assert "mock_provider" in get_registry().get_workspace_providers()

    def test_no_workspace_plugin(self, tmp_path: Path):
        """Providers with requires_workspace: false must not appear in workspace set."""
        import yaml

        data = yaml.safe_load(_VALID_YAML)
        data["provider"]["requires_workspace"] = False
        _write_plugin(tmp_path, "no_ws_provider", yaml.dump(data).replace("mock_provider", "no_ws_provider"))
        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.plugins.registry import get_registry

            assert "no_ws_provider" not in get_registry().get_workspace_providers()

    def test_manager_raises_for_unknown_plugin_type(self, tmp_path: Path):
        """ProviderManager.create_provider raises ValueError for unknown types."""
        with patch("cli_agent_orchestrator.constants.PLUGINS_DIR", tmp_path):
            reset_registry()
            from cli_agent_orchestrator.providers.manager import ProviderManager

            mgr = ProviderManager()
            with pytest.raises(ValueError, match="Unknown provider type"):
                mgr.create_provider("nonexistent_provider", "tid", "session", "window")
