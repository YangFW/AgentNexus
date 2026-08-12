from __future__ import annotations

import base64
import hashlib
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.db import DATA_DIR

_LOCK = threading.Lock()


class SecretStore:
    """Small local secret store for single-node deployments.

    Production deployments should set APP_SECRET_KEY from a secret manager.
    """

    def __init__(self) -> None:
        self.key_file = Path(os.getenv("APP_SECRET_KEY_FILE", str(DATA_DIR / ".secret_key")))

    def _key(self) -> bytes:
        configured = os.getenv("APP_SECRET_KEY", "").strip()
        if configured:
            raw = configured.encode("utf-8")
            try:
                Fernet(raw)
                return raw
            except ValueError:
                return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        with _LOCK:
            if self.key_file.exists():
                return self.key_file.read_bytes().strip()
            self.key_file.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            self.key_file.write_bytes(key)
            try:
                self.key_file.chmod(0o600)
            except OSError:
                pass
            return key

    def encrypt(self, value: str) -> str:
        return Fernet(self._key()).encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return Fernet(self._key()).decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("模型 API Key 无法解密，请检查 APP_SECRET_KEY 是否发生变化") from exc


secret_store = SecretStore()
