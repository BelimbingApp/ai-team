import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Sequence


def _git_root() -> Path | None:
    """Return the installation root for the Git executable on PATH."""
    git = shutil.which("git")
    if git is None:
        return None

    return Path(git).resolve().parent.parent


def _git_tool_executable(tool: str) -> str | None:
    """Resolve a Git-for-Windows tool when it is not itself on PATH."""
    git_root = _git_root()
    if git_root is None:
        return None

    for directory in ("bin", "usr/bin"):
        for suffix in ("", ".exe"):
            candidate = git_root / directory / f"{tool}{suffix}"
            if candidate.is_file():
                return str(candidate)

    return None


def _bash_executable() -> str:
    """Resolve Bash for POSIX hosts and ordinary Git-for-Windows installs."""
    # On Windows, `bash` may resolve to the WSL launcher in System32 even
    # though Git-for-Windows is installed. Prefer the Git installation so the
    # shell and the companion coreutils come from the same environment.
    git_bash = _git_tool_executable("bash")
    if git_bash is not None:
        return git_bash

    bash = shutil.which("bash")
    if bash is not None:
        return bash

    raise FileNotFoundError("Bash is required to exercise the AI-team shell mechanisms")


def bash_path(path: Path) -> str:
    """Return a PATH entry that Bash can consume on POSIX and Windows."""
    resolved = str(path.resolve())
    drive, tail = os.path.splitdrive(resolved)

    if not drive:
        return Path(resolved).as_posix()

    return f"/{drive.rstrip(':').lower()}{tail.replace(os.sep, '/')}"


def run_with_bash_path(
    command: Sequence[str],
    *,
    stub_directory: Path,
    env: dict[str, str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a command under Bash with extensionless test shims first on PATH."""
    child_env = env.copy()
    child_env["AI_TEAM_TEST_STUB_PATH"] = bash_path(stub_directory)
    if child_env.get("AI_TEAM_TEST_ORIGIN_REPO"):
        # Repository-aware mechanisms intentionally resolve the raw origin URL
        # instead of asking gh for its ambient choice. Most hermetic tests use
        # a local bare remote (or no remote) and still need their unrelated
        # Git operations to reach the real executable. Intercept only the one
        # read performed by ai_team_origin_repo; everything else is delegated.
        git_stub = stub_directory / "git"
        real_git = shutil.which("git")
        if real_git is None:
            raise FileNotFoundError("Git is required to exercise the AI-team mechanisms")
        if not git_stub.exists():
            git_stub.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
if [ -n "${{AI_TEAM_TEST_ORIGIN_REPO:-}}" ] && [ "${{1:-}} ${{2:-}} ${{3:-}}" = "remote get-url origin" ]; then
  printf 'https://github.com/%s.git\n' "$AI_TEAM_TEST_ORIGIN_REPO"
  exit 0
fi
exec {shlex.quote(bash_path(Path(real_git)))} "$@"
""",
                encoding="utf-8",
                newline="\n",
            )
            git_stub.chmod(git_stub.stat().st_mode | stat.S_IXUSR)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")

    return subprocess.run(
        [
            _bash_executable(),
            "-c",
            'PATH="$AI_TEAM_TEST_STUB_PATH:$PATH"; export PATH; exec "$@"',
            "ai-team-test",
            *command,
        ],
        env=child_env,
        **kwargs,
    )
