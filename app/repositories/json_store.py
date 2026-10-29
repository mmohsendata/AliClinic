import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock


EMPTY_STORE = {
    "users": [],
    "branches": [],
    "patients": [],
    "tests": [],
    "orders": [],
    "samples": [],
    "results": [],
    "audit_logs": [],
}


class JsonStore:
    """Atomic, locked JSON persistence; business logic only sees repository methods."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _lock(self) -> FileLock:
        return FileLock(str(self.path) + ".lock", timeout=10)

    def read(self) -> dict[str, Any]:
        with self._lock():
            return self._read_unlocked()

    def update(self, operation):
        with self._lock():
            data = self._read_unlocked()
            result = operation(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                    json.dump(data, temp_file, ensure_ascii=False, indent=2)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return result

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {key: list(value) for key, value in EMPTY_STORE.items()}
        try:
            with self.path.open(encoding="utf-8") as source:
                data = json.load(source)
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Unable to read data store {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Data store root must be a JSON object.")
        for key, default in EMPTY_STORE.items():
            data.setdefault(key, list(default))
            if not isinstance(data[key], list):
                raise RuntimeError(f"Data store collection '{key}' must be an array.")
        return data
