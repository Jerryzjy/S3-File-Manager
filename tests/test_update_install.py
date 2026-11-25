from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from r2_batch_download import update_install


def _windows_image(payload: bytes) -> bytes:
    dos = b"MZ" + bytes(58) + struct.pack("<I", 64)
    pe = b"PE\0\0\x64\x86" + bytes(18) + b"\x0b\x02"
    return dos + pe + payload


class UpdateInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.target = Path(folder).resolve() / "Manager.exe"
        self.workspace = self.target.parent / ".s3fm-update-0123abcd"
        self.workspace.mkdir()
        self.helper = self.workspace / "update-helper.exe"
        self.previous_bytes = _windows_image(b"previous")
        self.next_bytes = _windows_image(b"next")
        self.target.write_bytes(self.previous_bytes)
        self.helper.write_bytes(self.previous_bytes)
        self.staged = self.workspace / "downloaded.exe"
        self.staged.write_bytes(self.next_bytes)
        self.digest = hashlib.sha256(self.next_bytes).hexdigest()

    def test_only_valid_helper_can_target_a_sibling_executable(self) -> None:
        self.assertEqual(update_install._target_for_helper(self.helper, self.target.name), self.target)
        for unsupported_name in ("../Manager.exe", "C:Manager.exe", "Manager.py"):
            with self.subTest(unsupported_name=unsupported_name), self.assertRaises(update_install.UpdateInstallError):
                update_install._target_for_helper(self.helper, unsupported_name)

    def test_invalid_digest_or_non_windows_payload_cannot_replace_the_program(self) -> None:
        with self.assertRaises(update_install.UpdateInstallError):
            update_install._install_and_restart(self.workspace, self.target, "a" * 64)
        invalid = b"This is not a Windows executable."
        self.staged.write_bytes(invalid)
        with self.assertRaises(update_install.UpdateInstallError):
            update_install._install_and_restart(self.workspace, self.target, hashlib.sha256(invalid).hexdigest())
        self.assertEqual(self.target.read_bytes(), self.previous_bytes)

    def test_failed_relaunch_restores_the_installed_file(self) -> None:
        with patch.object(update_install, "_start", side_effect=OSError("relaunch failed")):
            with self.assertRaises(OSError):
                update_install._install_and_restart(self.workspace, self.target, self.digest)
        self.assertEqual(self.target.read_bytes(), self.previous_bytes)

    def test_early_exit_restores_the_previous_file(self) -> None:
        with patch.object(update_install, "_start") as start:
            start.return_value.poll.return_value = 1
            with self.assertRaises(update_install.UpdateInstallError):
                update_install._install_and_restart(self.workspace, self.target, self.digest)
        self.assertEqual(self.target.read_bytes(), self.previous_bytes)

    def test_successful_restart_preserves_backup_until_startup_is_confirmed(self) -> None:
        def start(executable: Path, *arguments: str) -> None:
            self.assertEqual(executable.read_bytes(), self.next_bytes)
            self.assertEqual((self.workspace / "previous.exe").read_bytes(), self.previous_bytes)
            self.assertEqual(arguments, (update_install.CLEANUP_ARGUMENT, self.workspace.name))
            (self.workspace / "startup.ok").touch()

        with patch.object(update_install, "_start", side_effect=start):
            update_install._install_and_restart(self.workspace, self.target, self.digest)
        self.assertTrue((self.workspace / "install.ok").exists())
        self.assertEqual(self.target.read_bytes(), self.next_bytes)

    @unittest.skipUnless(os.name == "nt", "Windows process identity")
    def test_windows_process_identity_and_wait_use_the_actual_image(self) -> None:
        python = Path(sys.executable).resolve()
        processes = update_install._WindowsProcesses()
        with processes.process(os.getpid()) as handle:
            self.assertIsNotNone(handle)
            timestamp, image = processes.identity(handle)
            self.assertEqual(image, python)
            # A recycled PID should be ignored instead of waiting for the wrong process.
        update_install._wait_for_exit(python, [f"{os.getpid()}:{timestamp - 1}"])
