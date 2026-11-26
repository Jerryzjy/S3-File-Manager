"""Find official updates and stage verified Windows executables."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from botocore.httpsession import get_cert_path
from packaging.version import InvalidVersion, Version


_REPOSITORY = "Jerryzjy/S3-File-Manager"
MAC_RELEASES_API = f"https://api.github.com/repos/{_REPOSITORY}/releases?per_page=30"
_RELEASE_ASSETS = f"https://github.com/{_REPOSITORY}/releases/download"
MAX_MANIFEST_BYTES = 64 * 1024
_MAX_RELEASES_BYTES = 2 * 1024 * 1024
MAX_UPDATE_BYTES = 1024 * 1024 * 1024
_READ_SIZE = 256 * 1024
_TIMEOUT = 20
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class UpdateError(RuntimeError):
    """An update could not be checked or staged."""


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    url: str
    sha256: str
    size: int
    notes: str = ""


@dataclass(frozen=True)
class MacUpdateRelease:
    version: str
    url: str
    size: int
    notes: str = ""


def validate_feed_url(url: str, *, allow_loopback_http: bool = False) -> str:
    """Accept an HTTPS URL. Plain HTTP is available for explicit loopback tests."""
    url = url.strip()
    if not url or len(url) > 4096 or "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
        raise UpdateError("请输入有效的 HTTPS 更新地址。")
    try:
        address = urlsplit(url)
        host = address.hostname
        _ = address.port
    except ValueError:
        raise UpdateError("请输入有效的 HTTPS 更新地址。") from None
    scheme_allowed = address.scheme == "https" or (
        allow_loopback_http and address.scheme == "http" and host in _LOOPBACK_HOSTS
    )
    if not scheme_allowed or not host or address.username is not None or address.password is not None or address.fragment:
        raise UpdateError("更新地址需要使用 HTTPS，且不能包含用户名、密码或片段。")
    return url


class _UpdateRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allow_loopback_http: bool) -> None:
        self.allow_loopback_http = allow_loopback_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = validate_feed_url(newurl, allow_loopback_http=self.allow_loopback_http)
        if urlsplit(req.full_url).scheme == "https" and urlsplit(newurl).scheme != "https":
            raise UpdateError("更新服务器尝试将安全连接重定向到 HTTP。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, allow_loopback_http: bool):
    safe_url = validate_feed_url(url, allow_loopback_http=allow_loopback_http)
    request = Request(safe_url, headers={"User-Agent": "S3FileManager-Updater", "Accept-Encoding": "identity"})
    redirects = _UpdateRedirectHandler(allow_loopback_http)
    if sys.platform == "darwin":
        # Packaged Python may have no system OpenSSL certificate directory.
        context = ssl.create_default_context()
        context.load_verify_locations(get_cert_path(True))
        opener = build_opener(redirects, HTTPSHandler(context=context))
    else:
        opener = build_opener(redirects)
    response = opener.open(request, timeout=_TIMEOUT)
    if response.getcode() != 200:
        response.close()
        raise UpdateError("更新服务器未返回完整内容。")
    return response


def _parse_version(value: object) -> Version:
    if not isinstance(value, str) or not re.fullmatch(r"[vV]?[0-9][0-9A-Za-z._+-]{0,63}", value):
        raise UpdateError("更新版本号无效。")
    try:
        return Version(value)
    except InvalidVersion:
        raise UpdateError("更新版本号无效。") from None


def _check_asset(sha: object, size: object) -> None:
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        raise UpdateError("更新清单缺少有效的 SHA-256 校验值。")
    if type(size) is not int or not 0 < size <= MAX_UPDATE_BYTES:
        raise UpdateError("更新清单中的安装包大小无效。")


def fetch_update(
    feed_url: str, current_version: str, *, allow_loopback_http: bool = False,
) -> UpdateRelease | None:
    """Return the described release when its version is newer than this build."""
    installed = _parse_version(current_version)
    try:
        with _open(feed_url, allow_loopback_http) as response:
            actual_url = response.geturl()
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except HTTPError as exc:
        raise UpdateError(f"更新服务器返回 HTTP {exc.code}。请稍后重试。") from None
    except (OSError, HTTPException, URLError, ValueError):
        raise UpdateError("无法连接更新服务器。请检查网络后重试。") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("更新清单超过大小限制。")

    try:
        manifest = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise UpdateError("无法读取更新清单。请稍后重试。") from None
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise UpdateError("更新清单格式不受支持。")
    version = manifest.get("version")
    newer = _parse_version(version)
    url = manifest.get("url")
    digest, size = manifest.get("sha256"), manifest.get("size")
    notes = manifest.get("notes", "")
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise UpdateError("更新清单缺少有效的安装包地址。")
    try:
        asset_url = validate_feed_url(urljoin(actual_url, url), allow_loopback_http=allow_loopback_http)
    except ValueError:
        raise UpdateError("更新清单缺少有效的安装包地址。") from None
    _check_asset(digest, size)
    if not isinstance(notes, str) or len(notes) > 12000:
        raise UpdateError("更新说明格式无效或内容过长。")
    if newer <= installed:
        return None
    return UpdateRelease(version=version, url=asset_url, sha256=digest.lower(), size=size, notes=notes)


def fetch_mac_update(current_version: str, architecture: str) -> MacUpdateRelease | None:
    """Find the newest published release with an installer for this Mac."""
    installed = _parse_version(current_version)
    if architecture not in {"arm64", "x86_64"}:
        raise UpdateError("当前 Mac 架构暂不支持应用内更新检查。")
    try:
        with _open(MAC_RELEASES_API, False) as response:
            raw = response.read(_MAX_RELEASES_BYTES + 1)
    except HTTPError as exc:
        raise UpdateError(f"更新服务器返回 HTTP {exc.code}。请稍后重试。") from None
    except (OSError, HTTPException, URLError, ValueError):
        raise UpdateError("无法连接更新服务器。请检查网络后重试。") from None
    if len(raw) > _MAX_RELEASES_BYTES:
        raise UpdateError("版本列表超过大小限制。请稍后重试。")
    try:
        releases = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise UpdateError("无法读取版本列表。请稍后重试。") from None
    if not isinstance(releases, list):
        raise UpdateError("版本列表格式不受支持。")

    latest = None
    newest = installed
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") is not False:
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith("v"):
            continue
        try:
            version = _parse_version(tag)
        except UpdateError:
            continue
        assets = release.get("assets")
        if version <= newest or not isinstance(assets, list):
            continue
        asset_name = f"S3FileManager-{tag[1:]}-macos-{architecture}.dmg"
        url = f"{_RELEASE_ASSETS}/{tag}/{asset_name}"
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") != asset_name or asset.get("state") != "uploaded":
                continue
            size = asset.get("size")
            if type(size) is int and size > 0:
                notes = release.get("body")
                latest = MacUpdateRelease(tag[1:], url, size, notes[:12000] if isinstance(notes, str) else "")
                newest = version
                break
    return latest


def download_update(
    release: UpdateRelease, stage_dir: Path, *,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    allow_loopback_http: bool = False,
) -> Path:
    """Download to an isolated file and return it only after size and hash match."""
    _check_asset(release.sha256, release.size)
    _check_cancel(cancel)
    partial: Path | None = None
    try:
        stage_dir.mkdir(parents=True, exist_ok=True)
        with _open(release.url, allow_loopback_http) as response:
            declared_size = response.headers.get("Content-Length")
            if declared_size is not None and (
                not re.fullmatch(r"[0-9]{1,12}", declared_size) or int(declared_size) != release.size
            ):
                raise UpdateError("服务器上的安装包大小与更新清单不符。")
            with tempfile.NamedTemporaryFile(
                dir=stage_dir, prefix="s3fm-update-", suffix=".part", delete=False,
            ) as stream:
                partial = Path(stream.name)
                received = 0
                digest = sha256()
                while True:
                    _check_cancel(cancel)
                    chunk = response.read(min(_READ_SIZE, release.size - received + 1))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release.size:
                        raise UpdateError("下载的安装包超过清单标注的大小。")
                    stream.write(chunk)
                    digest.update(chunk)
                    if on_progress is not None:
                        on_progress(received, release.size)
                if received != release.size:
                    raise UpdateError("安装包下载不完整，请重试。")
                if digest.hexdigest() != release.sha256.lower():
                    raise UpdateError("安装包校验失败，请重新检查更新。")
                stream.flush()
                os.fsync(stream.fileno())
        _check_cancel(cancel)
        verified = partial.with_suffix(".exe")
        partial.rename(verified)
        return verified
    except HTTPError as exc:
        raise UpdateError(f"下载更新时服务器返回 HTTP {exc.code}。") from None
    except (OSError, HTTPException, URLError, ValueError):
        raise UpdateError("下载更新失败。请检查网络或本机存储空间后重试。") from None
    finally:
        if partial is not None:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateError("已取消下载更新。")
