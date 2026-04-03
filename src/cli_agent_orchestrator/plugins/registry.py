"""Plugin registry for discovering and loading external provider plugins.

CAO scans ``PLUGINS_DIR`` (``~/.aws/cli-agent-orchestrator/plugins/``) at
startup for subdirectories that contain a ``plugin.yaml`` manifest.  Each
manifest declares a new provider type together with the metadata that CAO
needs to integrate it seamlessly without any source-code modifications.

plugin.yaml schema
------------------
::

    provider:
      type: my_provider          # unique string identifier (e.g. "devin_cli")
      class: my_pkg.mod:MyClass  # importable Python class (module:ClassName)
      binary: my-cli             # CLI binary name checked for installation
      label: My Provider         # human-readable display label
      requires_workspace: true   # whether the provider needs workspace access
    tool_mapping:
      execute_bash:
        - my_shell_tool
      fs_read:
        - my_read_tool
      fs_write:
        - my_write_tool
      fs_list:
        - my_list_tool
      fs_*:
        - my_read_tool
        - my_write_tool
        - my_list_tool
    agent_dir: ~/.my-provider/agents
"""

import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Type

import yaml

logger = logging.getLogger(__name__)

# Module-level singleton — reset via reset_registry() in tests
_registry: Optional["PluginRegistry"] = None


class PluginManifest:
    """Represents a parsed and validated ``plugin.yaml`` manifest."""

    def __init__(self, data: Dict[str, Any], plugin_dir: Path) -> None:
        provider_data: Dict[str, Any] = data.get("provider", {})
        self.type: str = str(provider_data.get("type", ""))
        self.class_path: str = str(provider_data.get("class", ""))
        self.binary: str = str(provider_data.get("binary", ""))
        self.label: str = str(provider_data.get("label", self.type))
        self.requires_workspace: bool = bool(provider_data.get("requires_workspace", False))
        self.tool_mapping: Dict[str, List[str]] = data.get("tool_mapping", {})
        raw_agent_dir = data.get("agent_dir")
        self.agent_dir: Optional[str] = str(raw_agent_dir) if raw_agent_dir is not None else None
        self.plugin_dir: Path = plugin_dir

    def load_provider_class(self) -> Type[Any]:
        """Import and return the provider class declared in ``provider.class``."""
        module_path, _, class_name = self.class_path.rpartition(":")
        if not module_path or not class_name:
            raise ValueError(
                f"provider.class '{self.class_path}' must be in 'module.path:ClassName' format"
            )
        module = importlib.import_module(module_path)
        return getattr(module, class_name)  # type: ignore[no-any-return]


class PluginRegistry:
    """Registry of external providers loaded from ``plugin.yaml`` manifests."""

    def __init__(self, plugins_dir: Path) -> None:
        self._plugins: Dict[str, PluginManifest] = {}
        self._load_from_dir(plugins_dir)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _load_from_dir(self, plugins_dir: Path) -> None:
        """Scan *plugins_dir* for subdirectories that contain ``plugin.yaml``."""
        if not plugins_dir.exists():
            return
        try:
            for subdir in sorted(plugins_dir.iterdir()):
                if subdir.is_dir():
                    manifest_file = subdir / "plugin.yaml"
                    if manifest_file.exists():
                        self._load_manifest(manifest_file, subdir)
        except Exception as exc:
            logger.warning("Failed to scan plugins directory %s: %s", plugins_dir, exc)

    def _load_manifest(self, manifest_file: Path, plugin_dir: Path) -> None:
        """Parse and register a single ``plugin.yaml`` file."""
        try:
            raw = manifest_file.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                logger.warning(
                    "Skipping %s: expected a YAML mapping, got %s",
                    manifest_file,
                    type(data).__name__,
                )
                return
            manifest = PluginManifest(data, plugin_dir)
            if not manifest.type:
                logger.warning(
                    "Skipping plugin at %s: missing required field 'provider.type'",
                    plugin_dir,
                )
                return
            if not manifest.class_path:
                logger.warning(
                    "Skipping plugin '%s' at %s: missing required field 'provider.class'",
                    manifest.type,
                    plugin_dir,
                )
                return
            self._plugins[manifest.type] = manifest
            logger.info("Loaded plugin provider: %s", manifest.type)
        except Exception as exc:
            logger.warning("Failed to load plugin.yaml at %s: %s", manifest_file, exc)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_plugin_types(self) -> List[str]:
        """Return the list of all registered plugin provider type strings."""
        return list(self._plugins.keys())

    def get_manifest(self, provider_type: str) -> Optional[PluginManifest]:
        """Return the manifest for *provider_type*, or ``None`` if not registered."""
        return self._plugins.get(provider_type)

    def get_provider_class(self, provider_type: str) -> Optional[Type[Any]]:
        """Import and return the provider class for *provider_type*.

        Returns ``None`` if the type is not registered or the import fails.
        """
        manifest = self.get_manifest(provider_type)
        if manifest is None:
            return None
        try:
            return manifest.load_provider_class()
        except Exception as exc:
            logger.error(
                "Failed to import provider class for plugin '%s': %s", provider_type, exc
            )
            return None

    def get_binaries(self) -> Dict[str, str]:
        """Return ``{provider_type: binary_name}`` for plugins that declare a binary."""
        return {pt: m.binary for pt, m in self._plugins.items() if m.binary}

    def get_workspace_providers(self) -> Set[str]:
        """Return the set of plugin provider types that require workspace access."""
        return {pt for pt, m in self._plugins.items() if m.requires_workspace}

    def get_tool_mappings(self) -> Dict[str, Dict[str, List[str]]]:
        """Return ``{provider_type: tool_mapping}`` for all plugins."""
        return {pt: m.tool_mapping for pt, m in self._plugins.items() if m.tool_mapping}

    def get_agent_dirs(self) -> Dict[str, str]:
        """Return ``{provider_type: agent_dir}`` for plugins that declare an agent directory."""
        return {
            pt: m.agent_dir
            for pt, m in self._plugins.items()
            if m.agent_dir is not None
        }

    def get_source_labels(self) -> Dict[str, str]:
        """Return ``{provider_type: label}`` for all registered plugins."""
        return {pt: m.label for pt, m in self._plugins.items()}


# ------------------------------------------------------------------
# Singleton helpers
# ------------------------------------------------------------------


def get_registry() -> PluginRegistry:
    """Return the module-level :class:`PluginRegistry` singleton.

    The registry is lazily initialised on first call so that ``PLUGINS_DIR``
    is resolved at runtime rather than at import time.
    """
    global _registry
    if _registry is None:
        from cli_agent_orchestrator.constants import PLUGINS_DIR

        _registry = PluginRegistry(PLUGINS_DIR)
    return _registry


def reset_registry() -> None:
    """Reset the singleton (used in tests to inject a fresh registry)."""
    global _registry
    _registry = None
