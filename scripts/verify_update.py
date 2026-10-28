"""Exercise installed updates, TUI configuration, and rollback with real user systemd.

Release transport alone is replaced: a PATH curl fixture serves version-consistent
local assets at the production GitHub URLs. Python, pip, the copied update worker,
the installed CLI, systemd, and MCP are real. This is an explicit CI gate, not part
of default pytest collection.
"""

import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from build_linux import build_bundle, check_wheel, checksums
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from verify_linux import (
    ROOT,
    available_port,
    clean_environment,
    require,
    sha256,
    unpack_verified,
)

from kt_mcp import managed_service
from kt_mcp.service import ServiceManager

RELEASES = "https://github.com/Stanwade/kohaku-kt-mcp-lite/releases"
FIXTURES = ("9.8.7", "9.8.8", "9.8.9")
SENTINEL = "kt_mcp_update_sentinel"


def write_wheel(path: Path, members: dict[str, bytes], info_directory: str):
    record = info_directory + "/RECORD"
    rows = []
    for name, content in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        rows.append((name, "sha256=" + digest.decode("ascii"), str(len(content))))
    rows.append((record, "", ""))
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    members[record] = stream.getvalue().encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, content in sorted(members.items()):
            wheel.writestr(name, content)


def fixture_wheel(source: Path, destination: Path, old_version: str, version: str):
    """Change every wheel identity and recalculate its complete RECORD."""
    old_info = f"kohaku_kt_mcp-{old_version}.dist-info"
    new_info = f"kohaku_kt_mcp-{version}.dist-info"
    members = {}
    with zipfile.ZipFile(source) as wheel:
        for name in wheel.namelist():
            if name == old_info + "/RECORD":
                continue
            content = wheel.read(name)
            if name == old_info + "/METADATA":
                content, count = re.subn(
                    rb"(?m)^Version: " + re.escape(old_version.encode()) + rb"\r?$",
                    b"Version: " + version.encode(),
                    content,
                )
                require(count == 1, "fixture wheel metadata version was not unique")
            elif name == "kt_mcp/__init__.py":
                content, count = re.subn(
                    rb"(?m)^__version__\s*=\s*['\"][^'\"]+['\"]\s*$",
                    ('__version__ = "' + version + '"\n').encode(),
                    content,
                )
                require(count == 1, "fixture wheel code version was not unique")
            if name.startswith(old_info + "/"):
                name = new_info + name[len(old_info) :]
            members[name] = content
    write_wheel(destination, members, new_info)
    check_wheel(destination, version)


def sentinel_wheel(directory: Path) -> Path:
    info = SENTINEL + "-1.0.0.dist-info"
    path = directory / (SENTINEL + "-1.0.0-py3-none-any.whl")
    write_wheel(
        path,
        {
            SENTINEL + ".py": b'VALUE = "retained-extra-package"\n',
            info
            + "/METADATA": (
                b"Metadata-Version: 2.1\nName: kt-mcp-update-sentinel\n"
                b"Version: 1.0.0\n"
            ),
            info
            + "/WHEEL": (
                b"Wheel-Version: 1.0\nGenerator: kt-mcp-update-verification\n"
                b"Root-Is-Purelib: true\nTag: py3-none-any\n"
            ),
        },
        info,
    )
    return path


def failure_installer(original: str, prefix: Path, marker: Path) -> str:
    """Fail only the real install, after the same installer passed its trial."""
    hook = (
        "\n# Verification fixture: the private real install deliberately fails.\n"
        'if [[ "$kt_prefix" == ' + shlex.quote(str(prefix)) + " ]]; then\n"
        "  \"$kt_venv/bin/python\" -I - <<'KT_MCP_FAILURE'\n"
        "import importlib.metadata, shutil, sys\nfrom pathlib import Path\n"
        f"expected = Path({str(prefix / 'venv')!r})\n"
        "assert Path(sys.prefix) == expected\n"
        "package = importlib.metadata.distribution('kohaku-kt-mcp')\n"
        "assert package.version == '9.8.9'\n"
        "sentinel = importlib.metadata.distribution('kt-mcp-update-sentinel')\n"
        f"sentinel.locate_file({SENTINEL + '.py'!r}).write_text('VALUE = \"corrupted\"\\n')\n"
        f"Path({str(marker)!r}).write_text('real-install-failed-after-trial\\n')\n"
        "module = package.locate_file('kt_mcp')\n"
        "assert module.resolve().is_relative_to(expected)\n"
        "shutil.rmtree(module)\n"
        "(expected / 'bin/python').unlink()\n"
        "KT_MCP_FAILURE\n"
        "  exit 42\nfi\n"
    )
    require(
        original.count('"$kt_target" --version') == 1,
        "fixture installer lacks its version check",
    )
    return original.replace('"$kt_target" --version', '"$kt_target" --version' + hook)


