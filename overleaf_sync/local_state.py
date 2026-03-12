"""Local workspace metadata for Overleaf Sync."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import click


STAGE_FILE_NAME = ".ovs-stage.json"
BASE_SNAPSHOT_DIR = ".ovs-base"
CONFLICT_STATE_FILE = ".ovs-conflicts.json"
CONFLICT_SNAPSHOT_DIR = ".ovs-conflicts"


def stage_file_path(root: Path) -> Path:
    return root / STAGE_FILE_NAME


def conflict_state_path(root: Path) -> Path:
    return root / CONFLICT_STATE_FILE


def conflict_snapshot_root(root: Path) -> Path:
    return root / CONFLICT_SNAPSHOT_DIR


def conflict_snapshot_path(root: Path, side: str, rel_path: str) -> Path:
    return conflict_snapshot_root(root) / side / Path(rel_path)


def base_snapshot_root(root: Path) -> Path:
    return root / BASE_SNAPSHOT_DIR


def base_snapshot_path(root: Path, rel_path: str) -> Path:
    return base_snapshot_root(root) / Path(rel_path)


def read_base_snapshot_map(root: Path) -> dict[str, bytes]:
    snapshot_root = base_snapshot_root(root)
    if not snapshot_root.exists():
        return {}
    result: dict[str, bytes] = {}
    for file_path in snapshot_root.rglob("*"):
        if file_path.is_file():
            result[file_path.relative_to(snapshot_root).as_posix()] = file_path.read_bytes()
    return result


def write_base_snapshot(root: Path, rel_path: str, content: bytes) -> None:
    path = base_snapshot_path(root, rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def remove_base_snapshot(root: Path, rel_path: str) -> None:
    path = base_snapshot_path(root, rel_path)
    if path.exists():
        path.unlink()
    parent = path.parent
    snapshot_root = base_snapshot_root(root)
    while parent != snapshot_root and parent.exists():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def replace_base_snapshot(root: Path, file_map: dict[str, bytes]) -> None:
    snapshot_root = base_snapshot_root(root)
    if snapshot_root.exists():
        for existing in sorted(snapshot_root.rglob("*"), reverse=True):
            if existing.is_file():
                existing.unlink()
            else:
                try:
                    existing.rmdir()
                except OSError:
                    pass
    if not file_map:
        return
    for rel_path, content in file_map.items():
        write_base_snapshot(root, rel_path, content)


def update_base_snapshot_from_local_paths(root: Path, sync_root: Path, rel_paths: set[str]) -> None:
    for rel_path in rel_paths:
        local_path = sync_root / rel_path
        if local_path.is_file():
            write_base_snapshot(root, rel_path, local_path.read_bytes())
        else:
            remove_base_snapshot(root, rel_path)


def load_conflict_entries(root: Path) -> dict[str, dict[str, bool]]:
    path = conflict_state_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Failed to read conflict state at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise click.ClickException(f"Invalid conflict state at {path}.")
    entries: dict[str, dict[str, bool]] = {}
    for rel_path, payload in data.items():
        if not isinstance(payload, dict):
            raise click.ClickException(f"Invalid conflict entry for {rel_path} in {path}.")
        entries[str(rel_path)] = {
            "ours_present": bool(payload.get("ours_present")),
            "theirs_present": bool(payload.get("theirs_present")),
        }
    return entries


def save_conflict_entries(root: Path, entries: dict[str, dict[str, bool]]) -> None:
    path = conflict_state_path(root)
    if not entries:
        if path.exists():
            path.unlink()
        snapshot_root = conflict_snapshot_root(root)
        if snapshot_root.exists():
            for existing in sorted(snapshot_root.rglob("*"), reverse=True):
                if existing.is_file():
                    existing.unlink()
                else:
                    try:
                        existing.rmdir()
                    except OSError:
                        pass
        return
    path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_conflict_snapshot(root: Path, side: str, rel_path: str, content: bytes | None) -> None:
    path = conflict_snapshot_path(root, side, rel_path)
    if content is None:
        if path.exists():
            path.unlink()
        parent = path.parent
        snapshot_root = conflict_snapshot_root(root)
        while parent != snapshot_root and parent.exists():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def read_conflict_snapshot(root: Path, side: str, rel_path: str) -> bytes | None:
    path = conflict_snapshot_path(root, side, rel_path)
    if not path.is_file():
        return None
    return path.read_bytes()


def set_conflict_entry(root: Path, rel_path: str, ours: bytes | None, theirs: bytes | None) -> None:
    entries = load_conflict_entries(root)
    entries[rel_path] = {
        "ours_present": ours is not None,
        "theirs_present": theirs is not None,
    }
    write_conflict_snapshot(root, "ours", rel_path, ours)
    write_conflict_snapshot(root, "theirs", rel_path, theirs)
    save_conflict_entries(root, entries)


def clear_conflict_entry(root: Path, rel_path: str) -> None:
    entries = load_conflict_entries(root)
    if rel_path not in entries:
        return
    entries.pop(rel_path, None)
    write_conflict_snapshot(root, "ours", rel_path, None)
    write_conflict_snapshot(root, "theirs", rel_path, None)
    save_conflict_entries(root, entries)


def print_conflict_entries(entries: dict[str, dict[str, bool]]) -> None:
    if not entries:
        return
    click.echo("Conflicts:")
    for rel_path in sorted(entries):
        click.echo(f"  {rel_path}")
    click.echo("")


def require_no_unresolved_conflicts(root: Path) -> None:
    entries = load_conflict_entries(root)
    if entries:
        raise click.ClickException(
            "Unresolved Overleaf conflicts exist. Run `ovs resolve` before pushing or pulling again."
        )


def load_stage_entries(root: Path) -> dict[str, dict[str, str | None]]:
    path = stage_file_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Failed to read stage file at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise click.ClickException(f"Invalid stage file at {path}.")
    entries: dict[str, dict[str, str | None]] = {}
    for rel_path, payload in data.items():
        if not isinstance(payload, dict):
            raise click.ClickException(f"Invalid staged entry for {rel_path} in {path}.")
        entries[str(rel_path)] = {
            "local_hash": payload.get("local_hash"),
            "remote_hash": payload.get("remote_hash"),
        }
    return entries


def save_stage_entries(root: Path, entries: dict[str, dict[str, str | None]]) -> None:
    path = stage_file_path(root)
    if not entries:
        if path.exists():
            path.unlink()
        return
    path.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def print_staged_entries(entries: dict[str, dict[str, str | None]]) -> None:
    if not entries:
        return
    click.echo("Staged:")
    for rel_path in sorted(entries):
        click.echo(f"  {rel_path}")
    click.echo("")
