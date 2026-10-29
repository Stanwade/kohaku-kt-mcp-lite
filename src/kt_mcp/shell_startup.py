"""Build Linux commands with the service user's Bash startup environment."""

from pathlib import Path

# Automatic startup files are disabled so Bash's initial, terminal-only
# diagnostics can be discarded before real profile output is captured.
_BOOTSTRAP = r"""
exec 2>&1
builtin printf S >&__STATUS_FD__
exec {__kt_mcp_output}>&1
exec 1>&__STARTUP_FD__ 2>&1
exec __STARTUP_FD__>&-

builtin readonly __kt_mcp_workspace="$1"
builtin readonly __kt_mcp_kind="$2"
builtin shift 2
__kt_mcp_arguments=("$@")
builtin readonly -a __kt_mcp_arguments
builtin set --

builtin set +m
builtin set +H
builtin set +o history
builtin unset HISTFILE

if [[ -r /etc/profile ]]; then
    builtin source /etc/profile
fi
if [[ -r "$HOME/.bash_profile" ]]; then
    builtin source "$HOME/.bash_profile"
elif [[ -r "$HOME/.bash_login" ]]; then
    builtin source "$HOME/.bash_login"
elif [[ -r "$HOME/.profile" ]]; then
    builtin source "$HOME/.profile"
fi

builtin set +m
builtin set +H
builtin set +o history
builtin unset HISTFILE
builtin cd -- "$__kt_mcp_workspace" || exit

exec 1>&"$__kt_mcp_output" 2>&1
exec {__kt_mcp_output}>&-
builtin printf R >&__STATUS_FD__
exec __STATUS_FD__>&-

if [[ "$__kt_mcp_kind" == bash ]]; then
    ( builtin eval -- "${__kt_mcp_arguments[0]}" )
else
    exec "${__kt_mcp_arguments[@]}"
fi
"""


def command(
    bash: str,
    workspace: Path,
    kind: str,
    arguments: list[str],
    startup_fd: int,
    status_fd: int,
) -> list[str]:
    """Keep user input in argv; only owned descriptor numbers enter the script."""
    bootstrap = _BOOTSTRAP.replace("__STARTUP_FD__", str(startup_fd)).replace(
        "__STATUS_FD__", str(status_fd)
    )
    return [
        bash,
        "--login",
        "--noprofile",
        "--norc",
        "-i",
        "-c",
        bootstrap,
        "kt-mcp-shell",
        str(workspace),
        kind,
        *arguments,
    ]
