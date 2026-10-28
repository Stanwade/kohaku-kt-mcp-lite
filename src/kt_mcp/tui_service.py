"""Responsive terminal progress for the shared service lifecycle operations."""

import queue
import threading
import time

from kt_mcp import operations, tui_forms
from kt_mcp.managed_service import StartupWaitCancelled


def run_action(screen, panel, action):
    """Keep curses on the UI thread while a bounded service operation runs."""
    from kt_mcp import tui

    titles = {
        "start": "Starting instance",
        "stop": "Stopping instance",
        "restart": "Restarting instance",
    }
    if action not in titles:
        raise ValueError("Unsupported instance lifecycle action.")
    updates = queue.SimpleQueue()
    done = threading.Event()
    cancel = threading.Event()
    outcome = {}
    started = time.monotonic()
    phase = "Preparing the service operation..."
    returning = False
    cancellable = action in {"start", "restart"}
    styles = tui._styles()

    def work():
        try:
            outcome["status"] = operations.control_service(
                panel.manager,
                panel.config_path,
                action,
                on_progress=updates.put,
                cancel=cancel.is_set if cancellable else None,
            )
        except BaseException as error:  # noqa: BLE001 -- re-raised on the UI thread
            outcome["error"] = error
        finally:
            done.set()

    def draw():
        nonlocal phase
        while True:
            try:
                phase = updates.get_nowait()
            except queue.Empty:
                break
        height, width = screen.getmaxyx()
        screen.erase()
        tui._put(screen, 0, 1, titles[action], styles["title"])
        body = [
            "Instance: " + panel.manager.name,
            "",
            panel.redact(phase),
            f"Elapsed: {time.monotonic() - started:.1f}s",
        ]
        if returning:
            body.extend(["", "Returning after the current service command..."])
        row = 2
        for paragraph in body:
            for line in tui.wrap_display(paragraph, max(1, width - 4)) or [""]:
                if row >= height - 2:
                    break
                tui._put(screen, row, 2, line)
                row += 1
        if height > 2:
            footer = (
                "q/Esc: stop waiting; service keeps starting"
                if cancellable
                else "Waiting for the service to stop..."
            )
            tui._put(screen, height - 1, 1, footer, styles["muted"])
        screen.refresh()

    screen.keypad(True)
    screen.timeout(100)
    worker = threading.Thread(target=work, name="kt-mcp-service-" + action)
    try:
        # Replace the confirmation/menu before any systemd command can block.
        draw()
        worker.start()
        while True:
            try:
                if done.wait(0.025):
                    break
                draw()
                if done.is_set():
                    break
                key = tui_forms._key(screen)
            except KeyboardInterrupt:
                key = "\x03"
            if key in ("q", "Q", "\x1b", "\x03", "\x04"):
                returning = True
                if cancellable:
                    cancel.set()
    finally:
        # A return ends readiness polling, never leaves a worker to mutate the
        # instance after another manager action has begun.
        if worker.ident is not None:
            cancel.set()
            while True:
                try:
                    if done.wait(0.1):
                        break
                except KeyboardInterrupt:
                    cancel.set()
            # The event confirms the backend has finished even if SIGINT
            # interrupted Thread.join and changed its internal alive flag.
            while True:
                try:
                    worker.join()
                    break
                except KeyboardInterrupt:
                    cancel.set()
        tui_forms._restore(screen)

    error = outcome.get("error")
    if isinstance(error, StartupWaitCancelled):
        panel._configuration_paused = False
        panel.note = "Startup wait ended; check the instance status or logs."
        return None
    if error is not None:
        panel.note = panel.redact(f"{action.capitalize()} did not complete: {error}")
        raise error
    status = outcome.get("status")
    if isinstance(status, dict):
        panel.status = status
    if cancellable:
        panel._configuration_paused = False
        panel.note = "MCP started; local connection verified."
    else:
        panel.note = "MCP stopped."
    return status
