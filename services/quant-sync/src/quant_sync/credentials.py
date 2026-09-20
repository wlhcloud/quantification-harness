"""Encryption-at-rest for upstream credentials with plaintext migration support."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"


class CredentialCodec:
    def __init__(self, secret: str = ""):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()) if secret else None
        self.fernet = Fernet(key) if key else None

    def encode(self, value: str) -> str:
        if not value or value.startswith(PREFIX) or not self.fernet:
            return value
        return PREFIX + self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decode(self, value: str) -> str:
        if not value.startswith(PREFIX):
            return value
        if not self.fernet:
            raise RuntimeError("QUANT_SYNC_CREDENTIAL_KEY is required to decrypt data-source credentials")
        try:
            return self.fernet.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken as error:
            raise RuntimeError("data-source credential cannot be decrypted with the configured key") from error
