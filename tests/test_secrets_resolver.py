from librarry.secrets_resolver import SecretsResolver
from librarry.vault import SecretsVault


def test_resolver_secret_ref(tmp_path):
    vault_path = tmp_path / "secrets.vault"
    key_path = tmp_path / "secrets.key"
    vault = SecretsVault(vault_path)
    vault.init_keyfile(key_path)
    vault.set("jackett_api_key", "jk-xyz", key_file=key_path)

    resolver = SecretsResolver(vault=vault, key_file=key_path)
    assert resolver.resolve("secret:jackett_api_key") == "jk-xyz"


def test_resolver_env_ref(monkeypatch):
    monkeypatch.setenv("NZBGEEK_API_KEY", "from-env")
    resolver = SecretsResolver()
    assert resolver.resolve("env:NZBGEEK_API_KEY") == "from-env"
