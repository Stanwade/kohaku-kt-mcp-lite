"""Nuitka entry point; keep normal package imports and provide startup diagnostics."""

import sys
import traceback
from pathlib import Path

from kt_mcp.tunnel import data_directory


def main():
    try:
        from kt_mcp.gui import main as gui_main

        if len(sys.argv) == 3 and sys.argv[1] == "--verify-onefile":
            return gui_main(verification_directory=Path(sys.argv[2]).resolve())
        return gui_main()
    except Exception:  # noqa: BLE001 -- preserve errors in a console-free executable
        directory = data_directory()
        directory.mkdir(parents=True, exist_ok=True)
        report = directory / "startup-error.log"
        report.write_text(traceback.format_exc(), encoding="utf-8")
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, f"无法启动 KT MCP，错误详情：\n{report}", "Kohaku KT MCP", 0x10
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
