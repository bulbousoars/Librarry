from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes

from librarry.config import OIDCConfig


class OIDCError(RuntimeError):
    pass


@dataclass
class OIDCClient:
    cfg: OIDCConfig

    def discovery(self) -> dict[str, Any]:
        if not self.cfg.issuer:
            raise OIDCError("OIDC issuer is not configured")
        resp = requests.get(
            f"{self.cfg.issuer.rstrip('/')}/.well-known/openid-configuration",
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("issuer", "").rstrip("/") != self.cfg.issuer.rstrip("/"):
            raise OIDCError("OIDC discovery issuer does not match configured issuer")
        return data

    def authorization_url(self, state: str, nonce: str) -> str:
        disc = self.discovery()
        return (
            disc["authorization_endpoint"]
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.cfg.client_id,
                    "redirect_uri": self.cfg.redirect_uri,
                    "scope": " ".join(self.cfg.scopes or ["openid", "email", "profile"]),
                    "state": state,
                    "nonce": nonce,
                }
            )
        )

    def callback_claims(self, code: str, nonce: str) -> dict[str, Any]:
        disc = self.discovery()
        token = self.exchange_code(disc["token_endpoint"], code)
        id_token = token.get("id_token")
        if not id_token:
            raise OIDCError("OIDC token response did not include id_token")
        claims = self.validate_id_token(id_token, nonce, disc)
        if not claims.get("email") and disc.get("userinfo_endpoint") and token.get("access_token"):
            try:
                resp = requests.get(
                    disc["userinfo_endpoint"],
                    headers={"Authorization": f"Bearer {token['access_token']}"},
                    timeout=20,
                )
                resp.raise_for_status()
                claims.update({k: v for k, v in resp.json().items() if v is not None})
            except Exception:
                pass
        return claims

    def exchange_code(self, token_endpoint: str, code: str) -> dict[str, Any]:
        resp = requests.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.cfg.redirect_uri,
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def validate_id_token(self, id_token: str, nonce: str, discovery: dict[str, Any] | None = None) -> dict[str, Any]:
        header, claims, signing_input, signature = _split_jwt(id_token)
        alg = header.get("alg")
        if alg == "RS256":
            disc = discovery or self.discovery()
            jwks_uri = disc.get("jwks_uri")
            if not jwks_uri:
                raise OIDCError("OIDC discovery did not include jwks_uri")
            resp = requests.get(jwks_uri, timeout=20)
            resp.raise_for_status()
            key = _select_jwk(resp.json().get("keys") or [], header.get("kid"))
            _verify_rs256(key, signing_input, signature)
        elif alg == "HS256":
            expected = hmac.new(self.cfg.client_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, signature):
                raise OIDCError("OIDC id_token signature is invalid")
        else:
            raise OIDCError(f"Unsupported OIDC id_token algorithm: {alg}")

        issuer = str(claims.get("iss", "")).rstrip("/")
        if issuer != self.cfg.issuer.rstrip("/"):
            raise OIDCError("OIDC id_token issuer is invalid")
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self.cfg.client_id not in audiences:
            raise OIDCError("OIDC id_token audience is invalid")
        if int(claims.get("exp", 0)) <= int(time.time()):
            raise OIDCError("OIDC id_token is expired")
        if nonce and claims.get("nonce") != nonce:
            raise OIDCError("OIDC id_token nonce is invalid")
        if not claims.get("sub"):
            raise OIDCError("OIDC id_token missing sub claim")
        return claims


def _split_jwt(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    try:
        h, c, s = token.split(".")
        return (
            json.loads(_b64url_decode(h)),
            json.loads(_b64url_decode(c)),
            f"{h}.{c}".encode("ascii"),
            _b64url_decode(s),
        )
    except Exception as exc:
        raise OIDCError("OIDC id_token is not a valid JWT") from exc


def _select_jwk(keys: list[dict[str, Any]], kid: str | None) -> dict[str, Any]:
    for key in keys:
        if key.get("kty") == "RSA" and (not kid or key.get("kid") == kid):
            return key
    raise OIDCError("No matching RSA key in OIDC JWKS")


def _verify_rs256(jwk: dict[str, Any], signing_input: bytes, signature: bytes) -> None:
    public_numbers = rsa.RSAPublicNumbers(
        e=int.from_bytes(_b64url_decode(jwk["e"]), "big"),
        n=int.from_bytes(_b64url_decode(jwk["n"]), "big"),
    )
    public_key = public_numbers.public_key()
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        raise OIDCError("OIDC id_token signature is invalid") from exc


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
