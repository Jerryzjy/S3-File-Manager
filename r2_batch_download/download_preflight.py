"""Check a pending download against the same paths used by the transfer engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import stat
import threading
from typing import Any

from .cli import comparison_path, destination_identity, path_conflicts
from .client_transfer import _destination
from .desktop_engine import Cancelled


@dataclass(frozen=True)
class DownloadPreflight:
    output: Path
    count: int
    total: int
    required: int
    free: int
    existing: int
    skipped: int
    different_size: int
    issues: int
    examples: tuple[str, ...]


def _directory(root: Path, checked: dict[Path, Path]) -> Path:
    """Find the existing ancestor on which an absent directory would be made."""
    missing = []
    path = root
    while path not in checked:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            if path == path.parent or path.is_symlink():
                raise ValueError("保存位置不可用，请选择可用的文件夹。") from None
            missing.append(path)
            path = path.parent
        else:
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("保存位置或其中的上级目录被文件占用。")
            checked[path] = path
    ancestor = checked[path]
    for child in missing:
        checked[child] = ancestor
    return ancestor


def inspect_download(
    entries: list[dict[str, Any]], output: str, prefix: str, strip_prefix: bool,
    skip_existing: bool, cancel: threading.Event,
) -> DownloadPreflight:
    if cancel.is_set():
        raise Cancelled("下载检查已取消。")
    if not output.strip():
        raise ValueError("请选择下载保存位置。")
    if not entries:
        raise ValueError("所选范围没有可下载的文件。")
    output_root = Path(output).expanduser().resolve()
    checked: dict[Path, Path] = {}
    free = shutil.disk_usage(_directory(output_root, checked)).free
    settings = {"output_dir": str(output_root), "prefix": prefix, "strip_prefix": strip_prefix}
    accepted_files: set[tuple[str, ...]] = set()
    accepted_parents: set[tuple[str, ...]] = set()
    total = required = existing = skipped = different_size = issues = 0
    examples: list[str] = []

    for entry in entries:
        if cancel.is_set():
            raise Cancelled("下载检查已取消。")
        total += entry["size"]
        try:
            _, _, destination, size = _destination({**settings, "key": entry["key"], "size": entry["size"]})
            identity = destination_identity(comparison_path(destination.resolve()).parts)
            if path_conflicts(identity, accepted_files, accepted_parents):
                raise ValueError("与所选的其他对象映射到同一个文件位置，或文件与目录重名。")
            accepted_files.add(identity)
            accepted_parents.update(identity[:index] for index in range(1, len(identity)))
            _directory(destination.parent, checked)
            try:
                metadata = destination.stat()
            except FileNotFoundError:
                if destination.is_symlink():
                    raise ValueError("目标位置被无效的链接占用。") from None
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("目标位置被目录或其他非普通文件占用。")
                existing += 1
                if skip_existing:
                    if metadata.st_size == size:
                        skipped += 1
                    else:
                        different_size += 1
                        if len(examples) < 3:
                            examples.append(f"{entry['key']}：已有同名文件，大小不同。")
                    continue
            required += size
        except (OSError, RuntimeError, ValueError) as exc:
            issues += 1
            if len(examples) < 3:
                examples.append(f"{entry['key']}：{exc}")

    return DownloadPreflight(output_root, len(entries), total, required, free, existing,
                             skipped, different_size, issues, tuple(examples))
