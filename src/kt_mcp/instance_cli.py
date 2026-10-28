"""Commands for independent workspace instances; service actions target one name."""

import argparse
import json
import os
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

from kt_mcp import instances, managed_service, operations
from kt_mcp.service import ServiceError, ServiceManager


def _selected(name: str) -> instances.Instance:
    item = instances.get_instance(name)
    if item.error:
        raise instances.InstanceError(f"{item.name}: {item.error}")
    return item


def _control(item: instances.Instance, action: str):
    return operations.control_service(
        ServiceManager(item.name), item.config_path, action
    )


def _display(value) -> str:
    return "".join(
        character if character.isprintable() else "?" for character in str(value)
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="kt-mcp instance",
        description="Manage a separate MCP service and configuration for each workspace.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "list", help="List saved instances without exposing credentials"
    )
    listing.add_argument(
        "--json", action="store_true", help="Print public instance metadata as JSON"
    )
    create = commands.add_parser(
        "create", help="Create a private instance; stopped by default"
    )
    create.add_argument("name")
    create.add_argument("--workspace", type=Path, default=None)
    create.add_argument("--port", type=int)
    create.add_argument("--mode", choices=("local", "quick", "named"), default="local")
    create.add_argument("--public-origin", default="")
    create.add_argument("--cloudflared", default="")
    create.add_argument("--allow-outside-workspace", action="store_true")
    create.add_argument(
        "--start", action="store_true", help="Register and start only the new instance"
    )
    for action, explanation in (
        ("start", "Register if needed, then start the selected instance"),
        ("stop", "Stop only the selected instance"),
        ("restart", "Restart only the selected instance"),
        ("link", "Print the selected instance's full MCP URL, including its token"),
        ("client-config", "Export HTTP or stdio client JSON"),
        ("configure", "Pause and edit the selected instance with the shared wizard"),
        ("uninstall", "Uninstall the service; retain its configuration and workspace"),
    ):
        child = commands.add_parser(action, help=explanation)
        child.add_argument("name")
        if action in {"link", "client-config"}:
            child.add_argument(
                "--output", type=Path, help="Create a private export file"
            )
        if action == "client-config":
            child.add_argument("--transport", choices=["http", "stdio"], default="http")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "list":
            items = instances.list_instances()
            if arguments.json:
                print(
                    json.dumps(
                        [
                            {**asdict(item), "config_path": str(item.config_path)}
                            for item in items
                        ],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif not items:
                print(
                    "No instances yet. Run kt-mcp instance create NAME --workspace PATH"
                )
            else:
                print("NAME\tMODE\tPORT\tWORKSPACE")
                for item in items:
                    print(
                        "\t".join(
                            _display(value)
                            for value in (
                                item.name,
                                item.mode or "-",
                                item.port or "-",
                                item.workspace or "-",
                            )
                        )
                    )
                    if item.error:
                        print("  " + _display(item.error))
            return 0
        if arguments.command == "create":
            item = instances.create_instance(
                arguments.name,
                workspace=arguments.workspace or Path.cwd(),
                port=arguments.port,
                mode=arguments.mode,
                public_origin=arguments.public_origin,
                cloudflared=arguments.cloudflared,
                allow_outside_workspace=arguments.allow_outside_workspace,
                tunnel_token=(
                    os.environ.get("TUNNEL_TOKEN", "")
                    if arguments.mode == "named"
                    else ""
                ),
            )
            print(f"Created {item.name}: {item.config_path}")
            if arguments.start:
                _control(item, "start")
                print(f"{item.name} is running.")
            else:
                print(
                    "Start: " + shlex.join(["kt-mcp", "instance", "start", item.name])
                )
            return 0
        item = _selected(arguments.name)
        if arguments.command in {"link", "client-config"}:
            kind = arguments.command
            if kind == "client-config" and arguments.transport == "stdio":
                kind = "stdio"
            text = operations.export_text(item.config_path, kind)
            if arguments.output is not None:
                print(f"Exported to {operations.write_export(arguments.output, text)}")
            else:
                print(text)
            return 0
        if arguments.command == "configure":
            from kt_mcp.tui import TerminalController

            controller = TerminalController(ServiceManager(item.name), item.config_path)
            controller.configure()
            print(controller.note)
            return 0
        if arguments.command == "uninstall":
            operations.uninstall_service(ServiceManager(item.name), item.config_path)
            print(
                f"{item.name}: service removed; configuration and workspace retained."
            )
            return 0
        _control(item, arguments.command)
        print(
            f"{item.name}: {'stopped' if arguments.command == 'stop' else 'running'}."
        )
        return 0
    except (
        instances.InstanceError,
        managed_service.ManagedServiceError,
        ServiceError,
        ValueError,
        OSError,
        TypeError,
    ) as error:
        print(f"Instance: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Instance command interrupted.", file=sys.stderr)
        return 130
