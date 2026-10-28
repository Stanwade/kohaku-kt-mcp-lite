"""Offline integration coverage for the curl-to-bash bootstrap."""

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the online installer targets ordinary Linux users"
)
ROOT = Path(__file__).resolve().parents[2]
RELEASES = "https://github.com/Stanwade/kohaku-kt-mcp-lite/releases"
LATEST = RELEASES + "/latest/download/SHA256SUMS"


def executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


class OfflineInstall:
    def __init__(self, root):
        self.root = root
        self.cwd = root / "caller workspace 中文"
        self.assets = root / "offline assets"
        self.bin = root / "fake bin"
        self.temporary = root / "temporary downloads 中文"
        self.home = root / "home"
        for path in (self.cwd, self.assets, self.bin, self.temporary, self.home):
            path.mkdir()
        self.route_file = root / "curl-routes.json"
        self.curl_log = root / "curl-log.jsonl"
        self.install_log = root / "installed.json"
        self.routes = {}
        self.python = sys.executable
        self.bash = shutil.which("bash")
        if not self.bash:
            pytest.skip("bash is required to exercise the installer")
        (self.bin / "python3").symlink_to(self.python)
        curl_program = root / "fake_curl.py"
        curl_program.write_text(
            """import json, os, sys
from pathlib import Path
arguments = sys.argv[1:]
try:
    output = arguments[arguments.index("--output") + 1]
    url = arguments[-1]
except (ValueError, IndexError):
    raise SystemExit("fixture curl requires --output FILE URL")
log = Path(os.environ["KT_MCP_TEST_CURL_LOG"])
previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"url": url, "arguments": arguments}) + "\\n")
routes = json.loads(Path(os.environ["KT_MCP_TEST_CURL_ROUTES"]).read_text())
if url not in routes:
    raise SystemExit("unexpected URL in offline test: " + url)
route = routes[url]
if isinstance(route, list):
    index = sum(entry["url"] == url for entry in previous)
    route = route[min(index, len(route) - 1)]
data = Path(route["source"]).read_bytes() if "source" in route else b""
if "limit" in route:
    data = data[:route["limit"]]
Path(output).write_bytes(data)
raise SystemExit(route.get("exit_code", 0))
""",
            encoding="utf-8",
        )
        executable(
            self.bin / "curl",
            "#!/bin/sh\nexec "
            + shlex.quote(self.python)
            + " -I "
            + shlex.quote(str(curl_program))
            + ' "$@"\n',
        )
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.defpath,
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "TMPDIR": str(self.temporary),
            "KT_MCP_TEST_CURL_ROUTES": str(self.route_file),
            "KT_MCP_TEST_CURL_LOG": str(self.curl_log),
            "KT_MCP_TEST_INSTALL_LOG": str(self.install_log),
        }
        for key in ("BASH_ENV", "ENV", "PYTHONHOME", "PYTHONPATH"):
            self.env.pop(key, None)

    def installer_script(self):
        program = (
            "import json,os,sys; from pathlib import Path; "
            "record={'arguments':sys.argv[2:],'script':os.path.abspath(sys.argv[1]),"
            "'cwd':os.getcwd(),'stdin':sys.stdin.read(),"
            "'python_environment':{key:os.environ.get(key) for key in "
            "('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP')}}; "
            "Path(os.environ['KT_MCP_TEST_INSTALL_LOG']).write_text("
            "json.dumps(record,ensure_ascii=False),encoding='utf-8'); "
            "print('fixture local installer completed')"
        )
        return (
            "#!/usr/bin/env bash\nset -euo pipefail\nexec "
            + shlex.quote(self.python)
            + " -c "
            + shlex.quote(program)
            + ' "$0" "$@"\n'
        ).encode()

    def release(self, version="9.8.7", *, unsafe=None, wheel_data=None):
        bundle_root = f"kt-mcp-{version}-linux"
        wheel = f"kohaku_kt_mcp-{version}-py3-none-any.whl"
        files = {
            wheel: (
                wheel_data
                if wheel_data is not None
                else b"offline fixture wheel; the fixture installer never invokes pip"
            ),
            "install.sh": self.installer_script(),
            "README.md": b"Offline fixture README\n",
            "UBUNTU.md": b"Offline fixture Ubuntu guide\n",
            "LICENSE": b"Offline fixture license\n",
            "NOTICE": b"Offline fixture notice\n",
        }
        files["SHA256SUMS"] = "".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n"
            for name, data in sorted(files.items())
        ).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            directory = tarfile.TarInfo(bundle_root)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)
            for name, data in files.items():
                member = tarfile.TarInfo(f"{bundle_root}/{name}")
                member.mode = 0o755 if name == "install.sh" else 0o644
                if name == "README.md" and unsafe in {"symlink", "hardlink"}:
                    member.type = (
                        tarfile.SYMTYPE if unsafe == "symlink" else tarfile.LNKTYPE
                    )
                    member.linkname = str(self.root / "escaped-payload")
                    archive.addfile(member)
                else:
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            if unsafe in {"traversal", "absolute"}:
                name = (
                    bundle_root + "/../../escaped-payload"
                    if unsafe == "traversal"
                    else str(self.root / "escaped-payload")
                )
                member = tarfile.TarInfo(name)
                member.size = 6
                archive.addfile(member, io.BytesIO(b"escape"))
        archive_name = bundle_root + ".tar.gz"
        archive_path = self.assets / archive_name
        archive_path.write_bytes(buffer.getvalue())
        manifest_path = self.assets / (version + "-SHA256SUMS")
        manifest_path.write_text(
            f"{hashlib.sha256(archive_path.read_bytes()).hexdigest()}  {archive_name}\n"
            f"{hashlib.sha256(files[wheel]).hexdigest()}  {wheel}\n"
            f"{hashlib.sha256(b'source fixture').hexdigest()}  kohaku_kt_mcp-{version}.tar.gz\n",
            encoding="ascii",
        )
        manifest_url = RELEASES + f"/download/v{version}/SHA256SUMS"
        archive_url = RELEASES + f"/download/v{version}/{archive_name}"
        self.routes[manifest_url] = {"source": str(manifest_path)}
        self.routes[archive_url] = {"source": str(archive_path)}
        self.routes.setdefault(LATEST, {"source": str(manifest_path)})
        return SimpleNamespace(
            version=version,
            archive=archive_path,
            manifest=manifest_path,
            archive_url=archive_url,
            manifest_url=manifest_url,
        )

    def run(self, *arguments, as_root=False):
        self.route_file.write_text(json.dumps(self.routes), encoding="utf-8")
        # The long trailing comment remains in the same pipe. A child reading the
        # bootstrap's stdin would consume it instead of receiving an empty stream.
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        source += "\n# " + "STREAM_SENTINEL " * 8192 + "\n"
        command = [self.bash, "-s", "--", *arguments]
        if as_root and os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo:
                pytest.skip("no root context or sudo is available")
            access = subprocess.run(
                [sudo, "-n", "true"], capture_output=True, timeout=5, check=False
            )
            if access.returncode:
                pytest.skip(
                    "root rejection needs an existing noninteractive sudo grant"
                )
            selected = {
                key: value
                for key, value in self.env.items()
                if key in {"PATH", "HOME", "XDG_DATA_HOME", "TMPDIR"}
                or key.startswith("KT_MCP_TEST_")
            }
            command = [
                sudo,
                "-n",
                "/usr/bin/env",
                "-i",
                *(f"{key}={value}" for key, value in selected.items()),
                *command,
            ]
        return subprocess.run(
            command,
            input=source,
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

    def urls(self):
        if not self.curl_log.exists():
            return []
        return [
            json.loads(line)["url"]
            for line in self.curl_log.read_text(encoding="utf-8").splitlines()
        ]

    def record(self):
        return json.loads(self.install_log.read_text(encoding="utf-8"))

    def options(self):
        arguments = iter(self.record()["arguments"])
        options = {}
        for argument in arguments:
            if argument == "--no-link":
                options[argument] = True
            else:
                assert argument in {"--prefix", "--bin-dir", "--python"}
                options[argument] = next(arguments)
        return options

    def assert_clean(self):
        assert list(self.temporary.iterdir()) == []
        assert not list(self.root.rglob("escaped-payload"))


@pytest.fixture
def offline_install(tmp_path):
    return OfflineInstall(tmp_path)


@pytest.fixture
def ordinary_install(offline_install):
    if os.geteuid() == 0:
        pytest.skip("successful installation requires an ordinary Linux account")
    return offline_install


def test_latest_is_resolved_once_and_user_targets_survive_private_execution(
    ordinary_install,
):
    fixture = ordinary_install
    release = fixture.release()
    next_release = fixture.release("9.8.8")
    fixture.routes[LATEST] = [
        {"source": str(release.manifest)},
        {"source": str(next_release.manifest)},
    ]
    tools = fixture.cwd / "tools"
    tools.mkdir()
    selected_python = tools / "python with space"
    selected_python.symlink_to(sys.executable)
    arguments = [
        "--prefix",
        'relative install 中文 % $ "quote"',
        "--bin-dir",
        "./commands with 'apostrophe'",
        "--python",
        "./tools/python with space",
        "--no-link",
    ]

    result = fixture.run(*arguments)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.urls() == [LATEST, release.archive_url]
    record = fixture.record()
    options = fixture.options()
    assert options == {
        "--prefix": os.path.abspath(fixture.cwd / arguments[1]),
        "--bin-dir": os.path.abspath(fixture.cwd / arguments[3]),
        "--python": str(selected_python),
        "--no-link": True,
    }
    assert Path(record["cwd"]) == Path(record["script"]).parent
    assert Path(record["cwd"]).is_relative_to(fixture.temporary)
    assert record["stdin"] == ""
    assert not Path(record["script"]).exists()
    assert "export PATH=" not in result.stdout
    fixture.assert_clean()


@pytest.mark.parametrize("version", ["9.8.7", "v9.8.7"])
def test_explicit_version_is_pinned_and_prints_a_quoted_path_hint(
    ordinary_install, version
):
    fixture = ordinary_install
    release = fixture.release()
    bin_dir = "./commands 中文 with 'quote'"

    result = fixture.run("--version", version, "--bin-dir", bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fixture.urls() == [release.manifest_url, release.archive_url]
    assert fixture.options()["--bin-dir"] == os.path.abspath(fixture.cwd / bin_dir)
    assert fixture.options()["--python"] == str(fixture.bin / "python3")
    expected = (
        "export PATH=" + shlex.quote(str((fixture.cwd / bin_dir).resolve())) + ":$PATH"
    )
    assert expected in result.stdout
    fixture.assert_clean()


def test_legacy_installer_is_isolated_from_project_modules_and_python_environment(
    ordinary_install,
):
    fixture = ordinary_install
    fixture.release()
    for name in ("venv.py", "json.py"):
        (fixture.cwd / name).write_text(
            "raise RuntimeError('workspace shadow module was imported')\n",
            encoding="utf-8",
        )
    startup = fixture.cwd / "python-startup.py"
    startup.write_text(
        "raise RuntimeError('unexpected Python startup')\n", encoding="utf-8"
    )
    fixture.env.update(
        {
            "XDG_DATA_HOME": "relative user data",
            "PYTHONPATH": str(fixture.cwd),
            "PYTHONHOME": str(fixture.root / "missing Python home"),
            "PYTHONSTARTUP": str(startup),
        }
    )

    result = fixture.run("--no-link")

    assert result.returncode == 0, result.stdout + result.stderr
    record = fixture.record()
    assert Path(record["cwd"]) == Path(record["script"]).parent
    assert Path(record["cwd"]).is_relative_to(fixture.temporary)
    assert fixture.options()["--prefix"] == str(
        fixture.cwd / "relative user data/kt-mcp"
    )
    assert fixture.options()["--bin-dir"] == str(fixture.home / ".local/bin")
    assert record["python_environment"] == {
        "PYTHONPATH": None,
        "PYTHONHOME": None,
        "PYTHONSTARTUP": None,
    }
    assert record["stdin"] == ""
    fixture.assert_clean()


@pytest.mark.parametrize("failure", ["checksum", "404", "truncated"])
def test_failed_or_incomplete_download_never_executes_the_local_installer(
    ordinary_install, failure
):
    fixture = ordinary_install
    release = fixture.release()
    if failure == "checksum":
        text = release.manifest.read_text(encoding="ascii")
        release.manifest.write_text("0" * 64 + text[64:], encoding="ascii")
    elif failure == "404":
        fixture.routes[release.archive_url] = {"exit_code": 22}
    else:
        fixture.routes[release.archive_url]["limit"] = (
            release.archive.stat().st_size // 2
        )

    result = fixture.run()

    assert result.returncode != 0
    assert not fixture.install_log.exists()
    assert ("download failed" if failure == "404" else "checksum") in (
        result.stdout + result.stderr
    ).lower()
    fixture.assert_clean()


@pytest.mark.parametrize("unsafe", ["traversal", "absolute", "symlink", "hardlink"])
def test_unsafe_archive_is_rejected_before_extracting_or_installing(
    ordinary_install, unsafe
):
    fixture = ordinary_install
    fixture.release(unsafe=unsafe)

    result = fixture.run()

    assert result.returncode != 0
    assert "unsafe archive" in (result.stdout + result.stderr).lower()
    assert not fixture.install_log.exists()
    fixture.assert_clean()


def test_missing_venv_fails_before_network_with_an_ubuntu_fix(ordinary_install):
    fixture = ordinary_install
    wrapper = executable(
        fixture.cwd / "python without venv",
        "#!/bin/sh\n"
        'case "$*" in *ensurepip*) exit 1 ;; esac\n'
        "exec " + shlex.quote(sys.executable) + ' "$@"\n',
    )

    result = fixture.run("--python", str(wrapper))

    assert result.returncode != 0
    assert "python3-venv" in result.stdout + result.stderr
    assert fixture.urls() == []
    assert not fixture.install_log.exists()
    fixture.assert_clean()


def test_missing_curl_fails_before_network_or_temporary_files(ordinary_install):
    fixture = ordinary_install
    minimal = fixture.root / "without curl"
    minimal.mkdir()
    for name in ("bash", "cat", "dirname", "id", "mktemp", "rm", "uname"):
        command = shutil.which(name)
        if command:
            (minimal / name).symlink_to(command)
    (minimal / "python3").symlink_to(sys.executable)
    fixture.env["PATH"] = str(minimal)

    result = fixture.run()

    assert result.returncode != 0
    assert "curl" in (result.stdout + result.stderr).lower()
    assert fixture.urls() == []
    assert not fixture.install_log.exists()
    fixture.assert_clean()


def test_invalid_version_is_rejected_without_network(ordinary_install):
    fixture = ordinary_install

    result = fixture.run("--version", "../v9.8.7")

    assert result.returncode != 0
    assert "version" in (result.stdout + result.stderr).lower()
    assert fixture.urls() == []
    assert not fixture.install_log.exists()
    fixture.assert_clean()


def test_help_does_not_download_or_create_installer_state(offline_install):
    result = offline_install.run("--help")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Usage:" in result.stdout
    assert offline_install.urls() == []
    assert not offline_install.install_log.exists()
    offline_install.assert_clean()


def test_root_is_rejected_before_network(offline_install):
    result = offline_install.run(as_root=True)

    assert result.returncode != 0
    assert "without sudo" in (result.stdout + result.stderr).lower()
    assert offline_install.urls() == []
    assert not offline_install.install_log.exists()
    offline_install.assert_clean()


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--name", "kt-mcp-project"], "--name requires --update"),
        (
            ["--update", "--bin-dir", "./different-bin"],
            "preserves existing command links",
        ),
        (["--update", "--no-link"], "preserves existing command links"),
    ],
)
def test_update_options_are_checked_before_download_or_installation(
    offline_install, arguments, message
):
    result = offline_install.run(*arguments)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
    assert offline_install.urls() == []
    assert not offline_install.install_log.exists()
    offline_install.assert_clean()


def test_release_without_update_modules_cannot_modify_an_existing_install(
    ordinary_install,
):
    fixture = ordinary_install
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as package:
        package.writestr("kt_mcp/__init__.py", '__version__ = "0.2.3"\n')
        package.writestr(
            "kohaku_kt_mcp-0.2.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: kohaku-kt-mcp\nVersion: 0.2.3\n",
        )
    release = fixture.release("0.2.3", wheel_data=wheel.getvalue())
    prefix = fixture.root / "old custom installation"
    (prefix / "venv").mkdir(parents=True)
    saved = prefix / "venv/old-package"
    saved.write_bytes(b"existing installation data")
    result = fixture.run(
        "--update", "--version", release.version, "--prefix", str(prefix)
    )
    assert result.returncode != 0
    assert "usable automatic updater" in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert fixture.urls() == [release.manifest_url, release.archive_url]
    assert saved.read_bytes() == b"existing installation data"
    assert not fixture.install_log.exists()
    fixture.assert_clean()