def make_fixtures(bundle: Path, version: str, root: Path, prefix: Path):
    source = bundle / f"kohaku_kt_mcp-{version}-py3-none-any.whl"
    routes = {}
    marker = root / "real-install-failure"
    for target in FIXTURES:
        directory = root / ("release-" + target)
        directory.mkdir()
        wheel = directory / f"kohaku_kt_mcp-{target}-py3-none-any.whl"
        fixture_wheel(source, wheel, version, target)
        installer = directory / "install.sh"
        content = (bundle / "install.sh").read_text(encoding="utf-8")
        if target == FIXTURES[-1]:
            content = failure_installer(content, prefix, marker)
        installer.write_text(content, encoding="utf-8")
        files = {wheel.name: wheel, "install.sh": installer}
        files.update(
            (name, bundle / name)
            for name in ("README.md", "UBUNTU.md", "LICENSE", "NOTICE")
        )
        inner = directory / "inner-SHA256SUMS"
        inner.write_text(checksums(files), encoding="ascii")
        archive = directory / f"kt-mcp-{target}-linux.tar.gz"
        build_bundle(archive, f"kt-mcp-{target}-linux", {**files, "SHA256SUMS": inner})
        manifest = directory / "SHA256SUMS"
        manifest.write_text(
            checksums(
                {archive.name: archive, wheel.name: wheel, "install.sh": installer}
            ),
            encoding="ascii",
        )
        base = RELEASES + "/download/v" + target + "/"
        routes[base + "SHA256SUMS"] = str(manifest)
        routes[base + archive.name] = str(archive)
        if target == FIXTURES[0]:
            routes[RELEASES + "/latest/download/SHA256SUMS"] = str(manifest)

    transport = root / "fixture transport"
    transport.mkdir()
    (transport / "routes.json").write_text(json.dumps(routes), encoding="utf-8")
    router = transport / "curl_fixture.py"
    router.write_text(
        "import json, shutil, sys\nfrom pathlib import Path\n"
        "routes=json.loads(Path(__file__).with_name('routes.json').read_text())\n"
        "arguments=sys.argv[1:]\n"
        "try:\n"
        "    destination=Path(arguments[arguments.index('--output')+1])\n"
        "    source=routes[arguments[-1]]\n"
        "    if destination.exists(): raise ValueError('existing download')\n"
        "    shutil.copyfile(source,destination)\n"
        "except (ValueError, KeyError, IndexError, OSError):\n"
        "    raise SystemExit(22) from None\n",
        encoding="utf-8",
    )
    curl = transport / "curl"
    curl.write_text(
        "#!/bin/sh\nexec "
        + shlex.join([sys.executable, "-B", "-I", str(router)])
        + ' "$@"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return transport, marker


def redact_diagnostic(message, secrets=()):
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"/mcp/[^\s/\"'<>]+", "/mcp/[REDACTED]", message)
    return re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)[^\s/?#]*@",
        r"\1[REDACTED]@",
        message,
    )


def execute(arguments, *, cwd, environment, token="", fails=False, timeout=900):
    process = subprocess.Popen(
        [str(argument) for argument in arguments],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        output, error = process.communicate(timeout=timeout)
    except BaseException:
        # The real worker handles SIGTERM and restores its environment.
        process.terminate()
        try:
            process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=10)
        raise
    require(not token or token not in output + error, "a command exposed the MCP token")
    expected = process.returncode != 0 if fails else process.returncode == 0
    if not expected:
        message = (
            "installed update verification command returned an unexpected exit status"
        )
        # Only this CLI owns controlled progress/errors; never expose raw pip or
        # transport output from other verification commands.
        if (
            len(arguments) >= 2
            and Path(arguments[0]).name == "kt-mcp"
            and str(arguments[1]) == "update"
        ):
            summary = redact_diagnostic(output + "\n" + error, (token,)).strip()
            if summary:
                message += "\nUpdater output (sanitized):\n" + summary[-2400:]
        require(False, message)
    return output


