"""Local example data for the desktop design preview.

Nothing in this module connects to R2, reads credentials, or changes real files.
The consuming preview must keep its example-data indicator visible. All dates
use Asia/Shanghai (UTC+08:00), anchored to 2026-11-25.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


PREVIEW_DATE = "2026-11-25"
PREVIEW_TIMEZONE = "Asia/Shanghai"


def _file(key: str, size: int, modified: str) -> dict[str, Any]:
    return {"key": key, "size": size, "modified": modified, "kind": "file"}


FILES: dict[str, list[dict[str, Any]]] = {
    "brand-assets": [
        _file("品牌视觉/品牌标志.png", 184_320, "2026-11-25T09:36:00+08:00"),
        _file("品牌视觉/品牌标志_反白.png", 214_016, "2026-11-25T09:35:00+08:00"),
        _file("品牌视觉/品牌主视觉.jpg", 3_041_280, "2026-11-24T16:42:00+08:00"),
        _file("品牌视觉/品牌使用规范.pdf", 4_404_019, "2026-11-24T15:20:00+08:00"),
        _file("品牌视觉/社交媒体封面.jpg", 1_887_436, "2026-11-24T11:08:00+08:00"),
        _file("品牌视觉/活动海报.png", 3_984_588, "2026-11-23T14:18:00+08:00"),
        _file("品牌视觉/产品画册.pdf", 12_582_912, "2026-11-22T10:26:00+08:00"),
        _file("品牌视觉/品牌素材包.zip", 89_128_960, "2026-11-22T10:15:00+08:00"),
        _file("产品图片/产品正面.jpg", 2_726_297, "2026-11-25T10:12:00+08:00"),
        _file("产品图片/产品侧面.jpg", 2_411_724, "2026-11-25T10:11:00+08:00"),
        _file("产品图片/产品背面.jpg", 2_202_009, "2026-11-25T10:10:00+08:00"),
        _file("产品图片/产品细节_01.jpg", 3_565_158, "2026-11-24T17:40:00+08:00"),
        _file("产品图片/产品细节_02.jpg", 3_145_728, "2026-11-24T17:38:00+08:00"),
        _file("产品图片/产品场景_01.jpg", 5_347_737, "2026-11-24T17:20:00+08:00"),
        _file("产品图片/产品场景_02.jpg", 4_718_592, "2026-11-24T17:18:00+08:00"),
        _file("产品图片/透明底主图.png", 4_089_446, "2026-11-23T13:24:00+08:00"),
        _file("视频素材/品牌宣传片.mp4", 247_463_936, "2026-11-25T09:24:00+08:00"),
        _file("视频素材/产品介绍.mp4", 192_937_984, "2026-11-24T18:32:00+08:00"),
        _file("视频素材/场景演示.mp4", 364_904_448, "2026-11-24T18:20:00+08:00"),
        _file("视频素材/开箱短片.mp4", 96_468_992, "2026-11-23T16:05:00+08:00"),
        _file("视频素材/社交短片.mp4", 67_108_864, "2026-11-23T15:42:00+08:00"),
        _file("项目说明.pdf", 1_153_433, "2026-11-25T09:08:00+08:00"),
        _file("拍摄素材清单.pdf", 1_258_291, "2026-11-24T10:32:00+08:00"),
        _file("素材归档.zip", 146_800_640, "2026-11-21T18:10:00+08:00"),
    ],
    "project-backups": [
        _file("2026-11/网站源码_1124.zip", 58_720_256, "2026-11-24T23:00:00+08:00"),
        _file("2026-11/设计源文件_1124.zip", 178_257_920, "2026-11-24T22:40:00+08:00"),
        _file("2026-11/数据库备份_1124.zip", 25_165_824, "2026-11-24T22:30:00+08:00"),
        _file("2026-10/网站源码_1031.zip", 53_477_376, "2026-10-31T23:00:00+08:00"),
        _file("2026-10/设计源文件_1031.zip", 165_675_008, "2026-10-31T22:40:00+08:00"),
        _file("备份说明.pdf", 943_718, "2026-11-20T10:15:00+08:00"),
    ],
}


def _size_label(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return "0 B"


BUCKETS: list[dict[str, Any]] = [
    {
        "id": "brand-assets",
        "name": "品牌素材库",
        "bucket": "brand-assets",
        "account_id": "",
        "file_count": len(FILES["brand-assets"]),
        "size_label": _size_label(sum(entry["size"] for entry in FILES["brand-assets"])),
        "status": "示例连接",
    },
    {
        "id": "project-backups",
        "name": "项目备份",
        "bucket": "project-backups",
        "account_id": "",
        "file_count": len(FILES["project-backups"]),
        "size_label": _size_label(sum(entry["size"] for entry in FILES["project-backups"])),
        "status": "示例连接",
    },
]


TASKS: list[dict[str, Any]] = [
    {
        "id": "preview-download-1",
        "name": "品牌宣传片.mp4",
        "key": "视频素材/品牌宣传片.mp4",
        "bucket": "brand-assets",
        "status": "downloading",
        "size": 247_463_936,
        "transferred": 143_529_082,
        "speed": 8_808_038,
        "error": "",
    },
    {
        "id": "preview-download-2",
        "name": "品牌素材包.zip",
        "key": "品牌视觉/品牌素材包.zip",
        "bucket": "brand-assets",
        "status": "queued",
        "size": 89_128_960,
        "transferred": 0,
        "speed": 0,
        "error": "",
    },
    {
        "id": "preview-download-3",
        "name": "品牌使用规范.pdf",
        "key": "品牌视觉/品牌使用规范.pdf",
        "bucket": "brand-assets",
        "status": "completed",
        "size": 4_404_019,
        "transferred": 4_404_019,
        "speed": 0,
        "error": "",
    },
    {
        "id": "preview-download-4",
        "name": "产品正面.jpg",
        "key": "产品图片/产品正面.jpg",
        "bucket": "brand-assets",
        "status": "completed",
        "size": 2_726_297,
        "transferred": 2_726_297,
        "speed": 0,
        "error": "",
    },
    {
        "id": "preview-download-5",
        "name": "设计源文件_1124.zip",
        "key": "2026-11/设计源文件_1124.zip",
        "bucket": "project-backups",
        "status": "failed",
        "size": 178_257_920,
        "transferred": 35_651_584,
        "speed": 0,
        "error": "示例：网络连接中断，可重试此任务。",
    },
    {
        "id": "preview-download-6",
        "name": "开箱短片.mp4",
        "key": "视频素材/开箱短片.mp4",
        "bucket": "brand-assets",
        "status": "paused",
        "size": 96_468_992,
        "transferred": 28_940_697,
        "speed": 0,
        "error": "",
    },
]


def immediate_children(
    entries: Iterable[Mapping[str, Any]], prefix: str = "",
) -> list[dict[str, Any]]:
    """Return one directory level, grouping all descendants into folder rows.

    ``prefix`` is a folder key, with or without its trailing slash. Folder keys
    in the result always end in ``/``. Their size is the sum of descendants and
    their modification time is the latest descendant's local ISO timestamp.
    File dictionaries are copied, never changed in the caller's data. Both
    folders and files are sorted case-insensitively by name, with folders first.
    """
    directory = prefix if not prefix or prefix.endswith("/") else prefix + "/"
    folders: dict[str, dict[str, Any]] = {}
    files: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for entry in entries:
        key = entry.get("key")
        if not isinstance(key, str) or key in seen or not key.startswith(directory):
            continue
        seen.add(key)
        relative = key[len(directory):]
        if not relative:
            continue
        name, separator, _remainder = relative.partition("/")
        if not name:
            continue
        size = max(0, int(entry.get("size", 0)))
        modified = str(entry.get("modified", ""))
        if separator:
            folder_key = directory + name + "/"
            folder = folders.setdefault(folder_key, {
                "key": folder_key, "name": name, "kind": "folder",
                "size": 0, "modified": "",
            })
            folder["size"] += size
            folder["modified"] = max(folder["modified"], modified)
        else:
            files[key] = {
                **entry, "key": key, "name": name, "kind": "file",
                "size": size, "modified": modified,
            }
    by_name = lambda item: (item["name"].casefold(), item["name"])
    return sorted(folders.values(), key=by_name) + sorted(files.values(), key=by_name)
