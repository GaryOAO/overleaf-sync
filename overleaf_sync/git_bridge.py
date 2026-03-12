"""Git/repository binding helpers for Overleaf Sync."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import click


BRIDGE_CONFIG_NAME = ".overleaf-sync.json"
BRIDGE_CONFIG_VERSION = 1
DEFAULT_GIT_REMOTE = "origin"


@dataclass(frozen=True)
class BridgeConfig:
    version: int
    project_name: str
    store_path: str
    sync_path: str
    olignore: str
    git_remote: str = ""
    default_branch: str = ""


@dataclass(frozen=True)
class GitStatusSummary:
    repo_root: Path
    git_remote: str
    remote_url: str
    current_branch: str
    default_branch: str
    is_clean: bool
    ahead: int
    behind: int


def run_git_command(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise click.ClickException("Git is required for repo workflows and text merges, but `git` was not found.") from exc
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed."
        raise click.ClickException(message)
    return result


def find_repo_root(start_path: Path | None = None) -> Path:
    cwd = (start_path or Path.cwd()).resolve()
    try:
        result = run_git_command(["rev-parse", "--show-toplevel"], cwd=cwd)
    except click.ClickException as exc:
        raise click.ClickException("Not inside a Git repository.") from exc
    return Path(result.stdout.strip()).resolve()


def bridge_config_path(repo_root: Path) -> Path:
    return repo_root / BRIDGE_CONFIG_NAME


def find_bound_root(start_path: Path | None = None, *, required: bool = True) -> Path | None:
    current = (start_path or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if bridge_config_path(candidate).is_file():
            return candidate
    if required:
        raise click.ClickException(
            f"No Overleaf binding found from {current}. Run `ovs bind --name \"Your Project\"` first."
        )
    return None


def status_entry_path(entry: str) -> str:
    path = entry[3:].strip() if len(entry) > 3 else ""
    if " -> " in path:
        return path.split(" -> ", 1)[1].strip()
    return path


def is_ignored_untracked_path(path: str, ignored: set[str]) -> bool:
    return any(path == item or path.startswith(f"{item}/") for item in ignored)


def has_meaningful_git_changes(
    entries: list[str],
    ignored_untracked_paths: set[str] | None = None,
    metadata_paths: set[str] | None = None,
) -> bool:
    ignored = set(metadata_paths or set()) | set(ignored_untracked_paths or set())
    for entry in entries:
        code = entry[:2]
        path = status_entry_path(entry)
        if code == "??" and is_ignored_untracked_path(path, ignored):
            continue
        return True
    return False


def parse_git_status_porcelain(output: str) -> dict[str, object]:
    lines = output.splitlines()
    header = lines[0] if lines else ""
    entries = lines[1:]
    branch = ""
    upstream = ""
    ahead = 0
    behind = 0

    if header.startswith("## "):
        branch_info = header[3:]
        branch_text, _, divergence = branch_info.partition(" [")
        branch_name, _, upstream_name = branch_text.partition("...")
        branch = branch_name.strip()
        upstream = upstream_name.strip()

        if divergence:
            divergence = divergence.rstrip("]")
            for item in divergence.split(","):
                item = item.strip()
                if item.startswith("ahead "):
                    ahead = int(item.split(" ", 1)[1])
                elif item.startswith("behind "):
                    behind = int(item.split(" ", 1)[1])

    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "entries": entries,
        "is_clean": not has_meaningful_git_changes(entries),
    }


def normalize_bridge_path(value: str, field_name: str) -> str:
    path = Path(value)
    if path.is_absolute():
        raise click.ClickException(f"{field_name} must be relative to the repository root.")
    return path.as_posix() or "."


def normalize_store_config_path(value: str) -> str:
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    return path.as_posix() or "."


def resolve_repo_path(repo_root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def display_store_config_path(repo_root: Path, store_path: Path) -> str:
    try:
        return store_path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(store_path.resolve())


def require_repo_binding(config: BridgeConfig) -> None:
    if config.git_remote and config.default_branch:
        return
    raise click.ClickException(
        "Current binding does not include GitHub settings. Run `ovs repo init` first."
    )


def load_bridge_config(repo_root: Path) -> BridgeConfig:
    config_path = bridge_config_path(repo_root)
    if not config_path.is_file():
        raise click.ClickException(f"Bridge config not found at {config_path}. Run `ovs repo init` first.")

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Failed to read bridge config at {config_path}: {exc}") from exc

    required_fields = {
        "version",
        "project_name",
        "store_path",
        "sync_path",
        "olignore",
    }
    missing_fields = sorted(required_fields - set(data))
    if missing_fields:
        raise click.ClickException(f"Bridge config is missing required field(s): {', '.join(missing_fields)}")
    if data["version"] != BRIDGE_CONFIG_VERSION:
        raise click.ClickException(
            f"Unsupported bridge config version {data['version']}. Expected {BRIDGE_CONFIG_VERSION}."
        )

    return BridgeConfig(
        version=int(data["version"]),
        project_name=str(data["project_name"]),
        store_path=str(data["store_path"]),
        sync_path=str(data["sync_path"]),
        olignore=str(data["olignore"]),
        git_remote=str(data.get("git_remote", "")),
        default_branch=str(data.get("default_branch", "")),
    )


def write_bridge_config(repo_root: Path, config: BridgeConfig) -> Path:
    config_path = bridge_config_path(repo_root)
    config_path.write_text(json.dumps(asdict(config), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def git_remote_url(repo_root: Path, git_remote: str) -> str:
    result = run_git_command(["remote", "get-url", git_remote], cwd=repo_root)
    return result.stdout.strip()


def detect_default_branch(repo_root: Path, git_remote: str) -> str:
    symbolic_ref = run_git_command(
        ["symbolic-ref", f"refs/remotes/{git_remote}/HEAD"],
        cwd=repo_root,
        check=False,
    )
    if symbolic_ref.returncode == 0:
        return symbolic_ref.stdout.strip().rsplit("/", 1)[-1]

    current_branch = run_git_command(["branch", "--show-current"], cwd=repo_root).stdout.strip()
    if current_branch in {"main", "master"}:
        return current_branch

    main_ref = run_git_command(
        ["rev-list", "--left-right", "--count", f"{git_remote}/main...HEAD"],
        cwd=repo_root,
        check=False,
    )
    if main_ref.returncode == 0:
        return "main"

    master_ref = run_git_command(
        ["rev-list", "--left-right", "--count", f"{git_remote}/master...HEAD"],
        cwd=repo_root,
        check=False,
    )
    if master_ref.returncode == 0:
        return "master"

    return "main"


def collect_git_status(
    repo_root: Path,
    git_remote: str,
    default_branch: str,
    *,
    ignored_untracked_paths: set[str] | None = None,
    metadata_paths: set[str] | None = None,
) -> GitStatusSummary:
    remote_url = git_remote_url(repo_root, git_remote)
    porcelain = parse_git_status_porcelain(
        run_git_command(["status", "--porcelain=v1", "--branch"], cwd=repo_root).stdout
    )
    current_branch = run_git_command(["branch", "--show-current"], cwd=repo_root).stdout.strip()
    if not current_branch:
        current_branch = str(porcelain["branch"] or "HEAD")

    ahead = int(porcelain["ahead"])
    behind = int(porcelain["behind"])
    rev_list = run_git_command(
        ["rev-list", "--left-right", "--count", f"{git_remote}/{current_branch}...HEAD"],
        cwd=repo_root,
        check=False,
    )
    if rev_list.returncode == 0:
        behind_str, ahead_str = rev_list.stdout.strip().split()
        behind = int(behind_str)
        ahead = int(ahead_str)

    return GitStatusSummary(
        repo_root=repo_root,
        git_remote=git_remote,
        remote_url=remote_url,
        current_branch=current_branch,
        default_branch=default_branch,
        is_clean=not has_meaningful_git_changes(
            list(porcelain["entries"]),
            ignored_untracked_paths,
            metadata_paths,
        ),
        ahead=ahead,
        behind=behind,
    )


def require_default_branch(git_status: GitStatusSummary) -> None:
    if git_status.current_branch != git_status.default_branch:
        raise click.ClickException(
            f"Bridge commands only operate on the default branch '{git_status.default_branch}', "
            f"but the current branch is '{git_status.current_branch}'."
        )


def require_clean_worktree(git_status: GitStatusSummary) -> None:
    if not git_status.is_clean:
        raise click.ClickException("Working tree must be clean for this bridge command.")
