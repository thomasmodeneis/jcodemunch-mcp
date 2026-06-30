"""Antigravity (Google) integration for jCodeMunch.

Antigravity follows the Gemini CLI configuration contract:

- **MCP servers** are declared in ``~/.gemini/settings.json`` under a top-level
  ``mcpServers`` object — the same shape every other ``json_patch`` client uses.
  So MCP *registration* is handled by the shared client-detection/patch path in
  ``init.py``: Antigravity is registered there as a detected ``json_patch``
  client pointing at the Gemini settings file. This module does not duplicate
  that; it owns the Antigravity-specific *paths* and the *skill bundle*.
- **Agent skills** install under ``~/.gemini/antigravity/skills/``.

Keeping the Antigravity-specific paths in one module means a contract change is a
one-file edit. P1 scope (this module): skill bundle + status; MCP registration
rides the shared path. The ``AfterTool`` context-enrichment hook is a later
phase (see docs/prd-antigravity-hooks.md).
"""
from __future__ import annotations

from pathlib import Path


def gemini_dir() -> Path:
    """Root config dir shared by the Gemini CLI and Antigravity."""
    return Path.home() / ".gemini"


def gemini_settings_path() -> Path:
    """Global settings file holding the ``mcpServers`` block."""
    return gemini_dir() / "settings.json"


def antigravity_skills_dir() -> Path:
    """Directory Antigravity loads agent skills from."""
    return gemini_dir() / "antigravity" / "skills"


def antigravity_present() -> bool:
    """True when a Gemini/Antigravity config dir exists to configure."""
    return gemini_dir().exists()


def _skill_dir() -> Path:
    from .skills import _SKILL_NAME

    return antigravity_skills_dir() / _SKILL_NAME


def install_antigravity_skill(*, dry_run: bool = False, backup: bool = True) -> str:
    """Write the jcodemunch skill bundle into the Antigravity skills dir."""
    from .skills import _install_skill_at

    return _install_skill_at(_skill_dir(), dry_run=dry_run, backup=backup)


def uninstall_antigravity_skill(*, dry_run: bool = False) -> str:
    """Remove the jcodemunch skill bundle from the Antigravity skills dir."""
    from .skills import _uninstall_skill_at

    return _uninstall_skill_at(_skill_dir(), dry_run=dry_run)


def antigravity_skill_status() -> dict:
    """Read-only status: is the jcodemunch Antigravity skill installed?"""
    from .skills import _skill_status_at

    return _skill_status_at(_skill_dir())
