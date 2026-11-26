"""Save user-supplied HTTPS resources as local upload sources."""
from __future__ import annotations

import os
from pathlib import Path
import re
import ssl
from threading import Event
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from botocore.httpsession import get_cert_path

from .desktop_engine import _raise_if_cancelled

MAX_IMPORT_BYTES = 512 * 1024**2


def urls_from_text(text: str) -> tuple[str, ...]:
    urls = tuple(dict.fromkeys(text.split()))
    if not urls or len(urls) > 20:
        raise ValueError("请输入 1 至 20 个 HTTPS 网址。")
    for url in urls:
        validate_url(url)
    return urls


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (len(url) <= 8192 and parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.fragment and not any(character.isspace() or ord(character) < 32 or character == "\\" for character in url))
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("网址必须使用 HTTPS，且不能包含登录信息或片段。")


class HttpsRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def save_url_sources(urls: tuple[str, ...], directory: Path, cancel: Event, progress) -> tuple[Path, ...]:
    context = ssl.create_default_context()
    context.load_verify_locations(get_cert_path(True))
    opener = build_opener(HttpsRedirects(), HTTPSHandler(context=context))
    saved: list[Path] = []
    total = 0
    try:
        for index, url in enumerate(urls, 1):
            _raise_if_cancelled(cancel)
            validate_url(url)
            filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
            filename = re.sub(r'[^\w.() -]', "_", filename).strip(" .")[:120]
            filename = filename.rstrip(" .") or "download"
            stem, suffix = os.path.splitext(filename)
            if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", filename.split(".", 1)[0], re.I):
                stem = "_" + stem
                filename = stem + suffix
            progress(index, len(urls))
            request = Request(url, headers={"User-Agent": "S3FileManager", "Accept-Encoding": "identity"})
            with opener.open(request, timeout=20) as response:
                if response.status != 200:
                    raise ValueError(f"第 {index} 个网址未返回完整文件。")
                for collision in range(1000):
                    name = filename if collision == 0 else f"{stem}-{collision}{suffix}"
                    path = directory / name
                    try:
                        stream = path.open("xb")
                    except FileExistsError:
                        continue
                    break
                else:
                    raise ValueError("导入目录中有过多同名文件，请选择其他目录。")
                saved.append(path)
                with stream:
                    while chunk := response.read(256 * 1024):
                        _raise_if_cancelled(cancel)
                        total += len(chunk)
                        if total > MAX_IMPORT_BYTES:
                            raise ValueError("单批网址导入的总大小上限为 512 MiB。")
                        stream.write(chunk)
        _raise_if_cancelled(cancel)
        return tuple(saved)
    except Exception:
        for path in saved:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
