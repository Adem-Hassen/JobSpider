"""Prompt templates shipped with the package."""

from __future__ import annotations

from importlib.resources import files


def load_prompt(name: str) -> str:
    """Return the text of ``ai/prompts/<name>.md``."""
    return (files(__package__) / f"{name}.md").read_text(encoding="utf-8").strip() + "\n"
