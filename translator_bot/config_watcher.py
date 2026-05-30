from __future__ import annotations

from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _ConfigChangeHandler(FileSystemEventHandler):
    def __init__(self, path: Path, callback: Callable[[], None], debounce_seconds: float) -> None:
        self.path = path.resolve()
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self._last_event_at = 0.0
        self._lock = Lock()

    def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
        if Path(event.src_path).resolve() != self.path:
            return
        with self._lock:
            now = monotonic()
            if now - self._last_event_at < self.debounce_seconds:
                return
            self._last_event_at = now
            self.callback()


class ConfigWatcher:
    def __init__(self, path: str | Path, callback: Callable[[], None], debounce_seconds: float = 1.0) -> None:
        self.path = Path(path).resolve()
        self.callback = callback
        self.debounce_seconds = debounce_seconds
        self.observer = Observer()

    def start(self) -> None:
        handler = _ConfigChangeHandler(self.path, self.callback, self.debounce_seconds)
        self.observer.schedule(handler, str(self.path.parent), recursive=False)
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5)
