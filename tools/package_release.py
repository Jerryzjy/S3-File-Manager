"""Package the built desktop executable, documentation and component notices."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path, help="Versioned directory containing S3FileManager.exe")
    parser.add_argument("--github-repository", metavar="OWNER/REPO", help="Use an absolute GitHub Release asset URL in the update manifest")
    parser.add_argument("--github-tag", help="GitHub release tag (default: v<VERSION>)")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    release = args.release.resolve()
    version = release.name
    if not re.fullmatch(r"[0-9][0-9A-Za-z._+-]{0,63}", version):
        parser.error("release directory name must be the application version")
    if args.github_tag and not args.github_repository:
        parser.error("--github-tag requires --github-repository")
    if args.github_repository and not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*", args.github_repository):
        parser.error("--github-repository must have the form OWNER/REPO")
    tag = args.github_tag or f"v{version}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag):
        parser.error("GitHub release tag may contain letters, numbers, dots, hyphens and underscores")
    executable = release / "S3FileManager.exe"
    if not executable.is_file():
        raise SystemExit(f"Build {executable.name} before packaging the release")
    for name, target in (("README.md", "使用说明.md"), ("CHANGELOG.md", "更新记录.md"), ("THIRD_PARTY_NOTICES.md", "第三方组件说明.md"), ("LICENSE", "LICENSE")):
        shutil.copy2(project / name, release / target)
    notices = release / "licenses"
    notices.mkdir(parents=True, exist_ok=True)
    for name in ("boto3", "botocore", "s3transfer", "jmespath", "python-dateutil", "urllib3", "six", "PySide6-Essentials", "shiboken6", "qtawesome", "QtPy", "PyInstaller", "certifi", "cryptography", "packaging"):
        distribution = metadata.distribution(name)
        folder = notices / name
        folder.mkdir(exist_ok=True)
        (folder / "VERSION.txt").write_text(f"{name} {distribution.version}\n", encoding="utf-8")
        for entry in distribution.files or []:
            in_license_dir = "licenses" in entry.parts and any(part.endswith(".dist-info") for part in entry.parts)
            if not in_license_dir and not any(word in entry.name.lower() for word in ("license", "licence", "copying", "notice", "copyright")):
                continue
            source = Path(distribution.locate_file(entry))
            if source.is_file():
                relative = Path(*(part for part in entry.parts if part not in ("..", ".")))
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, notices / "Python-LICENSE.txt")
    shutil.copy2(project / "LICENSE", notices / "GPL-3.0.txt")
    shutil.copy2(project / "docs" / "licenses" / "LGPL-3.0.txt", notices / "LGPL-3.0.txt")
    if args.github_repository:
        (release / "SOURCE.txt").write_text(
            f"https://github.com/{args.github_repository}/tree/{tag}\n"
            f"https://github.com/{args.github_repository}/archive/refs/tags/{tag}.zip\n", encoding="ascii",
        )
    hasher = hashlib.sha256()
    with executable.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    digest = hasher.hexdigest()
    (release / "SHA256SUMS.txt").write_text(f"{digest}  {executable.name}\n", encoding="ascii")
    bundle_name = f"{executable.stem}-{release.name}"
    archive = release.parent / f"{bundle_name}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(release.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(bundle_name) / path.relative_to(release))

    update_dir = release.parent / "updates" / version
    update_dir.mkdir(parents=True, exist_ok=True)
    update_executable = update_dir / f"{bundle_name}-windows-x64.exe"
    shutil.copy2(executable, update_executable)
    download_url = update_executable.name
    if args.github_repository:
        download_url = f"https://github.com/{args.github_repository}/releases/download/{tag}/{download_url}"
    changelog = (project / "CHANGELOG.md").read_text(encoding="utf-8").split("\n## ")
    notes = next((section.partition("\n")[2].strip() for section in changelog[1:] if section.partition("\n")[0] == version), "")
    manifest = update_dir / "update-windows-x64.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "version": version,
        "url": download_url,
        "sha256": digest,
        "size": executable.stat().st_size,
        "notes": notes[:4000],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(archive)
    print(update_executable)
    print(manifest)


if __name__ == "__main__":
    main()
