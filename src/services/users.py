"""Небольшой JSON-файл для первого /start и выбранной дисциплины."""

import json
from pathlib import Path


class Users:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, user_id: int) -> dict:
        return self.data.get(str(user_id), {})

    def update(self, user_id: int, **values) -> None:
        self.data.setdefault(str(user_id), {}).update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
