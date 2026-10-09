from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Optional

from .models import AppConfig


def data_directory() -> Path:
    override = os.environ.get("VACUUM_INTERLOCK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "VacuumInterlock"
    return Path.home() / ".local" / "share" / "vacuum-interlock"


class ConfigurationStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "config.json"
        self._lock = RLock()
        self._config = AppConfig()
        self._password_hash: Optional[str] = None
        self._version = 1
        self._load()

    def _load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._config = AppConfig.model_validate(data.get("config", {}))
            self._password_hash = data.get("password_hash")
            self._version = max(1, int(data.get("version", 1)))
        except (ValueError, OSError, TypeError):
            # A broken configuration must not enable hardware control.
            self._config = AppConfig(mode="real")
            self._password_hash = None

    def save(self) -> None:
        with self._lock:
            data = {
                "config": self._config.model_dump(mode="json"),
                "password_hash": self._password_hash,
                "version": self._version,
            }
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            temp.replace(self.path)

    def get_config(self) -> AppConfig:
        with self._lock:
            return AppConfig.model_validate(self._config.model_dump())

    @property
    def version(self) -> int:
        return self._version

    @property
    def password_hash(self) -> Optional[str]:
        return self._password_hash

    def set_password_hash(self, value: str) -> None:
        self._password_hash = value
        self.save()

    def update(self, config: AppConfig) -> int:
        with self._lock:
            self._config = config
            self._version += 1
            self.save()
            return self._version
