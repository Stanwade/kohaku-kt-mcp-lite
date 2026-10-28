"""One terminal overview for independently configured user MCP services."""

import time

from kt_mcp import instances, tui
from kt_mcp.onboarding import SetupCancelled
from kt_mcp.terminal_style import TerminalStyle


class InstanceManagerController:
    """Keep selection and per-instance controllers separate from curses drawing."""

    def __init__(self, selected_name=None):
        self.items = []
        self.index = 0
        self.note = ""
        self._requested_name = selected_name
        self._panels = {}
        self.errors = {}

    @property
    def selected(self):
        if not self.items:
            return None
        return self.items[self.index]

    def select(self, name):
        for index, item in enumerate(self.items):
            if item.name == name:
                self.index = index
                return
        raise ValueError("The selected instance is no longer available.")

    def move(self, amount):
        if self.items:
            self.index = max(0, min(len(self.items) - 1, self.index + amount))

    def redact(self, text):
        for panel in self._panels.values():
            text = panel.redact(text)
        return text

    def refresh(self):
        name = self.selected.name if self.selected else self._requested_name
        self.items = instances.list_instances()
        self._requested_name = None
        self.index = max(0, min(self.index, len(self.items) - 1))
        if name is not None:
            for index, item in enumerate(self.items):
                if item.name == name:
                    self.index = index
                    break
        retained = {item.name for item in self.items}
        self._panels = {
            name: panel for name, panel in self._panels.items() if name in retained
        }
        self.errors = {}
        for item in self.items:
            if item.error:
                self.errors[item.name] = item.error
                continue
            try:
                panel = self._panels.get(item.name)
                if panel is None or panel.config_path != item.config_path:
                    panel = tui.TerminalController(
                        tui.ServiceManager(item.name), item.config_path
                    )
                    self._panels[item.name] = panel
                panel.refresh()
                if panel.refresh_error:
                    self.errors[item.name] = panel.refresh_error
            except tui.UI_ERRORS as exc:
                # One invalid or unavailable instance must not hide other entries.
                self.errors[item.name] = self.redact(str(exc))

    def selected_controller(self):
        item = self.selected
        if item is None:
            raise ValueError("No instance selected. Press n to create one.")
        if item.error:
            raise ValueError(f"{item.name}: configuration is unavailable. {item.error}")
        panel = self._panels.get(item.name)
        if panel is None:
            raise ValueError(
                f"{item.name}: "
                + self.errors.get(item.name, "configuration is unavailable")
            )
        return panel

    def state(self, item):
        if item.error:
            return "invalid"
        if item.name in self.errors:
            return "unavailable"
        panel = self._panels.get(item.name)
        if panel is None:
            return "unknown"
        if panel.status.get("LoadState") == "not-found":
            return "not set up"
        return panel.status.get("ActiveState") or "unknown"

    def act(self, key):
        """Dispatch an action to exactly the selected instance."""
        key = key.lower()
        if key not in {"s", "x", "r", "u", "l", "c", "v", "?"}:
            return None
        panel = self.selected_controller()
        panel.refresh()
        if key == "s" and panel.status.get("LoadState") == "not-found":
            # Starting a saved instance registers only its own unit.
            key = "i"
        popup = panel.act(key)
        self.note = f"{panel.manager.name}: {panel.note}" if panel.note else ""
        return popup

    def create(self, *, ask=None, ask_secret=None):
        item = instances.setup_instance(ask=ask, ask_secret=ask_secret)
        self.refresh()
        self.select(item.name)
        self.note = f"{item.name} saved. Press s to install/start this instance."
        return item


def _padded(value, width):
    fitted = tui.ellipsize(str(value), width)
    return fitted + " " * (width - tui.display_width(fitted))


