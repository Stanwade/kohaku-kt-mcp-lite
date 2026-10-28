import builtins
import json
import os
import stat
import sys
from dataclasses import replace

import pytest

from kt_mcp.daemon import DaemonConfig, load_config, save_config
from kt_mcp.desktop import DesktopSettings
from kt_mcp.onboarding import ConfigEditor, SetupCancelled, edit_config, setup_config

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux first-run onboarding"
)
MCP_TOKEN = "onboarding-test-token-" + "a" * 32
TUNNEL_TOKEN = "private-cloudflare-tunnel-token"


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch):
    monkeypatch.delenv("KT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("TUNNEL_TOKEN", raising=False)


class Inputs:
    def __init__(self, values, config_path):
        self.remaining = list(values)
        self.config_path = config_path
        self.prompts = []

    def __call__(self, prompt):
        assert (
            not self.config_path.exists()
        ), "setup wrote configuration before confirmation"
        self.prompts.append(prompt)
        if not self.remaining:
            pytest.fail(f"Unexpected additional prompt: {prompt}")
        value = self.remaining.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def no_secret(prompt):
    pytest.fail("this setup must not request a tunnel credential")


def test_enter_accepts_visible_current_directory_and_saves_only_after_review(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "current workspace 中文"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    path = tmp_path / "private/config.json"
    answers = Inputs(["", "", "", ""], path)
    monkeypatch.setattr(builtins, "input", answers)

    created = setup_config(path, ask_secret=no_secret)

    assert answers.remaining == []
    loaded = load_config(path)
    assert loaded == created
    assert loaded.settings.workspace == str(workspace.resolve())
    assert loaded.settings.port == 8765
    assert loaded.settings.mode == "local"
    assert loaded.tunnel_token == ""
    assert len(loaded.token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    output = capsys.readouterr()
    shown = output.out + output.err + "\n".join(answers.prompts)
    assert str(workspace.resolve()) in shown
    assert str(path) in shown
    assert "8765" in shown and "local" in shown.lower()
    assert "save" in answers.prompts[-1].lower()
    assert loaded.token not in shown
    assert "\x1b" not in shown


@pytest.mark.parametrize(
    "stdin_tty,stdout_tty,term",
    [
        (False, True, "xterm-256color"),
        (True, False, "xterm-256color"),
        (True, True, "dumb"),
        (True, True, ""),
    ],
    ids=["redirected-input", "redirected-output", "dumb-terminal", "missing-term"],
)
def test_setup_keeps_plain_text_when_terminal_styling_is_unavailable(
    tmp_path, monkeypatch, capsys, stdin_tty, stdout_tty, term
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout_tty)
    monkeypatch.setenv("TERM", term)
    path = tmp_path / "private/config.json"
    answers = Inputs(["", "", "", ""], path)

    setup_config(path, ask=answers, ask_secret=no_secret)

    output = capsys.readouterr()
    shown = output.out + output.err + "\n".join(answers.prompts)
    assert "\x1b" not in shown
    assert "Workspace" in shown and str(tmp_path) in shown
    assert "Save?" in answers.prompts[-1]
    assert load_config(path).settings.workspace == str(tmp_path)


@pytest.mark.parametrize("kind", ["relative", "home", "absolute"])
def test_custom_workspace_supports_relative_home_and_special_paths(
    tmp_path, monkeypatch, kind
):
    home = tmp_path / "home"
    home.mkdir()
    workspace = home / 'project 中文 with space % $ "quote"'
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(home)
    entered = {
        "relative": workspace.name,
        "home": "~/" + workspace.name,
        "absolute": str(workspace),
    }[kind]
    path = tmp_path / "private/config.json"
    answers = Inputs([entered, "", "", ""], path)

    setup_config(path, ask=answers, ask_secret=no_secret)

    assert answers.remaining == []
    assert load_config(path).settings.workspace == str(workspace.resolve())


def test_invalid_fields_retry_in_place_without_creating_directories_or_config(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "missing"
    ordinary_file = tmp_path / "ordinary.txt"
    ordinary_file.write_text("preserve me", encoding="utf-8")
    path = tmp_path / "private/config.json"
    answers = Inputs(
        [
            str(missing),
            str(ordinary_file),
            str(workspace),
            "not-a-port",
            "0",
            "65536",
            "8123",
            "unsupported-mode",
            "2",
            "",
        ],
        path,
    )

    setup_config(path, ask=answers, ask_secret=no_secret)

    assert answers.remaining == []
    loaded = load_config(path)
    assert loaded.settings.workspace == str(workspace.resolve())
    assert loaded.settings.port == 8123
    assert loaded.settings.mode == "quick"
    assert not missing.exists()
    assert ordinary_file.read_text(encoding="utf-8") == "preserve me"


def test_back_preserves_valid_values_and_review_can_change_mode(tmp_path):
    workspace = tmp_path / "chosen"
    workspace.mkdir()
    path = tmp_path / "private/config.json"
    answers = Inputs(
        [str(workspace), "8123", ":back", "", "quick", ":back", "LOCAL", ""],
        path,
    )

    setup_config(path, ask=answers, ask_secret=no_secret)

    assert answers.remaining == []
    loaded = load_config(path)
    assert loaded.settings.workspace == str(workspace.resolve())
    assert loaded.settings.port == 8123
    assert loaded.settings.mode == "local"
    assert "8123" in answers.prompts[3]


def test_back_at_first_step_keeps_current_directory_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "private/config.json"
    answers = Inputs([":back", "", "", "", ""], path)

    setup_config(path, ask=answers, ask_secret=no_secret)

    assert answers.remaining == []
    assert load_config(path).settings.workspace == str(tmp_path.resolve())


def test_review_edit_can_change_workspace_without_losing_other_choices(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    path = tmp_path / "private/config.json"
    answers = Inputs(
        [str(first), "9123", "quick", "later", "edit", str(second), "", "", ""], path
    )

    setup_config(path, ask=answers, ask_secret=no_secret)

    assert answers.remaining == []
    loaded = load_config(path)
    assert loaded.settings.workspace == str(second.resolve())
    assert loaded.settings.port == 9123
    assert loaded.settings.mode == "quick"


def test_named_setup_retries_invalid_origin_and_hidden_empty_token(tmp_path, capsys):
    path = tmp_path / "private/config.json"
    answers = Inputs(
        [
            str(tmp_path),
            "9876",
            "named",
            "http://mcp.example.com",
            "https://mcp.example.com/path",
            "https://user:password@mcp.example.com",
            "https://mcp.example.com",
            "",
        ],
        path,
    )
    secrets = Inputs(["", TUNNEL_TOKEN], path)

    setup_config(path, ask=answers, ask_secret=secrets)

    assert answers.remaining == []
    assert secrets.remaining == []
    loaded = load_config(path)
    assert loaded.settings.port == 9876
    assert loaded.settings.mode == "named"
    assert loaded.settings.public_origin == "https://mcp.example.com"
    assert loaded.tunnel_token == TUNNEL_TOKEN
    output = capsys.readouterr()
    shown = output.out + output.err + "\n".join(answers.prompts + secrets.prompts)
    assert TUNNEL_TOKEN not in shown and loaded.token not in shown
    assert "user:password" not in shown


def test_named_setup_uses_environment_tokens_without_requesting_or_showing_them(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("KT_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.setenv("TUNNEL_TOKEN", TUNNEL_TOKEN)
    path = tmp_path / "private/config.json"
    answers = Inputs([str(tmp_path), "", "3", "https://mcp.example.com", ""], path)

    setup_config(path, ask=answers, ask_secret=no_secret)

    assert answers.remaining == []
    loaded = load_config(path)
    assert loaded.token == MCP_TOKEN
    assert loaded.tunnel_token == TUNNEL_TOKEN
    output = capsys.readouterr()
    shown = output.out + output.err + "\n".join(answers.prompts)
    assert MCP_TOKEN not in shown and TUNNEL_TOKEN not in shown


def test_returning_from_named_to_local_does_not_save_unused_tunnel_credentials(
    tmp_path, capsys
):
    path = tmp_path / "private/config.json"
    answers = Inputs(
        [
            str(tmp_path),
            "",
            "named",
            "https://mcp.example.com",
            ":back",
            ":back",
            "local",
            "",
        ],
        path,
    )
    secrets = Inputs([TUNNEL_TOKEN, ":back"], path)

    setup_config(path, ask=answers, ask_secret=secrets)

    assert answers.remaining == []
    assert secrets.remaining == []
    loaded = load_config(path)
    assert loaded.settings.mode == "local"
    assert loaded.settings.public_origin == ""
    assert loaded.tunnel_token == ""
    output = capsys.readouterr()
    shown = output.out + output.err + "\n".join(answers.prompts + secrets.prompts)
    assert TUNNEL_TOKEN not in shown and loaded.token not in shown


def test_supplied_defaults_are_preserved_when_accepted(tmp_path, monkeypatch):
    workspace = tmp_path / "selected"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "private/config.json"
    defaults = DesktopSettings(
        str(workspace), 9123, "quick", cloudflared="/usr/local/bin/cloudflared"
    )
    answers = Inputs(["", "", "", ""], path)

    setup_config(path, ask=answers, ask_secret=no_secret, defaults=defaults)

    assert answers.remaining == []
    assert load_config(path).settings == defaults


@pytest.mark.parametrize("at_review", [False, True], ids=["first-step", "review"])
@pytest.mark.parametrize(
    "abort", [":q", EOFError(), KeyboardInterrupt()], ids=["quit", "eof", "ctrl-c"]
)
def test_cancellation_never_creates_configuration_or_parent_directory(
    tmp_path, monkeypatch, at_review, abort
):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "private/config.json"
    values = ["", "", "", abort] if at_review else [abort]
    answers = Inputs(values, path)

    with pytest.raises(SetupCancelled):
        setup_config(path, ask=answers, ask_secret=no_secret)

    assert answers.remaining == []
    assert not path.exists()
    assert not path.parent.exists()


def test_cancellation_at_hidden_token_never_writes_partial_config(tmp_path):
    path = tmp_path / "private/config.json"
    answers = Inputs([str(tmp_path), "", "named", "https://mcp.example.com"], path)
    secrets = Inputs([":q"], path)

    with pytest.raises(SetupCancelled):
        setup_config(path, ask=answers, ask_secret=secrets)

    assert answers.remaining == []
    assert secrets.remaining == []
    assert not path.parent.exists()


def test_existing_configuration_is_preserved_before_any_prompt(tmp_path):
    path = tmp_path / "config.json"
    existing = DaemonConfig(DesktopSettings(str(tmp_path), mode="local"), MCP_TOKEN)
    save_config(existing, path)
    original = path.read_bytes()

    def must_not_prompt(prompt):
        pytest.fail("existing configuration must be preserved without prompting")

    with pytest.raises(FileExistsError):
        setup_config(path, ask=must_not_prompt, ask_secret=must_not_prompt)

    assert path.read_bytes() == original
    assert load_config(path) == existing


class EditInputs:
    def __init__(self, values, path):
        self.remaining = list(values)
        self.path = path
        self.original = path.read_bytes()
        self.prompts = []

    def __call__(self, prompt):
        assert self.path.read_bytes() == self.original, "editing saved before review"
        self.prompts.append(prompt)
        if not self.remaining:
            pytest.fail("The edit wizard requested an unexpected answer")
        value = self.remaining.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
def existing_configuration(tmp_path):
    workspace = tmp_path / "original workspace"
    workspace.mkdir()
    config = DaemonConfig(
        DesktopSettings(
            str(workspace),
            9123,
            "named",
            "https://old.example.com",
            "/opt/cloudflared",
        ),
        MCP_TOKEN,
        TUNNEL_TOKEN,
    )
    path = tmp_path / "existing config.json"
    save_config(config, path)
    return path, config


@pytest.mark.parametrize("replacement", ["", "replacement-tunnel-credential"])
def test_edit_prefills_saved_values_and_keeps_credentials_hidden(
    existing_configuration, tmp_path, monkeypatch, capsys, replacement
):
    path, original = existing_configuration
    workspace = tmp_path / 'new workspace 中文 % $ "quote"'
    workspace.mkdir()
    owner = path.stat().st_uid, path.stat().st_gid
    monkeypatch.setenv("KT_MCP_TOKEN", "unrelated-shell-token")
    monkeypatch.setenv("TUNNEL_TOKEN", "unrelated-shell-tunnel-token")
    answers = EditInputs(
        [str(workspace), "8123", "", "https://new.example.com", ""], path
    )
    hidden = EditInputs([replacement], path)

    edited = edit_config(path, ask=answers, ask_secret=hidden)

    assert not answers.remaining and not hidden.remaining
    assert edited.settings.workspace == str(workspace)
    assert edited.settings.port == 8123
    assert edited.settings.mode == "named"
    assert edited.settings.public_origin == "https://new.example.com"
    assert edited.settings.cloudflared == original.settings.cloudflared
    assert edited.token == original.token
    assert edited.tunnel_token == (replacement or original.tunnel_token)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["token"] == original.token
    assert saved["tunnel_token"] == edited.tunnel_token
    assert str(original.settings.workspace) in answers.prompts[0]
    assert "9123" in answers.prompts[1]
    assert "named" in answers.prompts[2]
    assert original.settings.public_origin in answers.prompts[3]
    assert "Enter keeps it" in hidden.prompts[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (path.stat().st_uid, path.stat().st_gid) == owner
    output = capsys.readouterr()
    shown = output.out + output.err + "\n".join(answers.prompts + hidden.prompts)
    for secret in (
        MCP_TOKEN,
        TUNNEL_TOKEN,
        replacement,
        "unrelated-shell-token",
        "unrelated-shell-tunnel-token",
    ):
        if secret:
            assert secret not in shown, "editing displayed a credential"
    assert "Configure" in shown


def test_edit_can_change_mode_without_rotating_the_access_token(existing_configuration):
    path, original = existing_configuration
    answers = EditInputs(["", "", "quick", ""], path)

    edited = edit_config(path, ask=answers, ask_secret=no_secret)

    assert edited.settings.mode == "quick"
    assert edited.settings.public_origin == ""
    assert edited.tunnel_token == ""
    assert edited.token == original.token


@pytest.mark.parametrize("abort", [":q", EOFError(), KeyboardInterrupt()])
def test_cancelled_edit_keeps_the_original_bytes_and_owner(
    existing_configuration, abort
):
    path, _ = existing_configuration
    original = path.read_bytes()
    info = path.stat()
    answers = EditInputs(["", "", "local", abort], path)

    with pytest.raises(SetupCancelled, match="configuration unchanged"):
        edit_config(path, ask=answers, ask_secret=no_secret)

    assert path.read_bytes() == original
    assert (path.stat().st_uid, path.stat().st_gid) == (info.st_uid, info.st_gid)
    assert path.stat().st_mtime_ns == info.st_mtime_ns


@pytest.mark.parametrize("change", ["content", "replacement", "permissions", "symlink"])
def test_edit_refuses_a_configuration_changed_during_the_wizard(
    existing_configuration, change
):
    path, config = existing_configuration
    original = path.read_bytes()
    answers = EditInputs(["", "", "local"], path)

    def ask(prompt):
        if answers.remaining:
            return answers(prompt)
        if change == "content":
            path.write_bytes(original + b"\n")
        elif change == "replacement":
            save_config(config, path, replace=True)
        elif change == "permissions":
            path.chmod(0o400)
        else:
            target = path.with_name("external-config.json")
            path.rename(target)
            path.symlink_to(target)
        return ""

    with pytest.raises(ValueError, match="changed outside"):
        edit_config(path, ask=ask, ask_secret=no_secret)

    assert path.read_bytes() == (original + b"\n" if change == "content" else original)
    assert path.is_symlink() == (change == "symlink")
    if change == "permissions":
        assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert not list(path.parent.glob("." + path.name + ".*"))


def test_failed_atomic_edit_keeps_the_original_file_and_cleans_temporary_data(
    existing_configuration, monkeypatch
):
    path, _ = existing_configuration
    original = path.read_bytes()
    answers = EditInputs(["", "", "local", ""], path)

    def fail_replace(*arguments):
        raise OSError("fixture filesystem publication failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failure"):
        edit_config(path, ask=answers, ask_secret=no_secret)

    assert path.read_bytes() == original
    assert not list(path.parent.glob("." + path.name + ".*"))


def test_editor_restores_exact_original_bytes_after_a_later_failure(
    existing_configuration,
):
    path, original = existing_configuration
    before = path.read_bytes() + b"\n"
    path.write_bytes(before)
    editor = ConfigEditor(path)
    edited = replace(original, settings=replace(original.settings, port=8123))

    editor.save(edited)
    assert editor.saved
    assert load_config(path).settings.port == 8123
    editor.restore()

    assert not editor.saved
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rollback_never_overwrites_an_external_configuration(existing_configuration):
    path, original = existing_configuration
    editor = ConfigEditor(path)
    editor.save(replace(original, settings=replace(original.settings, port=8123)))
    external = replace(original, settings=replace(original.settings, port=7123))
    save_config(external, path, replace=True)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="changed outside"):
        editor.restore()

    assert path.read_bytes() == before


def test_editor_refuses_access_token_changes(existing_configuration):
    path, original = existing_configuration
    editor = ConfigEditor(path)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="preserve the MCP access token"):
        editor.save(replace(original, token="different-access-token-" + "b" * 32))

    assert path.read_bytes() == before


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() != 0,
    reason="preserving another account's owner requires an existing root test context",
)
def test_root_edit_preserves_the_original_non_root_owner(existing_configuration):
    path, original = existing_configuration
    os.chown(path, 65534, 65534)
    editor = ConfigEditor(path)

    editor.save(replace(original, settings=replace(original.settings, port=8123)))

    assert (path.stat().st_uid, path.stat().st_gid) == (65534, 65534)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    editor.restore()
    assert (path.stat().st_uid, path.stat().st_gid) == (65534, 65534)
    assert load_config(path) == original


def test_edit_service_check_runs_before_any_configuration_publication(
    existing_configuration,
):
    path, _ = existing_configuration
    original = path.read_bytes()

    def check(config):
        assert config.settings.mode == "local"
        assert path.read_bytes() == original
        raise RuntimeError("fixture service is no longer stopped")

    with pytest.raises(RuntimeError, match="no longer stopped"):
        edit_config(
            path,
            ask=EditInputs(["", "", "local", ""], path),
            ask_secret=no_secret,
            before_save=check,
        )

    assert path.read_bytes() == original
