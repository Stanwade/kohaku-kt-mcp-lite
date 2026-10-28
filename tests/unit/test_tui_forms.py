import sys

import pytest

from kt_mcp import tui
from kt_mcp import tui_forms as forms
from kt_mcp.onboarding import SetupCancelled

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux curses dialogs")


class Screen:
    def __init__(self, keys, *, height=18, width=64):
        self.keys = iter(keys)
        self.height = height
        self.width = width
        self.frames = []
        self.drawn = []
        self.timeouts = []
        self.keypad_values = []
        self.clear_values = []
        self.cursor = None

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, value):
        self.keypad_values.append(value)

    def timeout(self, value):
        self.timeouts.append(value)

    def clearok(self, value):
        self.clear_values.append(value)

    def erase(self):
        self.drawn = []

    def addnstr(self, row, column, text, limit, attribute=0):
        value = text[:limit]
        assert 0 <= row < self.height
        assert column + tui.display_width(value) < self.width
        self.drawn.append((row, value))

    def refresh(self):
        self.frames.append(list(self.drawn))

    def move(self, row, column):
        assert 0 <= row < self.height
        assert 0 <= column < self.width
        self.cursor = row, column

    def get_wch(self):
        key = next(self.keys)
        return key(self) if callable(key) else key

    def getch(self):
        value = self.get_wch()
        return ord(value) if isinstance(value, str) else value

    def rendered(self):
        return "\n".join(text for frame in self.frames for _, text in frame)


@pytest.fixture(autouse=True)
def native_curses(monkeypatch):
    import curses

    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)

    def leave_terminal():
        pytest.fail("Native dialogs must not leave curses or use terminal input")

    monkeypatch.setattr(curses, "endwin", leave_terminal)
    monkeypatch.setattr("builtins.input", leave_terminal)


def test_wizard_keeps_shared_output_and_editable_unicode_inside_curses(capsys):
    import curses

    screen = Screen(["a", "界", "c", curses.KEY_LEFT, curses.KEY_BACKSPACE, "b", "\n"])

    def workflow(*, ask, ask_secret):
        print("\x1b[31mShared configuration guidance\x1b[0m")
        return ask("Workspace: ")

    assert forms.run_wizard(screen, "Configure", workflow) == "abc"
    assert "Shared configuration guidance" in screen.rendered()
    assert "\x1b" not in screen.rendered()
    assert capsys.readouterr().out == ""
    assert screen.timeouts[-1] == 250
    assert screen.clear_values[-1] is True


def test_secret_input_is_never_echoed_in_any_rendered_frame(capsys):
    token = "synthetic-credential-" + "x" * 120
    screen = Screen([*token, "\n"])

    def workflow(*, ask, ask_secret):
        return ask_secret("Tunnel token (hidden): ")

    assert forms.run_wizard(screen, "Credentials", workflow) == token
    rendered = screen.rendered()
    assert "synthetic" not in rendered
    assert "*" * 20 in rendered
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("key", ["\x1b", "\x03", "\x04"])
def test_wizard_cancellation_restores_the_parent_panel(key):
    screen = Screen(["u", "n", "s", "a", "v", "e", "d", key])

    with pytest.raises(SetupCancelled):
        forms.run_wizard(
            screen, "Configuration", lambda **readers: readers["ask"]("Value: ")
        )

    assert screen.timeouts[-1] == 250
    assert screen.keypad_values[-1] is True
    assert screen.clear_values[-1] is True


def test_small_terminal_does_not_accept_an_invisible_prompt():
    def resize(screen):
        screen.height, screen.width = 18, 64
        return -1

    screen = Screen(["\n", resize, "o", "k", "\n"], height=8, width=30)
    result = forms.run_wizard(
        screen, "Configuration", lambda **readers: readers["ask"]("Value: ")
    )
    assert result == "ok"
    assert "Resize terminal" in screen.rendered()


def test_wizard_failure_restores_parent_state():
    screen = Screen([])

    def workflow(**readers):
        raise ValueError("configuration unavailable")

    with pytest.raises(ValueError, match="configuration unavailable"):
        forms.run_wizard(screen, "Configuration", workflow)
    assert screen.timeouts[-1] == 250
    assert screen.clear_values[-1] is True


@pytest.mark.parametrize(
    ("keys", "expected"),
    [(["\n"], False), (["j", "\n"], True), (["\x1b"], False)],
)
def test_confirmation_requires_an_explicit_yes(keys, expected):
    screen = Screen(keys)
    assert (
        forms.confirm(screen, "Pause instance?", "The selected instance will stop.")
        is expected
    )


def test_confirmation_shows_all_fitting_effects_beyond_four_lines():
    effects = [
        "Service: kt-mcp-selected.service",
        "Start now: True",
        "Automatic startup: True",
        "User linger: True",
        "Replace: True",
        "Install --now also enables automatic startup.",
        "Configuration: /home/user/instance.json",
    ]
    screen = Screen(["\n"])
    assert forms.confirm(screen, "Install service", "\n".join(effects)) is False
    first_frame = "\n".join(text for _, text in screen.frames[0])
    for effect in effects:
        assert effect in first_frame
    assert "No, return" in first_frame
    assert "Yes, continue" in first_frame


@pytest.mark.parametrize(("height", "width"), [(10, 40), (18, 64)])
def test_long_confirmation_opens_full_scrollable_details_and_retains_choice(
    height, width
):
    import curses

    body = "\n".join(f"Effect {index:02d}" for index in range(35))
    screen = Screen(
        ["j", "v", *([curses.KEY_NPAGE] * 9), "q", "\n"],
        height=height,
        width=width,
    )

    assert forms.confirm(screen, "Confirm all effects", body) is True
    assert any("[v] Details" in text for _, text in screen.frames[0])
    assert "Effect 34" in screen.rendered()
    assert "Confirm all effects - details" in screen.rendered()
    # Returning from the viewer must restore the blocking menu, then parent polling.
    assert screen.timeouts[-2:] == [-1, 250]


def test_menu_scrolls_to_last_action_and_home_without_losing_selection():
    import curses

    options = [(str(index), f"Action {index:02d}") for index in range(30)]
    screen = Screen(
        [curses.KEY_END, curses.KEY_HOME, curses.KEY_END, "\n"], height=10, width=40
    )
    assert forms.choose(screen, "Actions", options) == "29"
    assert "> Action 29" in screen.rendered()
    assert screen.timeouts[-1] == 250


def test_menu_below_minimum_size_ignores_enter_and_can_cancel():
    screen = Screen(["\n", "\x1b"], height=8, width=30)
    assert forms.choose(screen, "Actions", [("delete", "Irreversible action")]) is None
    assert "Irreversible action" not in screen.rendered()


def test_empty_menu_does_not_consume_input():
    screen = Screen([])
    assert forms.choose(screen, "Actions", []) is None
    assert screen.frames == []
