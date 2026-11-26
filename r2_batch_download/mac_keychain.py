"""Protect macOS connection secrets with a key stored in the login Keychain."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path


def _cipher(state_file: Path, create: bool):
    from cryptography.fernet import Fernet
    from keyring.backends.macOS import Keyring
    from keyring.errors import KeyringError
    from PySide6.QtCore import QLockFile

    account = sha256(str(state_file.resolve()).encode("utf-8")).hexdigest()
    service = "S3 File Manager local storage"
    keychain = Keyring()
    try:
        key = keychain.get_password(service, account)
        if key is None and create:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(state_file.parent / "keychain.lock"))
            if not lock.tryLock(3000):
                raise ValueError("钥匙串正在被另一个窗口使用。")
            try:
                key = keychain.get_password(service, account)
                if key is None:
                    key = Fernet.generate_key().decode("ascii")
                    keychain.set_password(service, account, key)
            finally:
                lock.unlock()
    except (KeyringError, RuntimeError) as exc:
        raise ValueError("请解锁 macOS 登录钥匙串并允许 S3 File Manager 访问。") from exc
    if key is None:
        raise ValueError("登录钥匙串里没有对应的密钥，请重新输入凭据。")
    return Fernet(key.encode("ascii"))


def protect_secret(state_file: Path, secret: str) -> str:
    return _cipher(state_file, create=True).encrypt(secret.encode("utf-8")).decode("ascii")


def unprotect_secret(state_file: Path, protected: str) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _cipher(state_file, create=False).decrypt(protected.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("保存的密钥无法解密。") from exc
