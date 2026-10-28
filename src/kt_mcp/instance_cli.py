"""Commands for independent workspace instances; service actions target one name."""

import argparse
import json
import os
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

from kt_mcp import instances, managed_service
from kt_mcp.daemon import client_url
from kt_mcp.service import ServiceError, ServiceManager


def _selected(name: str) -> instances.Instance:
    item = instances.get_instance(name)
    if item.error:
        raise instances.InstanceError(f"{item.name}: {item.error}")
    return item


def _control(item: instances.Instance, action: str):
    manager = ServiceManager(item.name)
    if action == "start" and manager.status().get("LoadState") == "not-found":
        manager.install(item.config_path)
    binding = managed_service.inspect_service(
        item.name, expected_config=item.config_path
    )
    if binding is None:
        raise instances.InstanceError(
            "The instance service is not installed; use start first."
        )
    if action in {"start", "restart"}:
        config = instances.read_config(item.config_path)
        instances.ensure_available(
            config,
            exclude=item.config_path,
            check_socket=binding.status.get("ActiveState")
            not in {"active", "activating"},
        )
    if action == "stop":
        managed_service.stop_service(binding)
    elif action == "start":
        managed_service.start_service(binding)
    else:
        managed_service.stop_service(binding)
        managed_service.start_service(binding)


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
    create.add_argument(
        "--start", action="store_true", help="Register and start only the new instance"
    )
    for action, explanation in (
        ("start", "Register if needed, then start the selected instance"),
        ("stop", "Stop only the selected instance"),
        ("restart", "Restart only the selected instance"),
        ("link", "Print the selected instance's full MCP URL, including its token"),
    ):
        child = commands.add_parser(action, help=explanation)
        child.add_argument("name")
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
        if arguments.command == "link":
            print(client_url(instances.read_config(item.config_path), item.config_path))
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
