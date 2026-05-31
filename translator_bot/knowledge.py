from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from translator_bot.config import ReplySuggestionKnowledgeSettings


@dataclass(frozen=True)
class KnowledgeStatus:
    enabled: bool
    file_count: int
    char_count: int
    last_loaded_at: datetime | None
    paths: list[str]


class KnowledgeCache:
    def __init__(self, settings: ReplySuggestionKnowledgeSettings) -> None:
        self.settings = settings
        self.content = ""
        self.file_count = 0
        self.char_count = 0
        self.last_loaded_at: datetime | None = None

    async def reload(self) -> KnowledgeStatus:
        if not self.settings.enabled:
            self.content = ""
            self.file_count = 0
            self.char_count = 0
            self.last_loaded_at = datetime.now(UTC)
            return self.status()

        files = _collect_markdown_files(self.settings.paths)
        parts: list[str] = []
        char_count = 0
        loaded_count = 0
        for path in files[: self.settings.max_files]:
            remaining = self.settings.max_chars - char_count
            if remaining <= 0:
                break
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                continue
            text = text[:remaining]
            parts.append(f"# {path.name}\n\n{text.strip()}")
            char_count += len(text)
            loaded_count += 1

        self.content = "\n\n---\n\n".join(parts)
        self.file_count = loaded_count
        self.char_count = char_count
        self.last_loaded_at = datetime.now(UTC)
        return self.status()

    def status(self) -> KnowledgeStatus:
        return KnowledgeStatus(
            enabled=self.settings.enabled,
            file_count=self.file_count,
            char_count=self.char_count,
            last_loaded_at=self.last_loaded_at,
            paths=list(self.settings.paths),
        )


def _collect_markdown_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if path.is_file() and path.suffix.lower() == ".md":
            files.append(path)
        elif path.is_dir():
            files.extend(item for item in path.rglob("*.md") if item.is_file())
    return sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)
