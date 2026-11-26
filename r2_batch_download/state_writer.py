"""Coalesced background state persistence without disk work on the UI thread."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import client_store


SaveFunction = Callable[[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]], None]


class StateWriter:
    """Write one state snapshot at a time, retaining only the latest pending one.

    ``submit`` only copies flat record dictionaries and schedules the write;
    JSON encoding, encryption, filesystem work and callbacks run on the single
    writer thread. Callbacks must enqueue their result before touching a GUI.
    The flat records have the same contract as :func:`client_store.save_state`.

    ``flush`` is intended for tests or non-UI shutdown paths. UI code should
    submit its final snapshot, call ``close(timeout=0)``, then poll ``is_idle``
    or ``is_closed`` instead of blocking its event loop.
    """

    def __init__(
        self,
        on_error: Callable[[str], None],
        on_saved: Callable[[int], None] | None = None,
        save_fn: SaveFunction | None = None,
    ) -> None:
        self._on_error = on_error
        self._on_saved = on_saved
        self._save = save_fn if save_fn is not None else client_store.save_state
        self._condition = threading.Condition()
        self._pending: tuple[int, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]] | None = None
        self._busy = False
        self._closing = False
        self._revision = 0
        self._saved_revision = 0
        self._completed_revision = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="r2-state-writer")
        self._thread.start()

    def submit(
        self,
        profiles: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        preferences: dict[str, Any],
    ) -> int:
        """Schedule an isolated flat snapshot, replacing an older pending write.

        Previously accepted snapshots are never mutated by later UI changes.
        Nested runtime fields such as an attached Connection are not copied:
        they are outside the persisted state contract and are ignored by the
        store. Only flat primitive record fields are transferred to the writer.
        """
        def flat_copy(record: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(record, dict):
                raise ValueError("客户端记录格式不正确，无法保存。")
            return {key: value for key, value in record.items() if value is None or isinstance(value, (str, int, float, bool))}

        if not isinstance(profiles, list) or not isinstance(tasks, list) or not isinstance(preferences, dict):
            raise ValueError("客户端数据格式不正确，无法保存。")
        # Copying is intentionally outside the writer lock. An active disk
        # operation cannot prevent the UI from submitting a new revision.
        snapshot_profiles = [flat_copy(profile) for profile in profiles]
        for profile, snapshot in zip(profiles, snapshot_profiles):
            history = profile.get("sync_history")
            if isinstance(history, list):
                snapshot["sync_history"] = [
                    record.copy() for record in history[-20:] if isinstance(record, list) and len(record) == 2
                    and all(isinstance(item, str) for item in record)
                ]
        snapshot_tasks = [flat_copy(task) for task in tasks]
        snapshot_preferences = flat_copy(preferences)
        with self._condition:
            if self._closing:
                raise RuntimeError("客户端正在退出，不能再保存新的状态。")
            self._revision += 1
            revision = self._revision
            self._pending = revision, snapshot_profiles, snapshot_tasks, snapshot_preferences
            self._condition.notify_all()
        return revision

    @property
    def is_idle(self) -> bool:
        """True when no disk write is active and no snapshot is pending."""
        with self._condition:
            return not self._busy and self._pending is None

    @property
    def is_closed(self) -> bool:
        return not self._thread.is_alive()

    @property
    def saved_revision(self) -> int:
        """Highest successfully saved revision (coalesced revisions may skip)."""
        with self._condition:
            return self._saved_revision

    @property
    def completed_revision(self) -> int:
        """Highest attempted revision, including writes that failed."""
        with self._condition:
            return self._completed_revision

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for revisions accepted before this call; false on timeout/error."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            target = self._revision
            while self._completed_revision < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return self._saved_revision >= target

    def close(self, timeout: float = 0, flush: bool = True) -> bool:
        """Stop accepting submissions and drain accepted state before exiting.

        A zero timeout only requests shutdown; it does not discard queued state.
        Repeated close calls are safe. ``flush=False`` explicitly discards a
        pending (not active) snapshot and is for teardown where data is unwanted.
        The return value says whether the writer thread has finished, including
        its callbacks. A failed save is reported through ``on_error`` and does
        not leave a worker running forever or prevent a later pending recovery.
        """
        with self._condition:
            self._closing = True
            if not flush:
                self._pending = None
            self._condition.notify_all()
        if threading.current_thread() is self._thread:
            return False
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive()

    @staticmethod
    def _safe_callback(callback: Callable[..., None] | None, *args: Any) -> None:
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                # A notification failure must not kill persistence or strand the
                # newest queued snapshot. The file operation remains authoritative.
                pass

    @staticmethod
    def _safe_error(exc: Exception, profiles: list[dict[str, Any]]) -> str:
        message = str(exc)
        for profile in profiles:
            for field in ("secret_access_key", "access_key_id"):
                secret = profile.get(field)
                if isinstance(secret, str) and secret:
                    message = message.replace(secret, "[已隐藏]")
                    if secret.strip():
                        message = message.replace(secret.strip(), "[已隐藏]")
        return message

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None:
                    if self._closing:
                        self._condition.notify_all()
                        return
                    self._condition.wait()
                revision, profiles, tasks, preferences = self._pending
                self._pending = None
                self._busy = True
            # Do not hold the condition during JSON/encryption/fsync/callbacks.
            succeeded = False
            try:
                self._save(profiles, tasks, preferences)
                succeeded = True
                self._safe_callback(self._on_saved, revision)
            except Exception as exc:
                self._safe_callback(self._on_error, self._safe_error(exc, profiles))
            finally:
                with self._condition:
                    if succeeded:
                        self._saved_revision = revision
                    self._completed_revision = revision
                    self._busy = False
                    self._condition.notify_all()
