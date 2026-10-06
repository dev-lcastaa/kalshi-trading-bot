"""RSA-PSS request signing for Kalshi's API (REST + WebSocket handshake).

See https://docs.kalshi.com/getting_started/api_keys
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_private_key_from_file(file_path: str) -> rsa.RSAPrivateKey:
    with open(file_path, "rb") as key_file:
        return serialization.load_pem_private_key(
            key_file.read(), password=None, backend=default_backend()
        )


def sign_pss_text(private_key: rsa.RSAPrivateKey, text: str) -> str:
    message = text.encode("utf-8")
    try:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:  # pragma: no cover - defensive
        raise ValueError("RSA sign PSS failed") from exc
    return base64.b64encode(signature).decode("utf-8")


@dataclass
class KalshiAuth:
    key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_file(cls, key_id: str, private_key_path: str) -> "KalshiAuth":
        return cls(key_id=key_id, private_key=load_private_key_from_file(private_key_path))

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Build KALSHI-ACCESS-* headers. `path` must exclude the query string."""
        path_without_query = path.split("?")[0]
        timestamp_ms = str(int(time.time() * 1000))
        message = timestamp_ms + method.upper() + path_without_query
        signature = sign_pss_text(self.private_key, message)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }
