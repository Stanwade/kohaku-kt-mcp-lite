"""Verify a Linux release through a fresh install and the actual MCP protocol."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "read",
    "write",
    "edit",
    "multi_edit",
    "glob",
    "grep",
    "tree",
    "bash",
    "python",
    "job_status",
    "job_wait",
    "job_cancel",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unpack_verified(archive: Path, directory: Path) -> tuple[Path, str]:
    """Extract only regular files/directories, then verify complete manifest coverage."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        require(0 < len(members) <= 32, "unexpected archive member count")
        require(
            sum(member.size for member in members) <= 128 * 1024 * 1024,
            "release archive exceeds the verification size limit",
        )
        names = set()
        roots = set()
        for member in members:
            path = PurePosixPath(member.name)
            require(
                bool(path.parts)
                and not path.is_absolute()
                and ".." not in path.parts
                and "\\" not in member.name
                and (member.isfile() or member.isdir()),
                "archive contains an unsafe path or non-regular member",
            )
            require(path not in names, "archive contains duplicate paths")
            names.add(path)
            roots.add(path.parts[0])
        require(len(roots) == 1, "archive must contain one top-level directory")
        root_name = next(iter(roots))
        require(
            re.fullmatch(r"kt-mcp-[A-Za-z0-9.+_-]+-linux", root_name),
            "unexpected Linux bundle directory",
        )
        for member in members:
            target = directory.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if target.name == "install.sh" else 0o644)

    root = directory / root_name
    wheels = list(root.glob("kohaku_kt_mcp-*-py3-none-any.whl"))
    require(len(wheels) == 1, "bundle must contain exactly one universal wheel")
    expected = {
        wheels[0].name,
        "install.sh",
        "UBUNTU.md",
        "README.md",
        "LICENSE",
        "NOTICE",
    }
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    require(actual == expected | {"SHA256SUMS"}, "unexpected or missing bundle files")
    checksums = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        require(match is not None, "invalid checksum manifest line")
        checksum, name = match.groups()
        require(
            name in expected and name not in checksums,
            "unsafe or duplicate checksum entry",
        )
        checksums[name] = checksum
    require(
        set(checksums) == expected,
        "checksum manifest does not cover the complete bundle",
    )
    for name, expected_checksum in checksums.items():
        require(
            sha256(root / name) == expected_checksum,
            "bundle checksum mismatch: " + name,
        )
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_names = [
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        ]
        require(len(metadata_names) == 1, "wheel must contain one metadata record")
        metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))
        require(metadata["Name"] == "kohaku-kt-mcp", "unexpected wheel project")
        version = metadata["Version"]
    require(
        bool(version) and wheels[0].name == f"kohaku_kt_mcp-{version}-py3-none-any.whl",
        "wheel filename and metadata versions differ",
    )
    require(root_name == f"kt-mcp-{version}-linux", "bundle and wheel versions differ")
    return root, version


def clean_environment():
    environment = dict(os.environ)
    for key in (
        "KT_MCP_TOKEN",
        "TUNNEL_TOKEN",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_USER",
    ):
        environment.pop(key, None)
    return environment


def command(arguments, *, cwd, environment, timeout=60) -> str:
    result = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    require(
        result.returncode == 0,
        f"{Path(arguments[0]).name} verification command failed (exit {result.returncode})",
    )
    return result.stdout


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def wait_ready(url, process):
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process is not None:
                require(
                    process.poll() is None, "installed daemon exited during startup"
                )
            try:
                if (await client.get(url.rsplit("/", 1)[0])).status_code == 404:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError("installed daemon did not become ready")


