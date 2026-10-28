"""Foreground CLI for the standalone Kohaku KT MCP server."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from kt_mcp import __version__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if (
        not argv
        and sys.platform == "linux"
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        from kt_mcp.tui import main as tui_main

        return tui_main(["--workspace", str(Path.cwd())])
    if argv and argv[0] in {"init", "daemon", "client-config", "link"}:
        from kt_mcp.daemon import main as daemon_main

        return daemon_main(argv)
    if argv and argv[0] == "service":
        from kt_mcp.service import main as service_main

        return service_main(argv[1:])
    if argv and argv[0] == "tui":
        from kt_mcp.tui import main as tui_main

        return tui_main(argv[1:])
    if argv and argv[0] == "instance":
        from kt_mcp.instance_cli import main as instance_main

        return instance_main(argv[1:])
    if argv and argv[0] == "update":
        from kt_mcp.update import main as update_main

        return update_main(argv[1:])
    parser = argparse.ArgumentParser(
        description="Kohaku KT MCP: standalone workspace tools derived from KohakuTerrarium.",
        epilog=(
            "Linux commands: init, daemon, link, client-config, service, tui, instance, update. "
            "Use kt-mcp COMMAND --help for details. "
            "On Linux, no arguments in a terminal selects this directory's instance. "
            "Use kt-mcp tui to manage all instances. "
            "Use --transport stdio to force the MCP protocol, including in a terminal. "
            "Upstream: https://github.com/Kohaku-Lab/KohakuTerrarium"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"Kohaku KT MCP {__version__}"
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--token-env",
        default="KT_MCP_TOKEN",
        help="Environment variable containing the HTTP access token for /mcp/<token>",
    )
    parser.add_argument(
        "--public-origin",
        default="",
        help="HTTPS reverse-proxy origin allowed by HTTP Host/Origin checks",
    )
    parser.add_argument(
        "--allow-outside-workspace",
        action="store_true",
        help="Allow file tools to access paths outside the workspace",
    )
    args = parser.parse_args(argv)
    import uvicorn

    from kt_mcp.config import Config
    from kt_mcp.runtime import Runtime
    from kt_mcp.server import create_app, serve_stdio

    logging.basicConfig(
        level=logging.WARNING,
        format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        config = Config(
            args.workspace, allow_outside_workspace=args.allow_outside_workspace
        )
        runtime = Runtime(config)
        if args.transport == "stdio":
            asyncio.run(serve_stdio(runtime))
        else:
            app = create_app(
                runtime,
                token=os.environ.get(args.token_env, ""),
                port=args.port,
                public_origin=args.public_origin,
            )
            uvicorn.run(
                app, host="127.0.0.1", port=args.port, access_log=False, log_config=None
            )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        pass
    return 0
