from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from typing import TypeVar


ResultT = TypeVar("ResultT")


class BackgroundJobRunner:
    """Runs one blocking operation at a time and returns on Tk's event loop."""

    def __init__(
        self,
        root: tk.Misc,
        *,
        poll_interval_ms: int = 100,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self._root = root
        self._poll_interval_ms = poll_interval_ms
        self._thread_factory = thread_factory
        self._deliveries: queue.Queue[Callable[[], None]] = queue.Queue()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def start(
        self,
        operation: Callable[[], ResultT],
        on_finished: Callable[[ResultT | None, Exception | None], None],
        *,
        thread_name: str,
    ) -> None:
        if self._busy:
            raise RuntimeError("Background job is already running")

        self._busy = True

        def worker() -> None:
            try:
                value = operation()
            except Exception as exc:  # delivered to the Tk thread
                def deliver(error: Exception = exc) -> None:
                    on_finished(None, error)
            else:
                def deliver(result: ResultT = value) -> None:
                    on_finished(result, None)
            self._deliveries.put(deliver)

        self._thread_factory(target=worker, name=thread_name, daemon=False).start()
        self._root.after(self._poll_interval_ms, self._poll)

    def _poll(self) -> None:
        try:
            deliver = self._deliveries.get_nowait()
        except queue.Empty:
            self._root.after(self._poll_interval_ms, self._poll)
            return

        self._busy = False
        deliver()
