# Librarry

Hardcover-first ebook orchestrator. Replaces LazyLibrarian with a small Python stack you own.

**Source of truth:** Hardcover Want to Read  
**Search:** Newznab (Usenet) then Torznab (torrents)  
**Download:** SABnzbd and/or qBittorrent  
**Fallback:** Library Genesis  
**Import:** `/mnt/storage/books` with format-aware quality rules

## Secrets

Librarry stores API keys and passwords in an **encrypted local vault** — not in config files or git.

```bash
librarry secrets init --config config.yaml              # key file (cron-friendly)
librarry secrets init --config config.yaml --password   # master password

librarry secrets set hardcover_token --config config.yaml
librarry secrets set nzbgeek_api_key --config config.yaml
librarry secrets set jackett_api_key --config config.yaml
librarry secrets set sab_user --config config.yaml
librarry secrets set sab_password --config config.yaml
librarry secrets set sab_api_key --config config.yaml
librarry secrets set qbit_user --config config.yaml
librarry secrets set qbit_password --config config.yaml
librarry secrets list --config config.yaml
```

Reference secrets in `config.yaml` as `secret:name`:

```yaml
secrets:
  vault: state/secrets.vault
  key_file: state/secrets.key

hardcover:
  token: secret:hardcover_token
```

**Unlock modes**

| Mode | Use case | Cron |
|------|----------|------|
| Key file (`secrets.key`, mode 600) | Servers, mediaprod | Yes — default `secrets init` |
| Master password | Interactive / desktop | Set `LIBRARRY_MASTER_PASSWORD` if needed |
| `env:VAR` | CI or migration | Yes |
| `openbao:path#key` | External vault | Yes, with AppRole creds file |

Vault files use Fernet encryption (PBKDF2-SHA256 with 600k iterations for password mode). Add `secrets.vault`, `secrets.key`, and `config.yaml` to `.gitignore` before publishing.

## Install

```bash
cd Documents/librarry
pip install -e .
cp config.example.yaml /mnt/storage/docker/scripts/librarry/config.yaml
# set env vars or OpenBao creds on mediaprod
librarry init --config /path/to/config.yaml
```

## Commands

| Command | Purpose |
|---------|---------|
| `librarry init` | Create SQLite database |
| `librarry sync` | Hardcover Want to Read → wanted |
| `librarry search` | Search indexers and snatch best release |
| `librarry poll` | Poll SAB/qBit for completed downloads |
| `librarry libgen` | LibGen fallback for still-wanted books |
| `librarry import` | Import completed downloads to library |
| `librarry check` | Validate secrets, paths, SAB/qBit connectivity |
| `librarry books [--status wanted]` | List books |
| `librarry retry` | Reset failed → wanted |
| `librarry secrets bootstrap` | Interactive setup for all credentials |
| `librarry status` | Show counts by status |
| `librarry serve` | Web UI on port **5300** (`LIBRARRY_CONFIG` env supported) |
| `librarry run` | sync → search → poll → libgen → import |

## Cron (mediaprod)

```cron
*/30 * * * * /usr/bin/librarry run --config /mnt/storage/docker/scripts/librarry/config.yaml >> /mnt/storage/docker/scripts/librarry/logs/cron.log 2>&1
```

## Status flow

```
wanted → snatched → imported
              ↘ failed
```

Quality rules reject audiobook releases (m4a, mp3, narrator-tagged NZBs) for ebook requests.

## Kindle delivery

Set `import.send_kindle: true` and store SMTP secrets (`kindle_smtp_from`, `kindle_smtp_user`, `kindle_smtp_password`). Librarry emails the imported file to your `kindle.to` address after a successful library import.

## Web UI

```bash
librarry serve --config config.yaml
# http://localhost:5300
```

The UI has a left-hand navigation panel (Sonarr/Radarr style):

- **Library** — status cards + a sortable, filterable book table with rich Hardcover metadata (series, genre, rating, pages, ISBN, language, publisher, release year) plus on-disk info (file type, filename, path, size). Click any header to sort; filter by author; toggle columns via the **Columns** menu (persisted in localStorage). Per-row **🔍 Interactive Search** lists releases from enabled indexers **plus LibGen and Anna's Archive** (grab any one — Sonarr/Radarr style, with size, age, seeders/leechers, grabs); **🗑** opens delete options: *Delete file & re-queue* (removes the file from disk, keeps the request so it re-searches) or *Remove book entirely* (optionally also deleting the file). Statuses display as Wanted / In Progress / Owned / Failed.
- **Tasks** — run sync / search / poll / libgen / import / full pipeline / retry, with last-run result
- **Add Books** — Hardcover-first search (recommended; pulls full metadata in one call) with an OpenLibrary fallback and manual add
- **Settings** — Email Forwarder (Kindle SMTP), Formats, **Indexers** and **Download Clients** (add/edit/delete from the UI; API keys/passwords stored in the encrypted vault), Import Lists, Search & Run limits
- **System** — Health checks, Logs viewer with **Live tail**, Updates, About

Editable settings are written back to `config.yaml`. Secret references (`secret:…`, `env:…`, `openbao:…`) are never overwritten — credentials stay in the encrypted vault and are edited via `librarry secrets set`.

Homelab: **https://librarry.dugganco.com** via Traefik → `192.168.1.212:5300` (Authentik middleware).

See `deploy/traefik-snippet.yml` and `deploy/librarry.service` or `deploy/docker-compose.yml`.

### mediaprod (homelab)

```bash
cd /mnt/storage/docker/scripts/librarry/deploy
docker compose up -d
docker exec -e LIBRARRY_CONFIG=/config/config.yaml librarry python /app/deploy/bootstrap-mediaprod.py
```

`bootstrap-mediaprod.py` imports credentials from OpenBao (Hardcover) and LazyLibrarian `config.ini` into the encrypted vault. Pipeline cron: `deploy/librarry.cron` → `/etc/cron.d/librarry`.

## Open source

MIT licensed. See `LICENSE`. Never commit `config.yaml`, `secrets.vault`, or `secrets.key`.

Install on a server:

```bash
bash scripts/install.sh
```
