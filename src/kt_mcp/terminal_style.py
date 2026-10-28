"""Small standard-library terminal styling shared by interactive commands."""

import os
import re
import sys


class TerminalStyle:
    """Use the current terminal's capabilities without decorating redirected text."""

    def __init__(self):
        self._bold = ""
        self._reset = ""
        self._colors = {}
        term = os.environ.get("TERM", "")
        if (
            not sys.stdin.isatty()
            or not sys.stdout.isatty()
            or not term
            or term.lower() == "dumb"
        ):
            return
        try:
            import curses
        except ImportError:
            return

        def sequence(value: bytes | None) -> str:
            # terminfo padding directives are for tputs(), not printable text.
            return re.sub(rb"\$<[^>]*>", b"", value or b"").decode("ascii")

        try:
            curses.setupterm(term=term, fd=sys.stdout.fileno())
            reset = sequence(curses.tigetstr("sgr0"))
            if not reset:
                return
            bold = sequence(curses.tigetstr("bold"))
            colors = {}
            foreground = curses.tigetstr("setaf")
            if (
                "NO_COLOR" not in os.environ
                and foreground
                and curses.tigetnum("colors") >= 8
            ):
                for name, number in (("cyan", 6), ("green", 2), ("yellow", 3)):
                    colors[name] = sequence(curses.tparm(foreground, number))
        except (curses.error, OSError, ValueError):
            return
        self._bold = bold
        self._reset = reset
        self._colors = colors

    def text(self, value: str, *, bold: bool = False, color: str = "") -> str:
        prefix = (self._bold if bold else "") + self._colors.get(color, "")
        return prefix + value + self._reset if prefix else value

    def prompt(self, label: str, default: str | None = None) -> str:
        label = self.text(label, bold=True)
        if default is not None:
            label += " [" + self.text(default, bold=True, color="cyan") + "]"
        return label + ": "
