from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

VAULT_VERSION = 1
KDF_NONE = "none"
KDF_PBKDF2 = "pbkdf2"
PBKDF2_ITERATIONS = 600_000


class VaultError(RuntimeError):
    pass


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _fernet_from_password(password: str, salt: bytes) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    return Fernet(key)


def _fernet_from_keyfile(key_file: Path) -> Fernet:
    raw = key_file.read_bytes().strip()
    if len(raw) == 44:  # url-safe base64 Fernet key
        return Fernet(raw)
    if len(raw) == 32:
        return Fernet(base64.urlsafe_b64encode(raw))
    raise VaultError(f"Invalid key file format: {key_file}")


def generate_keyfile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(Fernet.generate_key())
    _restrict_permissions(path)


class SecretsVault:
    """Encrypted local store for API keys and passwords."""

    def __init__(self, vault_path: Path):
        self.vault_path = vault_path

    def exists(self) -> bool:
        return self.vault_path.exists()

    def _load_envelope(self) -> dict[str, Any]:
        if not self.exists():
            return {"version": VAULT_VERSION, "kdf": KDF_NONE, "salt": "", "ciphertext": ""}
        try:
            return json.loads(self.vault_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise VaultError(f"Corrupt vault file: {self.vault_path}") from exc

    def _save_envelope(self, envelope: dict[str, Any]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        self.vault_path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        _restrict_permissions(self.vault_path)

    def _fernet(self, *, password: str | None, key_file: Path | None) -> Fernet:
        envelope = self._load_envelope()
        kdf = envelope.get("kdf", KDF_NONE)
        if kdf == KDF_PBKDF2:
            if not password:
                raise VaultError("Master password required to unlock vault")
            salt = base64.b64decode(envelope["salt"])
            return _fernet_from_password(password, salt)
        if key_file and key_file.exists():
            return _fernet_from_keyfile(key_file)
        if password:
            raise VaultError("Vault uses key file unlock; password not applicable")
        raise VaultError(
            f"Cannot unlock vault. Provide key file or set kdf=pbkdf2 with master password."
        )

    def _decrypt_data(
        self,
        *,
        password: str | None = None,
        key_file: Path | None = None,
    ) -> dict[str, str]:
        envelope = self._load_envelope()
        if not envelope.get("ciphertext"):
            return {}
        fernet = self._fernet(password=password, key_file=key_file)
        try:
            payload = fernet.decrypt(envelope["ciphertext"].encode("ascii"))
        except InvalidToken as exc:
            raise VaultError("Failed to decrypt vault — wrong password or key file") from exc
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise VaultError("Vault payload must be a JSON object")
        return {str(k): str(v) for k, v in data.items()}

    def _encrypt_data(
        self,
        data: dict[str, str],
        *,
        password: str | None = None,
        key_file: Path | None = None,
        kdf: str = KDF_NONE,
    ) -> None:
        if kdf == KDF_PBKDF2:
            if not password:
                raise VaultError("Master password required to create password-protected vault")
            salt = secrets.token_bytes(16)
            fernet = _fernet_from_password(password, salt)
            envelope = {
                "version": VAULT_VERSION,
                "kdf": KDF_PBKDF2,
                "salt": base64.b64encode(salt).decode("ascii"),
            }
        else:
            if not key_file or not key_file.exists():
                raise VaultError("Key file required for key-file vault mode")
            fernet = _fernet_from_keyfile(key_file)
            envelope = {"version": VAULT_VERSION, "kdf": KDF_NONE, "salt": ""}

        ciphertext = fernet.encrypt(
            json.dumps(data, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        envelope["ciphertext"] = ciphertext
        self._save_envelope(envelope)

    def init_keyfile(self, key_file: Path) -> None:
        if self.exists():
            raise VaultError(f"Vault already exists: {self.vault_path}")
        generate_keyfile(key_file)
        self._encrypt_data({}, key_file=key_file, kdf=KDF_NONE)

    def init_password(self, password: str) -> None:
        if self.exists():
            raise VaultError(f"Vault already exists: {self.vault_path}")
        self._encrypt_data({}, password=password, kdf=KDF_PBKDF2)

    def list_names(self, *, password: str | None = None, key_file: Path | None = None) -> list[str]:
        return sorted(self._decrypt_data(password=password, key_file=key_file))

    def get(
        self,
        name: str,
        *,
        password: str | None = None,
        key_file: Path | None = None,
    ) -> str:
        data = self._decrypt_data(password=password, key_file=key_file)
        if name not in data:
            raise VaultError(f"Secret not found: {name}")
        return data[name]

    def set(
        self,
        name: str,
        value: str,
        *,
        password: str | None = None,
        key_file: Path | None = None,
    ) -> None:
        data = self._decrypt_data(password=password, key_file=key_file) if self.exists() else {}
        data[name] = value
        envelope = self._load_envelope()
        kdf = envelope.get("kdf", KDF_NONE) if self.exists() else (
            KDF_PBKDF2 if password else KDF_NONE
        )
        self._encrypt_data(
            data,
            password=password if kdf == KDF_PBKDF2 else None,
            key_file=key_file if kdf == KDF_NONE else None,
            kdf=kdf,
        )

    def delete(
        self,
        name: str,
        *,
        password: str | None = None,
        key_file: Path | None = None,
    ) -> bool:
        data = self._decrypt_data(password=password, key_file=key_file)
        if name not in data:
            return False
        del data[name]
        envelope = self._load_envelope()
        kdf = envelope.get("kdf", KDF_NONE)
        self._encrypt_data(
            data,
            password=password if kdf == KDF_PBKDF2 else None,
            key_file=key_file if kdf == KDF_NONE else None,
            kdf=kdf,
        )
        return True

    def rotate_password(
        self,
        old_password: str,
        new_password: str,
    ) -> None:
        data = self._decrypt_data(password=old_password)
        self._encrypt_data(data, password=new_password, kdf=KDF_PBKDF2)
