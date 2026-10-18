"""Symmetric encryption for stored OAuth tokens, keyed from HOME_AGENT_SECRET_KEY."""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SecretBox:
    def __init__(self, secret_key: str):
        if not secret_key or len(secret_key) < 16:
            raise ValueError("HOME_AGENT_SECRET_KEY must be set (run: uv run home-agent init)")
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"home-agent", info=b"oauth-token-encryption").derive(
            secret_key.encode("utf-8")
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise ValueError("Stored credentials could not be decrypted (was HOME_AGENT_SECRET_KEY changed?)") from None
