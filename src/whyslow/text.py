"""Rendering untrusted strings (process names, command lines) safely."""

from __future__ import annotations


def safe(text: str | None, width: int | None = None) -> str:
    """Strip control characters so names can't inject terminal escapes or break a menu.

    Process names and arguments are attacker-influenced input; anything that
    reaches a terminal or the macOS menu bar goes through here first.
    """
    text = "".join(c if c.isprintable() else "?" for c in (text or ""))
    if width is not None and len(text) > width:
        text = text[: width - 1] + "…"
    return text