def snapshot(manager, config):
    unit_info, config_info = manager.unit_path.stat(), config.stat()
    return {
        "unit": manager.unit_path.read_bytes(),
        "config": config.read_bytes(),
        "unit_permissions": (
            unit_info.st_uid,
            unit_info.st_gid,
            stat.S_IMODE(unit_info.st_mode),
        ),
        "config_permissions": (
            config_info.st_uid,
            config_info.st_gid,
            stat.S_IMODE(config_info.st_mode),
        ),
        "enabled": manager.status()["UnitFileState"],
    }


async def probe(url, version, workspace=None):
    async with (
        httpx.AsyncClient(trust_env=False, timeout=10) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        require(
            (await session.initialize()).serverInfo.version == version,
            "the running MCP version differs from the update result",
        )
        if workspace is not None:
            result = await session.call_tool(
                "python", {"code": "import os; print(os.getcwd())"}
            )
            require(
                not result.isError
                and isinstance(result.structuredContent, dict)
                and result.structuredContent.get("output", "").strip()
                == str(workspace),
                "the restarted MCP did not use the edited workspace",
            )


def ready(manager, python, config, url, version, workspace=None):
    binding = managed_service.inspect_service(
        manager.name, expected_python=python, expected_config=config
    )
    require(binding is not None, "the verification service disappeared")
    status = managed_service.wait_ready(binding, expected_version=version)
    asyncio.run(probe(url, version, workspace))
    return status


def sentinel_state(python, run):
    program = (
        "import hashlib,importlib.metadata,json,kt_mcp_update_sentinel as s; "
        "from pathlib import Path; "
        "print(json.dumps({'version':importlib.metadata.version('kt-mcp-update-sentinel'),"
        "'value':s.VALUE,'sha256':hashlib.sha256(Path(s.__file__).read_bytes()).hexdigest()}))"
    )
    return json.loads(run([python, "-I", "-c", program]))


def configure_via_installed_tui(python, config, name, workspace, run):
    """Exercise the actual controller with controlled answers, without a screen stub."""
    program = r"""
import contextlib, io, json, sys
from pathlib import Path
from kt_mcp.service import ServiceManager
from kt_mcp.tui import TerminalController

config, name, workspace = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
manager = ServiceManager(name)
original = json.loads(config.read_bytes())
info = config.stat()
permissions = (info.st_uid, info.st_gid, info.st_mode)
paused_questions = 0

def configure(answers):
    global paused_questions
    choices = iter(answers)
    count = 0
    def ask(prompt):
        nonlocal count
        global paused_questions
        if count:
            state = manager.status()
            assert state["ActiveState"] == "inactive" and state["MainPID"] == "0"
            paused_questions += 1
        count += 1
        return next(choices)
    controller = TerminalController(manager, config)
    with contextlib.redirect_stdout(io.StringIO()) as shown:
        controller.configure(ask=ask)
    assert original["token"] not in shown.getvalue()
    state = manager.status()
    assert state["ActiveState"] == "inactive" and state["MainPID"] == "0"
    return controller

configure(["y", str(workspace), "", "quick", ""])
assert json.loads(config.read_bytes())["mode"] == "quick"
controller = configure(["y", "", "", "local", ""])
saved = json.loads(config.read_bytes())
assert saved["mode"] == "local" and saved["workspace"] == str(workspace)
assert saved["token"] == original["token"]
assert saved.get("tunnel_token", "") == original.get("tunnel_token", "")
info = config.stat()
assert (info.st_uid, info.st_gid, info.st_mode) == permissions
expected = "WorkingDirectory=" + str(workspace).replace("%", "%%")
assert expected in manager.unit_path.read_text().splitlines()
controller.act("s")
assert manager.status()["ActiveState"] == "active"
print(json.dumps({"editing_questions_while_stopped": paused_questions,
                  "workspace_changed": True, "mode_round_trip": True,
                  "explicit_start": True}))
"""
    result = json.loads(run([python, "-I", "-c", program, config, name, workspace]))
    require(
        result["editing_questions_while_stopped"] >= 8, "wizard did not remain stopped"
    )
    return result


def cancel_via_installed_tui(python, config, name, run):
    program = r"""
import contextlib, io, json, sys
from pathlib import Path
from kt_mcp.service import ServiceManager
from kt_mcp.tui import TerminalController
config, name = Path(sys.argv[1]), sys.argv[2]
manager = ServiceManager(name)
before = config.read_bytes(), manager.unit_path.read_bytes()
choices = iter(["y", ":q"])
with contextlib.redirect_stdout(io.StringIO()):
    TerminalController(manager, config).configure(ask=lambda prompt: next(choices))
assert before == (config.read_bytes(), manager.unit_path.read_bytes())
state = manager.status()
assert state["ActiveState"] == "inactive" and state["MainPID"] == "0"
print("cancel-preserved-files")
"""
    require(
        run([python, "-I", "-c", program, config, name]).strip()
        == "cancel-preserved-files",
        "cancelling configuration changed files or restarted MCP",
    )


def verify(arguments, report, sensitive):
    root = Path(tempfile.mkdtemp(prefix="kt-mcp-update-verify-")).resolve()
    manager = None
    attempted = False
    try:
        bundle, version = unpack_verified(arguments.archive, root / "unpacked")
        report.update(initial_version=version, archive_sha256=sha256(arguments.archive))
        require(
            tuple(map(int, version.split("."))) < (9, 8, 7),
            "choose newer verification fixture versions for this release",
        )
        prefix = root / 'install 空间 % $ "quotes"'
        bin_dir = root / "command links"
        workspace = root / "workspace 中文"
        workspace.mkdir()
        config = root / 'private config 中文 % $ "quotes".json'
        transport, failure_marker = make_fixtures(bundle, version, root, prefix)
        environment = clean_environment()
        environment["PATH"] = str(transport) + os.pathsep + environment.get("PATH", "")
        token = ""

        def run(arguments, **kwargs):
            return execute(
                arguments, cwd=root, environment=environment, token=token, **kwargs
            )

        report["stage"] = "fresh current installation"
        run(
            [
                "bash",
                bundle / "install.sh",
                "--prefix",
                prefix,
                "--bin-dir",
                bin_dir,
                "--python",
                sys.executable,
            ]
        )
        cli, python = bin_dir / "kt-mcp", prefix / "venv/bin/python"
        run([python, "-I", "-m", "pip", "install", "--no-index", sentinel_wheel(root)])
        retained_sentinel = sentinel_state(python, run)
        port = available_port()
        run(
            [
                cli,
                "init",
                "--workspace",
                workspace,
                "--config",
                config,
                "--mode",
                "local",
                "--port",
                str(port),
            ]
        )
        token = json.loads(config.read_bytes())["token"]
        sensitive.append(token)
        url = f"http://127.0.0.1:{port}/mcp/{token}"
        name = "kt-mcp-update-verify-" + uuid.uuid4().hex[:12]
        manager = ServiceManager(name)
        report["temporary_unit"] = manager.unit_name
        report["temporary_unit_path"] = str(manager.unit_path)
        require(
            not manager.unit_path.exists()
            and not manager.unit_path.is_symlink()
            and manager.status()["LoadState"] == "not-found",
            "the unique verification service name already exists",
        )
        attempted = True
        run([cli, "service", "install", "--name", name, "--config", config])
        run([cli, "service", "start", "--name", name])
        before = ready(manager, python, config, url, version)
        require(before["UnitFileState"] == "disabled", "initial service was enabled")
        original = snapshot(manager, config)

        report["stage"] = "active update"
        run([cli, "update", "--name", name])
        after = ready(manager, python, config, url, FIXTURES[0])
        require(after["MainPID"] != before["MainPID"], "updating did not restart MCP")
        require(
            snapshot(manager, config) == original, "active update changed saved state"
        )
        require(
            sentinel_state(python, run) == retained_sentinel,
            "update lost another package",
        )
        report["active_update"] = (
            "passed; restarted, version verified, remains disabled"
        )

        report["stage"] = "installed TUI configuration"
        edited_workspace = root / 'edited workspace 中文 % $ "quotes"'
        edited_workspace.mkdir()
        report["tui_configuration"] = configure_via_installed_tui(
            python, config, name, edited_workspace, run
        )
        ready(manager, python, config, url, FIXTURES[0], edited_workspace)
        cancel_via_installed_tui(python, config, name, run)
        report["tui_configuration"]["cancel_preserved_files_and_stopped"] = True

        report["stage"] = "stopped update"
        run([cli, "service", "enable", "--name", name])
        stopped = snapshot(manager, config)
        require(stopped["enabled"] == "enabled", "stopped service was not enabled")
        run([cli, "update", "--version", FIXTURES[1], "--name", name])
        require(
            manager.status()["ActiveState"] == "inactive"
            and manager.status()["MainPID"] == "0",
            "updating a stopped service started it",
        )
        require(
            snapshot(manager, config) == stopped, "stopped update changed saved state"
        )
        require(
            run([cli, "--version"]).strip() == "Kohaku KT MCP " + FIXTURES[1],
            "stopped update installed the wrong version",
        )
        require(
            not config.with_suffix(".status.json").exists(),
            "stopped MCP has stale status",
        )
        report["stopped_update"] = "passed; remains stopped and enabled"

        report["stage"] = "failed actual install and complete rollback"
        run([cli, "service", "start", "--name", name])
        ready(manager, python, config, url, FIXTURES[1], edited_workspace)
        rollback_snapshot = snapshot(manager, config)
        run([cli, "update", "--version", FIXTURES[2], "--name", name], fails=True)
        require(failure_marker.is_file(), "failure did not reach the real installation")
        ready(manager, python, config, url, FIXTURES[1], edited_workspace)
        require(
            snapshot(manager, config) == rollback_snapshot,
            "rollback changed saved state",
        )
        require(
            sentinel_state(python, run) == retained_sentinel,
            "rollback lost the sentinel package",
        )
        require(
            run([cli, "--version"]).strip() == "Kohaku KT MCP " + FIXTURES[1],
            "rollback did not restore the installed CLI",
        )
        require(
            not (prefix / ".kt-mcp-update").exists(),
            "completed recovery retained a transaction",
        )
        logs = manager.logs(1000)
        require(
            bool(logs.strip()) and token not in logs,
            "service journal failed its credential check",
        )
        report["rollback"] = (
            "passed; real install failed after trial; interpreter, package, "
            "extra sentinel package and running service restored"
        )
        report["credential_log_check"] = "passed"
        report["stage"] = "complete"
        report["success"] = True
    finally:
        report["cleanup"] = "pending"
        try:
            if attempted and manager is not None:
                if manager.unit_path.exists():
                    manager.uninstall()
                require(
                    not manager.unit_path.exists()
                    and manager.status()["MainPID"] in {"", "0"},
                    "the verification service did not fully stop and uninstall",
                )
            shutil.rmtree(root)
            report["cleanup"] = "passed"
        except Exception:
            report["success"] = False
            report["retained_temporary_directory"] = str(root)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=ROOT / "dist/update-smoke.json")
    arguments = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.error("update verification requires Linux and an ordinary systemd user")
    arguments.archive = arguments.archive.expanduser().absolute()
    report = {
        "success": False,
        "stage": "starting",
        "cleanup": "not started",
        "platform": platform.platform(),
        "fixture_versions": list(FIXTURES),
        "release_transport": "local verified fixtures served only at official URLs",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    sensitive = []
    logging.disable(logging.CRITICAL)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        verify(arguments, report, sensitive)
    except (
        Exception,  # noqa: BLE001 -- sanitize every failure in the report
        KeyboardInterrupt,
    ) as error:
        report["success"] = False
        message = (
            "verification interrupted"
            if isinstance(error, KeyboardInterrupt)
            else str(error)
        )
        message = redact_diagnostic(message, sensitive)
        report["error"] = type(error).__name__ + ": " + message[:3000]
    path = arguments.report.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