async def verify_tools(url, version, workspace, interpreter, report):
    async with (
        httpx.AsyncClient(trust_env=False, timeout=30) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        require(
            initialized.serverInfo.version == version, "installed MCP version differs"
        )
        advertised = {tool.name for tool in (await session.list_tools()).tools}
        require(advertised == TOOLS, "installed MCP tool inventory differs")
        passed = set()

        async def call(name, arguments=None):
            result = await asyncio.wait_for(
                session.call_tool(name, arguments or {}), timeout=30
            )
            require(not result.isError, "installed MCP tool failed: " + name)
            require(
                isinstance(result.structuredContent, dict),
                "missing structured tool result",
            )
            passed.add(name)
            report["tools"] = sorted(passed)
            return result.structuredContent

        await call("write", {"path": "sample 中文.txt", "content": "alpha beta\n"})
        require(
            "alpha beta" in (await call("read", {"path": "sample 中文.txt"}))["output"],
            "file read failed",
        )
        await call(
            "edit",
            {"path": "sample 中文.txt", "old_string": "alpha", "new_string": "gamma"},
        )
        await call(
            "multi_edit",
            {
                "path": "sample 中文.txt",
                "edits": [
                    {"old_string": "gamma", "new_string": "delta"},
                    {"old_string": "beta", "new_string": "omega"},
                ],
            },
        )
        require(
            (workspace / "sample 中文.txt").read_text(encoding="utf-8")
            == "delta omega\n",
            "file editing produced unexpected contents",
        )
        await call("write", {"path": "nested/child.txt", "content": "needle\n"})
        require(
            "nested/child.txt"
            in (await call("glob", {"pattern": "**/*.txt"}))["files"],
            "glob missed a workspace file",
        )
        require(
            any(
                item["path"] == "nested/child.txt"
                for item in (await call("grep", {"pattern": "needle"}))["matches"]
            ),
            "grep missed content",
        )
        require(
            "nested/child.txt" in (await call("tree", {"depth": 3}))["entries"],
            "tree missed a workspace file",
        )
        shell = await call(
            "bash", {"command": "printf bash-ok > bash-output.txt; printf bash-stdout"}
        )
        require(
            shell["output"] == "bash-stdout"
            and (workspace / "bash-output.txt").read_text() == "bash-ok",
            "Bash execution failed",
        )
        python = await call(
            "python",
            {
                "code": (
                    "import json,sys,kt_mcp,importlib.metadata; "
                    "from pathlib import Path; Path('python-output.txt').write_text('python-ok'); "
                    "dist=importlib.metadata.distribution('kohaku-kt-mcp'); "
                    "direct=json.loads(dist.read_text('direct_url.json') or '{}'); "
                    "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
                    "'module':kt_mcp.__file__,'version':kt_mcp.__version__,"
                    "'editable':direct.get('dir_info',{}).get('editable',False)}))"
                )
            },
        )
        worker = json.loads(python["output"])
        require(
            Path(worker["executable"]).absolute() == interpreter.absolute(),
            "Python tool did not use the installed venv interpreter",
        )
        require(
            Path(worker["prefix"]).absolute() == interpreter.parent.parent.absolute(),
            "Python tool did not retain the installed venv",
        )
        require(
            Path(worker["module"])
            .absolute()
            .is_relative_to(interpreter.parent.parent.absolute())
            and not worker["editable"],
            "verification accidentally used an editable/source package",
        )
        require(worker["version"] == version, "Python worker package version differs")
        report["installed_venv_worker"] = True
        job = await call(
            "python",
            {
                "code": "import time; time.sleep(0.2); print('job-done')",
                "run_in_background": True,
            },
        )
        finished = await call("job_wait", {"job_id": job["job_id"], "timeout": 10})
        require(
            finished["state"] == "completed"
            and finished["result"]["output"].strip() == "job-done",
            "background job did not finish",
        )
        pending = await call(
            "python", {"code": "import time; time.sleep(10)", "run_in_background": True}
        )
        require(
            (await call("job_status", {"job_id": pending["job_id"]}))["state"]
            == "running",
            "background job was not retained",
        )
        require(
            (await call("job_cancel", {"job_id": pending["job_id"]}))["state"]
            == "cancelled",
            "background job did not cancel",
        )
        require(passed == TOOLS, "not all twelve tools were exercised")
        report["tool_count"] = len(passed)


async def verify_connection(url, version):
    async with (
        httpx.AsyncClient(trust_env=False, timeout=10) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        require(
            (await session.initialize()).serverInfo.version == version,
            "MCP connection failed after TUI exit",
        )


def terminal_attributes_at(captured, text):
    """Check attributes where text was drawn in the first real terminal frame."""
    position = captured.find(text.encode("utf-8"))
    require(position >= 0, "installed TUI did not draw the expected label")
    bold = False
    foreground = None
    for match in re.finditer(rb"\x1b\[([0-9;]*)m", captured[:position]):
        parameters = iter(int(value or b"0") for value in match[1].split(b";"))
        for value in parameters:
            if value == 0:
                bold, foreground = False, None
            elif value == 1:
                bold = True
            elif value == 22:
                bold = False
            elif 30 <= value <= 37:
                foreground = value - 30
            elif 90 <= value <= 97:
                foreground = value - 90 + 8
            elif value == 39:
                foreground = None
            elif value in (38, 48):
                mode = next(parameters, None)
                color = tuple(
                    next(parameters, None) for _ in range(3 if mode == 2 else 1)
                )
                if value == 38:
                    foreground = color
    return bold, foreground


def verify_tui(cli, config, name, *, cwd, environment, url):
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()
    process = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        terminal_environment = {
            **environment,
            "TERM": "xterm-256color",
            "LC_ALL": "C.UTF-8",
        }
        terminal_environment.pop("NO_COLOR", None)
        process = subprocess.Popen(
            [str(cli), "tui", "--config", str(config), "--name", name],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=terminal_environment,
            start_new_session=True,
        )
        os.close(slave)
        slave = None
        captured = bytearray()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            require(process.poll() is None, "installed TUI exited before rendering")
            if select.select([master], [], [], 0.1)[0]:
                captured.extend(os.read(master, 65536))
                del captured[:-200_000]
                if b"[b] Instances [q] Quit" in captured:
                    break
        else:
            raise TimeoutError("installed TUI did not render")
        plain = re.sub(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([0-~])", b"", captured)
        require(b"MCP link" in plain, "installed TUI did not label its MCP link")
        require(
            url.encode("utf-8") in plain,
            "installed TUI did not display the complete MCP link by default",
        )
        require(
            terminal_attributes_at(captured, "MCP link")[0],
            "installed TUI link heading is not bold",
        )
        bold, foreground = terminal_attributes_at(captured, url)
        require(
            bold and foreground is not None,
            "installed TUI link did not use bold and color in the actual terminal",
        )
        os.write(master, b"q")
        require(process.wait(timeout=10) == 0, "installed TUI did not exit cleanly")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        os.close(master)
        if slave is not None:
            os.close(slave)


def verify_native_creation(cli, *, cwd, environment):
    """Create through the installed launcher, with no editable package or test stub."""
    import fcntl
    import pty
    import termios

    name = "kt-mcp-wheel-" + uuid.uuid4().hex[:10]
    workspace = cwd / "native workspace 中文"
    workspace.mkdir()
    config_home = cwd / "native-tui-configuration"
    target = config_home / "kt-mcp/instances" / (name + ".json")
    port = available_port()
    terminal_environment = {
        **environment,
        "TERM": "xterm-256color",
        "LC_ALL": "C.UTF-8",
        "XDG_CONFIG_HOME": str(config_home),
    }
    terminal_environment.pop("NO_COLOR", None)
    master, slave = pty.openpty()
    process = None
    captured = bytearray()
    position = 0
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 64, 0, 0))
        process = subprocess.Popen(
            [str(cli), "tui"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=terminal_environment,
            start_new_session=True,
        )
        os.close(slave)
        slave = None

        def native():
            flags = termios.tcgetattr(master)[3]
            require(
                not flags & termios.ICANON and not flags & termios.ECHO,
                "installed new-instance form left native terminal input",
            )
            require(
                re.search(rb"\x1b\[\?(?:1049|1047|47)l", captured) is None,
                "installed new-instance form exited curses",
            )

        def expect(label):
            nonlocal position
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                plain = re.sub(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([0-~])", b"", captured)
                # These ordered stages each introduce a new prompt. Matching is
                # restricted to bytes after the preceding matched prompt.
                found = plain.find(label.encode("utf-8"), position)
                if found >= 0:
                    position = found + len(label.encode("utf-8"))
                    native()
                    return
                require(process.poll() is None, "installed creation TUI exited early")
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        raise RuntimeError(
                            "installed creation TUI closed its terminal"
                        ) from None
                    require(
                        bool(chunk), "installed creation TUI stopped producing output"
                    )
                    captured.extend(chunk)
                    require(
                        len(captured) < 2_000_000,
                        "installed creation TUI output exceeded its limit",
                    )
            raise TimeoutError("installed creation TUI did not reach " + label)

        def answer(label, value=""):
            expect(label)
            os.write(master, (value + "\n").encode("utf-8"))

        expect("Instances")
        os.write(master, b"n")
        answer("Instance name [", name)
        answer("Workspace [", str(workspace))
        answer("Port [", str(port))
        answer("Mode [", "local")
        answer("Save?")
        expect("Start instance now?")
        # No is the default; no service should be installed by this operation.
        expect("No, return")
        os.write(master, b"\n")
        expect("Selected: " + name)
        saved = json.loads(target.read_text(encoding="utf-8"))
        require(
            saved["workspace"] == str(workspace)
            and saved["port"] == port
            and saved["mode"] == "local",
            "installed native creation saved different settings",
        )
        require(
            bool(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", saved["token"]))
            and target.stat().st_mode & 0o777 == 0o600,
            "installed native creation did not save a private independent token",
        )
        require(
            not (config_home / "systemd/user" / (name + ".service")).exists(),
            "installed native creation registered a service after declining Start",
        )
        native()
        os.write(master, b"q")
        require(
            process.wait(timeout=10) == 0, "installed creation TUI did not exit cleanly"
        )
        return {
            "created_through_installed_launcher": True,
            "native_curses": True,
            "terminal": "64x18",
            "workspace_port_mode_verified": True,
            "private_token": True,
            "declined_start_without_unit": True,
        }
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        os.close(master)
        if slave is not None:
            os.close(slave)


def verify(arguments, report, sensitive):
    environment = clean_environment()
    with tempfile.TemporaryDirectory(prefix="kt-mcp-linux-verify-") as temporary:
        root = Path(temporary)
        report["stage"] = "archive integrity"
        bundle, version = unpack_verified(arguments.archive, root / "unpacked")
        report["version"] = version
        report["archive_sha256"] = sha256(arguments.archive)
        special = 'spaces % $ ${HOME} "quote"'
        prefix = root / ("install " + special)
        bin_dir = root / ("bin " + special)
        workspace = root / ("workspace 中文 " + special)
        workspace.mkdir()
        config = root / ("config " + special + ".json")
        report["stage"] = "fresh wheel installation"
        command(
            [
                "bash",
                bundle / "install.sh",
                "--prefix",
                prefix,
                "--bin-dir",
                bin_dir,
                "--python",
                sys.executable,
            ],
            cwd=root,
            environment=environment,
            timeout=300,
        )
        cli = bin_dir / "kt-mcp"
        interpreter = prefix / "venv/bin/python"
        require(
            command([cli, "--version"], cwd=root, environment=environment).strip()
            == "Kohaku KT MCP " + version,
            "installed CLI version differs",
        )
        port = available_port()
        command(
            [
                cli,
                "init",
                "--config",
                config,
                "--workspace",
                workspace,
                "--mode",
                "local",
                "--port",
                str(port),
            ],
            cwd=root,
            environment=environment,
        )
        token = json.loads(config.read_text(encoding="utf-8"))["token"]
        sensitive.append(token)
        url = f"http://127.0.0.1:{port}/mcp/{token}"
        require(
            command(
                [cli, "link", "--config", config], cwd=root, environment=environment
            )
            == url + "\n",
            "installed link export did not return one complete plain URL",
        )
        report["link_export"] = "passed; selected configuration returned one plain URL"
        name = "kt-mcp-verify-" + uuid.uuid4().hex[:12]
        process = None
        unit_path = None
        unit_attempted = False
        log_path = root / "daemon.log"
        report["stage"] = "installed service startup"

        def service_status():
            return json.loads(
                command(
                    [cli, "service", "status", "--name", name],
                    cwd=root,
                    environment=environment,
                )
            )

        with log_path.open("wb") as log:
            try:
                if arguments.systemd:
                    unit_path = Path(
                        command(
                            [
                                interpreter,
                                "-c",
                                "import sys; from kt_mcp.service import ServiceManager; print(ServiceManager(sys.argv[1]).unit_path)",
                                name,
                            ],
                            cwd=root,
                            environment=environment,
                        ).strip()
                    )
                    require(
                        not unit_path.exists() and not unit_path.is_symlink(),
                        "temporary verification service name already exists",
                    )
                    require(
                        service_status()["LoadState"] == "not-found",
                        "temporary service name is already loaded",
                    )
                    unit_attempted = True
                    report["temporary_unit"] = name + ".service"
                    report["temporary_unit_path"] = str(unit_path)
                    command(
                        [
                            cli,
                            "service",
                            "install",
                            "--config",
                            config,
                            "--name",
                            name,
                            "--now",
                        ],
                        cwd=root,
                        environment=environment,
                    )
                else:
                    process = subprocess.Popen(
                        [str(cli), "daemon", "--config", str(config)],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=root,
                        env=environment,
                        start_new_session=True,
                    )
                asyncio.run(wait_ready(url, process))
                report["stage"] = "all twelve MCP tools"
                asyncio.run(verify_tools(url, version, workspace, interpreter, report))
                if arguments.systemd:
                    report["stage"] = "installed TUI lifecycle"
                    before = service_status()
                    require(
                        before["ActiveState"] == "active"
                        and int(before["MainPID"]) > 0,
                        "verification service is not active",
                    )
                    verify_tui(
                        cli,
                        config,
                        name,
                        cwd=root,
                        environment=environment,
                        url=url,
                    )
                    report["stage"] = "installed native new-instance form"
                    report["tui_native_creation"] = verify_native_creation(
                        cli, cwd=root, environment=environment
                    )
                    after = service_status()
                    require(
                        after["ActiveState"] == "active"
                        and after["MainPID"] == before["MainPID"],
                        "exiting TUI changed the running service",
                    )
                    asyncio.run(verify_connection(url, version))
                    report["tui"] = (
                        "passed; default link and terminal styling checked; "
                        "service PID unchanged and MCP reachable"
                    )
                    service_logs = command(
                        [cli, "service", "logs", "--name", name, "--lines", "1000"],
                        cwd=root,
                        environment=environment,
                    )
                    require(
                        bool(service_logs.strip()),
                        "verification service journal is empty",
                    )
                    require(
                        token not in service_logs,
                        "installed service leaked its access token in journal",
                    )
                    report["credential_log_check"] = (
                        "passed; checked the unique unit journal"
                    )
            finally:
                report["cleanup"] = "pending"
                if unit_attempted and unit_path is not None and unit_path.exists():
                    command(
                        [cli, "service", "uninstall", "--name", name],
                        cwd=root,
                        environment=environment,
                    )
                    require(not unit_path.exists(), "verification unit was not removed")
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=35)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        raise RuntimeError(
                            "installed daemon required a forced shutdown"
                        ) from None
                require(
                    not config.with_suffix(".status.json").exists(),
                    "daemon status remained after shutdown",
                )
                report["cleanup"] = "passed"
        if not arguments.systemd:
            require(
                token not in log_path.read_text(encoding="utf-8", errors="replace"),
                "installed daemon leaked its access token in logs",
            )
            report["credential_log_check"] = "passed; checked daemon output"
    report["stage"] = "complete"
    report["success"] = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument(
        "--systemd",
        action="store_true",
        help="Exercise a unique user unit and a real TUI terminal",
    )
    parser.add_argument("--report", type=Path, default=ROOT / "dist/linux-smoke.json")
    arguments = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.error("verification requires Linux and an ordinary non-root user")
    arguments.archive = arguments.archive.expanduser().absolute()
    report = {
        "success": False,
        "version": None,
        "platform": platform.platform(),
        "verification_python": platform.python_version(),
        "mode": "systemd-user" if arguments.systemd else "foreground",
        "tools": [],
        "tool_count": 0,
        "installed_venv_worker": False,
        "tui": "not requested",
        "tui_native_creation": "not requested",
        "cleanup": "not started",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    sensitive = []
    try:
        verify(arguments, report, sensitive)
    except Exception as exc:  # noqa: BLE001 -- sanitize failures into a nonzero report
        message = str(exc)
        for secret in sensitive:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"/mcp/[A-Za-z0-9_-]+", "/mcp/[REDACTED]", message)
        report["error"] = type(exc).__name__ + ": " + message[:2000]
    path = arguments.report.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Verification report:", path)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
