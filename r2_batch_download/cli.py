from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import __version__


@dataclass(frozen=True)
class Settings:
    account_id: str | None
    access_key_id: str | None
    secret_access_key: str | None
    bucket: str | None
    endpoint_url: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        account_id = os.getenv("R2_ACCOUNT_ID") or os.getenv("CLOUDFLARE_ACCOUNT_ID")
        endpoint_url = os.getenv("R2_ENDPOINT_URL")
        if not endpoint_url and account_id:
            endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        return cls(
            account_id=account_id,
            access_key_id=os.getenv("R2_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY"),
            bucket=os.getenv("R2_BUCKET"),
            endpoint_url=endpoint_url,
        )

    def missing(self, require_bucket: bool = True) -> list[str]:
        required = {
            "R2_ACCESS_KEY_ID (or AWS_ACCESS_KEY_ID)": self.access_key_id,
            "R2_SECRET_ACCESS_KEY (or AWS_SECRET_ACCESS_KEY)": self.secret_access_key,
        }
        if not self.account_id and not self.endpoint_url:
            required["R2_ACCOUNT_ID (or R2_ENDPOINT_URL)"] = None
        if require_bucket:
            required["R2_BUCKET or --bucket"] = self.bucket
        return [name for name, value in required.items() if not value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="r2-batch-download",
        description="List and batch-download objects from a Cloudflare R2 bucket.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check local configuration and SDK availability")
    doctor.set_defaults(handler=run_doctor)

    list_cmd = subparsers.add_parser("list", help="list objects in a bucket")
    _add_bucket(list_cmd)
    list_cmd.add_argument("--prefix", default="", help="only include keys beginning with this prefix")
    list_cmd.add_argument("--limit", type=positive_int, help="maximum objects to list")
    list_cmd.set_defaults(handler=run_list)

    download = subparsers.add_parser("download", help="download objects in a bucket or prefix")
    _add_bucket(download)
    download.add_argument("--prefix", default="", help="download only keys beginning with this prefix")
    download.add_argument("--out", type=Path, default=Path("downloads"), help="destination directory (default: ./downloads)")
    download.add_argument("--workers", type=positive_int, default=4, help="parallel downloads (default: 4)")
    download.add_argument("--limit", type=positive_int, help="maximum objects to download")
    download.add_argument("--dry-run", action="store_true", help="show selected keys without downloading")
    download.add_argument("--overwrite", action="store_true", help="replace existing destination files")
    download.add_argument("--strip-prefix", action="store_true", help="remove --prefix from local paths")
    download.set_defaults(handler=run_download)
    return parser


def _add_bucket(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bucket", help="R2 bucket (defaults to R2_BUCKET)")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def make_client(settings: Settings) -> Any:
    missing = settings.missing(require_bucket=False)
    if missing or not settings.endpoint_url:
        missing = missing or ["R2_ENDPOINT_URL or R2_ACCOUNT_ID"]
        raise ValueError("Missing configuration: " + ", ".join(missing))
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("boto3 is not installed; run: python -m pip install -e .") from exc
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"}),
    )


