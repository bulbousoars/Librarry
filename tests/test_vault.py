from pathlib import Path

import pytest

from librarry.vault import SecretsVault, VaultError


@pytest.fixture
def vault_paths(tmp_path: Path):
    return tmp_path / "secrets.vault", tmp_path / "secrets.key"


def test_keyfile_roundtrip(vault_paths):
    vault_path, key_path = vault_paths
    vault = SecretsVault(vault_path)
    vault.init_keyfile(key_path)
    vault.set("nzbgeek_api_key", "abc123", key_file=key_path)
    assert vault.get("nzbgeek_api_key", key_file=key_path) == "abc123"
    assert vault.list_names(key_file=key_path) == ["nzbgeek_api_key"]


def test_password_roundtrip(vault_paths):
    vault_path, _ = vault_paths
    vault = SecretsVault(vault_path)
    vault.init_password("correct-horse")
    vault.set("sab_password", "hunter2", password="correct-horse")
    assert vault.get("sab_password", password="correct-horse") == "hunter2"


def test_wrong_password_fails(vault_paths):
    vault_path, _ = vault_paths
    vault = SecretsVault(vault_path)
    vault.init_password("right")
    with pytest.raises(VaultError):
        vault.get("x", password="wrong")


def test_delete_secret(vault_paths):
    vault_path, key_path = vault_paths
    vault = SecretsVault(vault_path)
    vault.init_keyfile(key_path)
    vault.set("a", "1", key_file=key_path)
    assert vault.delete("a", key_file=key_path)
    with pytest.raises(VaultError):
        vault.get("a", key_file=key_path)
