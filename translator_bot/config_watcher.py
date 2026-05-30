from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _ConfigChangeHandler(FileSystemEventHandler):
    def __init__(self, path: Path, callback: Callable[[], None]) -> None:
        self.path = path.resolve()
        self.callback = callback
        self._lock = Lock()

    def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
        if Path(event.src_path).resolve() != self.path:
            return
        with self._lock:
            self.callback()


class ConfigWatcher:
    def __init__(self, path: str | Path, callback: Callable[[], None]) -> None:
        self.path = Path(path).resolve()
        self.callback = callback
        self.observer = Observer()

    def start(self) -> None:
        handler = _ConfigChangeHandler(self.path, self.callback)
        self.observer.schedule(handler, str(self.path.parent), recursive=False)
        self.observer.start()

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5)