def iter_objects(client: Any, bucket: str, prefix: str = "") -> Iterator[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            yield item


def safe_destination(root: Path, key: str) -> Path:
    """Map an object key below root, rejecting path traversal and absolute paths."""
    if not key or "\x00" in key:
        raise ValueError("object key is empty or contains a NUL byte")
    # S3 keys use forward slashes. Empty/dot segments and Windows-reserved
    # characters are encoded so valid object keys remain inside the output root.
    parts = key.split("/")
    parts = [
        {"": "%00", ".": "%2E", "..": "%2E%2E"}.get(part, _windows_safe_segment(part) if os.name == "nt" else part)
        for part in parts
    ]
    root_resolved = root.resolve()
    destination = root.joinpath(*parts)
    resolved = destination.resolve()
    if not comparison_path(resolved).is_relative_to(comparison_path(root_resolved)):
        raise ValueError("object key resolves outside the output directory")
    return destination


def comparison_path(path: Path) -> Path:
    """Normalize equivalent Win32 extended/UNC forms after resolving links."""
    if os.name != "nt":
        return path
    text = str(path).replace("/", "\\")
    if text.casefold().startswith("\\\\?\\unc\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text)


def _windows_safe_segment(segment: str) -> str:
    """Percent-encode characters and names that Windows cannot store safely."""
    encoded = ""
    for char in segment:
        if char == "%" or char in '<>:"/\\|?*' or ord(char) < 32:
            encoded += f"%{ord(char):02X}"
        else:
            encoded += char
    while encoded.endswith((".", " ")):
        char = encoded[-1]
        encoded = encoded[:-1] + ("%2E" if char == "." else "%20")
    device_stem = encoded.split(".", 1)[0].upper()
    if device_stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        encoded = "%" + f"{ord(encoded[0]):02X}" + encoded[1:]
    return encoded


def destination_identity(relative_parts: tuple[str, ...]) -> tuple[str, ...]:
    """Return path components with the target filesystem's comparison rules."""
    return tuple(part.casefold() for part in relative_parts) if os.name == "nt" else relative_parts


def path_conflicts(
    identity: tuple[str, ...],
    accepted_files: set[tuple[str, ...]],
    accepted_parents: set[tuple[str, ...]],
) -> bool:
    has_file_parent = any(identity[:index] in accepted_files for index in range(1, len(identity)))
    return identity in accepted_files or has_file_parent or identity in accepted_parents


def download_one(
    client: Any,
    bucket: str,
    item: dict[str, Any],
    output_root: Path,
    prefix: str,
    strip_prefix: bool,
    overwrite: bool,
    secret_values: tuple[str | None, ...] = (),
) -> dict[str, Any]:
    key = item["Key"]
    if key.endswith("/"):
        return {"key": key, "status": "skipped", "reason": "directory marker"}
    relative_key = key[len(prefix):] if strip_prefix and prefix else key
    if not relative_key:
        return {"key": key, "status": "skipped", "reason": "prefix has no relative file path"}
    try:
        destination = safe_destination(output_root, relative_key)
    except ValueError as exc:
        return {"key": key, "status": "failed", "error": _redact_text(str(exc), secret_values)}

    expected_size = item.get("Size")
    if destination.exists() and not overwrite:
        if destination.is_file() and (expected_size is None or destination.stat().st_size == expected_size):
            return {"key": key, "status": "skipped", "path": str(destination), "reason": "file already exists"}
        return {"key": key, "status": "failed", "error": f"destination exists: {destination} (use --overwrite to replace)"}

    temp_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".r2-download-", suffix=".part", dir=destination.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            client.download_fileobj(bucket, key, temp_file)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if expected_size is not None and temp_path.stat().st_size != expected_size:
            raise IOError(f"size mismatch (expected {expected_size}, got {temp_path.stat().st_size})")
        os.replace(temp_path, destination)
        return {"key": key, "status": "downloaded", "path": str(destination), "bytes": destination.stat().st_size}
    except Exception as exc:  # The caller records per-object failures and continues.
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"key": key, "status": "failed", "error": _redact_text(str(exc), secret_values)}


def run_doctor(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    try:
        import boto3  # noqa: F401
        sdk_available = True
    except ImportError:
        sdk_available = False
    missing = settings.missing(require_bucket=False)
    ready = sdk_available and not missing and bool(settings.endpoint_url)
    result = {
        "ok": ready,
        "version": __version__,
        "sdk_available": sdk_available,
        "network_checked": False,
        "endpoint": settings.endpoint_url,
        "configuration": {
            "account_id": bool(settings.account_id),
            "access_key_id": bool(settings.access_key_id),
            "secret_access_key": bool(settings.secret_access_key),
            "bucket": bool(settings.bucket),
        },
        "missing": missing + ([] if sdk_available else ["boto3 (python -m pip install -e .)"]),
    }
    _emit(args.json, result, f"Configuration {'ready' if ready else 'incomplete'}. Network/authentication was not tested.")
    if not args.json:
        for name in result["missing"]:
            print(f"Missing: {name}")
    return 0 if ready else 1


def _resolve_bucket(settings: Settings, override: str | None) -> str:
    bucket = override or settings.bucket
    if not bucket:
        raise ValueError("Specify --bucket or set R2_BUCKET")
    return bucket


def run_list(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    bucket = _resolve_bucket(settings, args.bucket)
    client = make_client(settings)
    objects = iter_objects(client, bucket, args.prefix)
    if args.limit:
        objects = islice(objects, args.limit)
    rows = [object_summary(item) for item in objects]
    _emit(args.json, {"bucket": bucket, "prefix": args.prefix, "count": len(rows), "objects": rows})
    if not args.json:
        for row in rows:
            print(f"{row['key']}\t{row['size']} bytes")
        print(f"{len(rows)} object(s)")
    return 0


def object_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {"key": item.get("Key"), "size": item.get("Size"), "last_modified": item.get("LastModified").isoformat() if item.get("LastModified") else None}


def run_download(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    bucket = _resolve_bucket(settings, args.bucket)
    client = make_client(settings)
    objects: Iterable[dict[str, Any]] = iter_objects(client, bucket, args.prefix)
    if args.limit:
        objects = islice(objects, args.limit)

    if args.dry_run:
        rows = [object_summary(item) for item in objects]
        _emit(args.json, {"bucket": bucket, "prefix": args.prefix, "dry_run": True, "count": len(rows), "objects": rows})
        if not args.json:
            for row in rows:
                print(f"{row['key']}\t{row['size']} bytes")
            print(f"Dry run: {len(rows)} object(s) selected")
        return 0

    output_root = args.out.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    pending: set[Future[dict[str, Any]]] = set()
    pending_keys: dict[Future[dict[str, Any]], str] = {}
    accepted_destinations: set[tuple[str, ...]] = set()
    accepted_parents: set[tuple[str, ...]] = set()
    max_pending = max(args.workers * 2, args.workers)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for item in objects:
            key = item["Key"]
            relative_key = key[len(args.prefix):] if args.strip_prefix and args.prefix else key
            if relative_key and not key.endswith("/"):
                try:
                    relative_parts = safe_destination(output_root, relative_key).relative_to(output_root).parts
                    identity = destination_identity(relative_parts)
                    if path_conflicts(identity, accepted_destinations, accepted_parents):
                        results.append({"key": key, "status": "failed", "error": "local path collides with another object key on this filesystem"})
                        if not args.json:
                            _print_progress(results[-1:])
                        continue
                    accepted_destinations.add(identity)
                    accepted_parents.update(identity[:index] for index in range(1, len(identity)))
                except ValueError as exc:
                    results.append({"key": key, "status": "failed", "error": str(exc)})
                    if not args.json:
                        _print_progress(results[-1:])
                    continue
            future = pool.submit(
                download_one,
                client,
                bucket,
                item,
                output_root,
                args.prefix,
                args.strip_prefix,
                args.overwrite,
                (settings.access_key_id, settings.secret_access_key),
            )
            pending.add(future)
            pending_keys[future] = key
            if len(pending) >= max_pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"key": pending_keys[future], "status": "failed", "error": _safe_error(exc, settings)})
                    pending_keys.pop(future, None)
                if not args.json:
                    _print_progress(results[-len(done):])
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"key": pending_keys[future], "status": "failed", "error": _safe_error(exc, settings)})
                pending_keys.pop(future, None)
            if not args.json:
                _print_progress(results[-len(done):])

    counts = {status: sum(1 for result in results if result["status"] == status) for status in ("downloaded", "skipped", "failed")}
    result = {"bucket": bucket, "prefix": args.prefix, "output": str(output_root), "total": len(results), **counts, "results": results}
    _emit(args.json, result, f"Downloaded {counts['downloaded']}; skipped {counts['skipped']}; failed {counts['failed']}.")
    return 1 if counts["failed"] else 0


def _print_progress(results: list[dict[str, Any]]) -> None:
    for result in results:
        if result["status"] == "failed":
            print(f"FAILED {result['key']}: {result.get('error', 'unknown error')}", file=sys.stderr)
        else:
            print(f"{result['status'].upper()} {result['key']}")


def _emit(json_mode: bool, value: dict[str, Any], human_message: str | None = None) -> None:
    if json_mode:
        print(json.dumps(value, ensure_ascii=False, default=str))
    elif human_message:
        print(human_message)


def _safe_error(exc: Exception, settings: Settings) -> str:
    return _redact_text(str(exc), (settings.secret_access_key, settings.access_key_id))


def _redact_text(message: str, secret_values: tuple[str | None, ...]) -> str:
    for secret in secret_values:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        exit_code = args.handler(args)
    except Exception as exc:
        message = _safe_error(exc, Settings.from_env())
        if args.json:
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        else:
            print(f"Error: {message}", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
