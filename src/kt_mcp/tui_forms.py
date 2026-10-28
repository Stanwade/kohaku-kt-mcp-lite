"""Curses dialogs used by every interactive management entry point."""

try:
    import curses
except ImportError:  # Importing shared helpers must not break Windows CLI commands.
    curses = None

import io
import re
from contextlib import redirect_stdout

from kt_mcp.onboarding import SetupCancelled

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _text(value):
    return _ANSI.sub("", str(value)).replace("\r", "")


def _restore(screen):
    screen.keypad(True)
    screen.timeout(250)
    screen.clearok(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass


def _key(screen):
    try:
        value = screen.get_wch()
    except AttributeError:
        value = screen.getch()
        if 0 <= value < 256:
            return chr(value)
    except curses.error:
        return None
    return value


class _Output(io.StringIO):
    def write(self, value):
        size = super().write(value)
        if self.tell() > 65536:
            retained = self.getvalue()[-49152:]
            self.seek(0)
            self.truncate(0)
            super().write(retained)
        return size


class Wizard:
    """Adapt the shared configuration wizard to native, hidden-input dialogs."""

    def __init__(self, screen, title):
        self.screen = screen
        self.title = title
        self.output = _Output()

    def ask(self, prompt):
        return self._input(prompt, False)

    def ask_secret(self, prompt):
        return self._input(prompt, True)

    def _input(self, prompt, secret):
        from kt_mcp import tui

        screen = self.screen
        value = []
        cursor = 0
        styles = tui._styles()
        screen.timeout(-1)
        try:
            while True:
                height, width = screen.getmaxyx()
                screen.erase()
                tui._put(screen, 0, 1, self.title, styles["title"])
                if height < 10 or width < 40:
                    tui._put(screen, 2, 1, "Resize terminal to 40 x 10; Esc cancels.")
                    screen.refresh()
                    key = _key(screen)
                    if key in ("\x1b", "\x03", "\x04"):
                        raise SetupCancelled("Operation cancelled.")
                    continue
                available = width - 4
                prompt_lines = tui.wrap_display(_text(prompt), available)
                prompt_lines = prompt_lines[-3:]
                input_row = height - 3
                prompt_row = input_row - len(prompt_lines) - 1
                context = _text(self.output.getvalue())
                context_lines = tui.wrap_display(context, available)
                context_room = max(0, prompt_row - 3)
                for row, line in enumerate(
                    context_lines[-context_room:] if context_room else [], 2
                ):
                    tui._put(screen, row, 2, line)
                for row, line in enumerate(prompt_lines, prompt_row):
                    tui._put(screen, row, 2, line, styles["title"])
                shown = "*" * len(value) if secret else "".join(value)
                before = "*" * cursor if secret else "".join(value[:cursor])
                start = 0
                while tui.display_width(before[start:]) >= available - 2:
                    start += 1
                visible = tui.clip_display(shown[start:], available - 1)
                tui._put(screen, input_row, 2, visible)
                tui._put(
                    screen,
                    height - 1,
                    1,
                    "Enter: accept   Esc: cancel   Ctrl+U: clear",
                    styles["muted"],
                )
                try:
                    curses.curs_set(1)
                    screen.move(
                        input_row, min(width - 2, 2 + tui.display_width(before[start:]))
                    )
                except curses.error:
                    pass
                screen.refresh()
                key = _key(screen)
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(value)
                if key in ("\x1b", "\x03", "\x04"):
                    raise SetupCancelled("Operation cancelled.")
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if cursor:
                        del value[cursor - 1]
                        cursor -= 1
                elif key == curses.KEY_DC:
                    if cursor < len(value):
                        del value[cursor]
                elif key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                elif key in (curses.KEY_HOME, "\x01"):
                    cursor = 0
                elif key in (curses.KEY_END, "\x05"):
                    cursor = len(value)
                elif key == "\x15":
                    value.clear()
                    cursor = 0
                elif key == "\x17":
                    while cursor and value[cursor - 1].isspace():
                        del value[cursor - 1]
                        cursor -= 1
                    while cursor and not value[cursor - 1].isspace():
                        del value[cursor - 1]
                        cursor -= 1
                elif isinstance(key, str) and key.isprintable() and len(value) < 8192:
                    value[cursor:cursor] = list(key)
                    cursor += len(key)
        finally:
            _restore(screen)

    def run(self, callback):
        with redirect_stdout(self.output):
            return callback(ask=self.ask, ask_secret=self.ask_secret)


def run_wizard(screen, title, callback):
    """Run a shared input-based workflow without leaving the curses application."""
    try:
        return Wizard(screen, title).run(callback)
    finally:
        _restore(screen)


def choose(screen, title, options, body=""):
    """Scrollable menu with the full description available at every terminal size."""
    from kt_mcp import tui

    if not options:
        return None
    index = 0
    offset = 0
    styles = tui._styles()
    screen.keypad(True)
    screen.timeout(-1)
    try:
        while True:
            height, width = screen.getmaxyx()
            screen.erase()
            tui._put(screen, 0, 1, title, styles["title"])
            overflow = False
            if height < 10 or width < 40:
                tui._put(screen, 2, 1, "Resize terminal to 40 x 10; Esc returns.")
                page = 1
            else:
                description = tui.wrap_display(_text(body), width - 4) if body else []
                intro_room = max(0, height - 5 - min(2, len(options)))
                intro = description[:intro_room]
                overflow = len(description) > len(intro)
                for row, line in enumerate(intro, 2):
                    tui._put(screen, row, 2, line, styles["muted"])
                first = 3 + len(intro) if intro else 2
                page = max(1, height - first - 2)
                if index < offset:
                    offset = index
                elif index >= offset + page:
                    offset = index - page + 1
                for row, (_, label) in enumerate(
                    options[offset : offset + page], first
                ):
                    selected = offset + row - first == index
                    attribute = styles["title"] | curses.A_REVERSE if selected else 0
                    tui._put(
                        screen, row, 2, ("> " if selected else "  ") + label, attribute
                    )
                footer = (
                    "[v] Details  Up/Down  Enter  Esc/q"
                    if overflow
                    else "Up/Down: select  Enter: open  Esc/q: return"
                )
                tui._put(screen, height - 1, 1, footer, styles["muted"])
            screen.refresh()
            key = _key(screen)
            if key in ("\x1b", "q", "Q", "\x03", "\x04"):
                return None
            if height < 10 or width < 40:
                continue
            if key in ("v", "V") and overflow:
                message(screen, title + " - details", body)
                screen.timeout(-1)
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                return options[index][0]
            if key in (curses.KEY_DOWN, "j", "J"):
                index = min(len(options) - 1, index + 1)
            elif key in (curses.KEY_UP, "k", "K"):
                index = max(0, index - 1)
            elif key == curses.KEY_NPAGE:
                index = min(len(options) - 1, index + page)
            elif key == curses.KEY_PPAGE:
                index = max(0, index - page)
            elif key == curses.KEY_HOME:
                index = 0
            elif key == curses.KEY_END:
                index = len(options) - 1
    finally:
        _restore(screen)


def confirm(screen, title, body, default=False):
    choices = [("yes", "Yes, continue"), ("no", "No, return")]
    if not default:
        choices.reverse()
    return choose(screen, title, choices, body=body) == "yes"


def message(screen, title, body):
    from kt_mcp.tui import _popup

    return _popup(screen, title, _text(body))
