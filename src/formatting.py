from __future__ import annotations

"""Tiny leaf module for shared numeric formatting helpers.

Kept dependency-free so any layer (scoring, prompts, reporting) can import it
without pulling in heavier modules.
"""


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def multiple(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}x"