def _columns(width):
    available = width - 4
    name = max(12, min(25, available // 4))
    state, port, mode = 11, 5, 5
    workspace = available - name - state - port - mode - 8
    return name, state, port, mode, workspace


def _row(values, widths):
    return "  ".join(_padded(value, width) for value, width in zip(values, widths))


def _new_instance(screen, controller):
    """Run the normal setup wizard without affecting a running instance."""
    import curses

    item = None
    curses.def_prog_mode()
    curses.endwin()
    try:
        try:
            item = controller.create()
        except (SetupCancelled, KeyboardInterrupt, EOFError):
            controller.note = "New instance cancelled."
        except tui.UI_ERRORS as exc:
            controller.note = controller.redact(f"Instance creation failed: {exc}")
        print("\n" + TerminalStyle().text(controller.note, bold=True, color="yellow"))
        try:
            input("Press Enter to return to the manager: ")
        except (EOFError, KeyboardInterrupt):
            pass
    finally:
        curses.reset_prog_mode()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(250)
        screen.clearok(True)
    return item


def _detail(screen, controller):
    panel = controller.selected_controller()
    panel.return_to_list = True
    tui._screen(screen, panel)
    controller.note = f"{panel.manager.name}: {panel.note}" if panel.note else ""
    screen.clearok(True)


def instance_screen(screen, controller, open_selected=False):
    """Show every saved instance; q in a detail panel returns to this list."""
    import curses

    styles = tui._styles()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(250)
    controller.refresh()
    if open_selected and controller.selected is not None:
        try:
            _detail(screen, controller)
        except tui.UI_ERRORS as exc:
            controller.note = controller.redact(str(exc))
            tui._popup(screen, "Instance unavailable", controller.note)
    next_refresh = 0.0
    offset = 0
    while True:
        if time.monotonic() >= next_refresh:
            try:
                controller.refresh()
            except tui.UI_ERRORS as exc:
                controller.note = controller.redact(str(exc))
            next_refresh = time.monotonic() + 2
        height, width = screen.getmaxyx()
        screen.erase()
        tui._put(screen, 0, 1, "Kohaku KT MCP · Instances", styles["title"])
        if height < 18 or width < 64:
            tui._put(
                screen, 2, 1, "Resize terminal to at least 64 x 18. Press q to quit."
            )
        else:
            available = width - 4
            tui._put(
                screen,
                1,
                2,
                f"{len(controller.items)} saved | Actions affect the selected instance",
                styles["muted"],
            )
            widths = _columns(width)
            tui._put(
                screen,
                3,
                2,
                _row(("Instance", "State", "Port", "Mode", "Workspace"), widths),
                curses.A_BOLD,
            )
            page = height - 12
            offset = max(
                0,
                min(
                    offset,
                    controller.index,
                    max(0, len(controller.items) - page),
                ),
            )
            if controller.index >= offset + page:
                offset = controller.index - page + 1
            for row, item in enumerate(controller.items[offset : offset + page], 4):
                selected = item is controller.selected
                attribute = curses.A_REVERSE if selected else 0
                tui._put(
                    screen,
                    row,
                    2,
                    controller.redact(
                        _row(
                            (
                                item.name,
                                controller.state(item),
                                item.port or "-",
                                item.mode or "-",
                                item.workspace or "-",
                            ),
                            widths,
                        )
                    ),
                    attribute,
                )
            if not controller.items:
                tui._put(
                    screen,
                    4,
                    2,
                    "No instances yet. Press n to create one.",
                    styles["muted"],
                )
            item = controller.selected
            if item is not None:
                tui._put(
                    screen,
                    height - 8,
                    2,
                    "Selected: " + item.name,
                    styles["title"],
                )
                tui._put(
                    screen,
                    height - 7,
                    2,
                    "Workspace: "
                    + tui.ellipsize(
                        controller.redact(item.workspace or "-"), available - 11
                    ),
                )
                if item.error:
                    link = "Configuration unavailable. Other entries remain selectable."
                else:
                    panel = controller._panels.get(item.name)
                    link = (
                        panel.connection_url or panel.connection_state
                        if panel is not None
                        else "Connection unavailable."
                    )
                lines = tui.wrap_display(link, available)
                for row, line in enumerate(lines[:3], height - 6):
                    tui._put(screen, row, 2, line, styles["link"])
                if len(lines) > 3:
                    tui._put(
                        screen,
                        height - 4,
                        2,
                        "Press u for the complete link, or Enter for details.",
                        styles["muted"],
                    )
            tui._put(
                screen,
                height - 3,
                2,
                "Up/Down: select | Enter: details | n: new | q: quit",
                styles["muted"],
            )
            tui._put(
                screen,
                height - 2,
                2,
                "s Start | x Stop | r Restart | o Configure | u Link | l Logs",
            )
            tui._put(
                screen,
                height - 1,
                1,
                controller.redact(controller.note),
                styles["warning"],
            )
        screen.refresh()
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if height < 18 or width < 64:
            continue
        if key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            controller.move(
                {
                    curses.KEY_UP: -1,
                    curses.KEY_DOWN: 1,
                    curses.KEY_PPAGE: -page,
                    curses.KEY_NPAGE: page,
                }[key]
            )
            continue
        if key in (10, 13, curses.KEY_ENTER):
            character = "enter"
        elif 0 <= key <= 255:
            character = chr(key).lower()
        else:
            continue
        if character not in {"enter", "n", "s", "x", "r", "o", "u", "l", "c", "v", "?"}:
            continue
        try:
            if character == "n":
                if _new_instance(screen, controller) is not None:
                    _detail(screen, controller)
            elif character == "enter":
                _detail(screen, controller)
            elif character == "o":
                panel = controller.selected_controller()
                tui._configure(screen, panel)
                controller.note = (
                    f"{panel.manager.name}: {panel.note}" if panel.note else ""
                )
            else:
                if character in {"s", "x", "r"}:
                    controller.note = "Working on the selected instance..."
                    tui._put(screen, height - 1, 1, controller.note)
                    screen.refresh()
                popup = controller.act(character)
                if popup:
                    tui._popup(screen, *popup)
        except tui.UI_ERRORS as exc:
            controller.note = controller.redact(str(exc))
            tui._popup(screen, "Action could not be completed", controller.note)
        next_refresh = 0.0
