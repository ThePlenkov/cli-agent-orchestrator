# Devcontainer Feature (CAO)

This document describes how to use the official CAO devcontainer feature, how to validate it locally, and how it should be released.

## Goal

Install CLI Agent Orchestrator inside a devcontainer with one feature block, optionally build Web UI assets, and optionally autostart `cao-server`.

## Options

The feature supports these options:

- `version` (string, default: `latest`) - git ref to checkout (`latest`, tag, or commit SHA)
- `webui` (boolean, default: `false`) - build web assets during install
- `port` (string, default: `9889`) - server port used by entrypoint autostart
- `autostart` (boolean, default: `false`) - run `cao-server` when container starts

## Usage

### 1) Published feature usage (recommended)

Use after publishing to GHCR:

```json
{
  "features": {
    "ghcr.io/awslabs/cli-agent-orchestrator/cao:2": {
      "version": "latest",
      "webui": false,
      "port": "9889",
      "autostart": false
    }
  }
}
```

### 2) Local feature usage (for development and testing)

Use directly from the repository checkout:

```json
{
  "features": {
    "./.devcontainer/features/cao": {
      "version": "latest",
      "webui": false,
      "port": "9889",
      "autostart": false
    }
  }
}
```

If you enable `webui: true`, ensure `npm` is available in the container (for example by adding `ghcr.io/devcontainers/features/node:1`).

## Common scenarios

These examples are based on standard [devcontainer](https://containers.dev/) configuration patterns. For option semantics, see the [Options](#options) section above.

### 1) Minimal install

Use this for a quick CAO install in Codespaces or VS Code "Reopen in Container" flows.

```json
{
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
  "features": {
    "ghcr.io/devcontainers/features/python:1": {},
    "ghcr.io/awslabs/cli-agent-orchestrator/cao:2": {}
  }
}
```

### 2) Server with web UI on container start

Use this when you want the container to start CAO server automatically with web UI enabled.

```json
{
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
  "features": {
    "ghcr.io/devcontainers/features/python:1": {},
    "ghcr.io/devcontainers/features/node:1": {},
    "ghcr.io/awslabs/cli-agent-orchestrator/cao:2": {
      "webui": true,
      "autostart": true,
      "port": "9889"
    }
  },
  "forwardPorts": [9889]
}
```

### 3) Multi-feature stack

Use this when combining CAO with other devcontainer features.

```json
{
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu",
  "features": {
    "ghcr.io/devcontainers/features/python:1": {},
    "ghcr.io/devcontainers/features/node:1": {},
    "ghcr.io/devcontainers/features/docker-in-docker:2": {},
    "ghcr.io/awslabs/cli-agent-orchestrator/cao:2": {
      "webui": true
    }
  }
}
```

`webui: true` requires `ghcr.io/devcontainers/features/node:1`; feature ordering is handled by `installsAfter`.

### 4) Pinned version / internal fork

Use this for reproducible CI builds or when installing from an internal mirror.

```json
{
  "features": {
    "ghcr.io/awslabs/cli-agent-orchestrator/cao:2": {
      "version": "v2.1.1"
    }
  },
  "remoteEnv": {
    "REPO_URL": "https://github.example.com/internal/cli-agent-orchestrator.git"
  }
}
```

## Validation

### Mandatory smoke checks

Run in the target container environment:

```bash
sudo VERSION=latest WEBUI=false AUTOSTART=false bash .devcontainer/features/cao/install.sh
cao --help
cao-server --help
```

### Optional full checks

```bash
sudo VERSION=latest WEBUI=true AUTOSTART=false bash .devcontainer/features/cao/install.sh
```

Then verify one of these web artifact layouts exists for the selected version:

- `/usr/local/share/cao/repo/web/dist/index.html` (older layout)
- `/usr/local/share/cao/repo/src/cli_agent_orchestrator/web_ui/index.html` (current layout)

## Notes

- Default repo source is official upstream: `https://github.com/awslabs/cli-agent-orchestrator.git`
- `REPO_URL` may be overridden only for testing/custom forks
- Feature manifest depends on `ghcr.io/devcontainers/features/python:1` to guarantee `pip` availability

## Release Plan

1. Keep feature in draft PR until smoke checks pass.
2. Merge into `main` after review.
3. Build and publish feature artifact to `ghcr.io/awslabs/cli-agent-orchestrator/cao` with major tag `:2` and immutable version tag.
4. Update repository docs/examples to use published registry reference.
5. Run post-release verification by creating a fresh devcontainer from the published feature block.
6. Announce availability in release notes and include rollback note (pin to previous known-good feature tag).
