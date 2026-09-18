"""Process log helpers for local pipeline runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class ProcessLog:
    path: Path
    lines: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        self.lines.append(f"[{timestamp}] {message}")

    def write(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return self.path
