#!/usr/bin/env python3
"""Patch Traefik internal-services.yml to add librarry.dugganco.com route."""
from __future__ import annotations

import re
import sys
from pathlib import Path

PATH = Path("/mnt/storage/docker/traefik/dynamic/internal-services.yml")

ROUTER = """
    librarry:
      rule: 'Host(`librarry.dugganco.com`)'
      entrypoints: [websecure]
      tls:
        certResolver: cloudflare
      middlewares: [authentik@file]
      service: librarry"""

SERVICE = """
    librarry:
      loadBalancer:
        servers:
          - url: 'http://192.168.1.212:5300'"""


def main() -> int:
    text = PATH.read_text(encoding="utf-8")
    if "librarry.dugganco.com" in text:
        print("librarry route already present")
        return 0
    if "    library:" in text:
        text = text.replace(
            "    library:\n      rule: 'Host(`library.dugganco.com`)'",
            ROUTER.strip() + "\n    library:\n      rule: 'Host(`library.dugganco.com`)'",
            1,
        )
    else:
        print("Could not find library router anchor", file=sys.stderr)
        return 1
    if "    library:\n      loadBalancer:" in text:
        # insert service before library service block
        text = text.replace(
            "    library:\n      loadBalancer:",
            SERVICE.strip() + "\n    library:\n      loadBalancer:",
            1,
        )
    else:
        print("Could not find library service anchor", file=sys.stderr)
        return 1
    backup = PATH.with_suffix(".yml.bak-librarry")
    backup.write_text(PATH.read_text(encoding="utf-8"), encoding="utf-8")
    PATH.write_text(text, encoding="utf-8")
    print(f"Patched {PATH} (backup {backup})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
