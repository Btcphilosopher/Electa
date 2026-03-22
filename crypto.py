"""
Electa Systems — Cryptographic Utilities
Ed25519 signature verification for vote payloads.
Voters sign a canonical JSON representation with their private key;
the API verifies against the stored public key.
"""

import base64
import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger("electa.crypto")

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False
    logger.warning("cryptography package not installed. Signature verification skipped.")


def canonical_vote_payload(
    proposal_id: str,
    voter_id: str,
    choice: Optional[str],
    timestamp: int,
) -> bytes:
    """
    Deterministic UTF-8 JSON encoding of the fields that are signed.
    Voters must sign exactly this byte sequence.
    """
    return json.dumps(
        {"choice": choice, "proposal_id": proposal_id,
         "timestamp": timestamp, "voter_id": voter_id},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def verify_signature(public_key_pem: str, signature_b64: str, message: bytes) -> bool:
    """Verify a Base64url-encoded Ed25519 signature against a PEM public key."""
    if not _CRYPTO_AVAILABLE:
        logger.warning("Skipping signature verification: cryptography not available.")
        return True

    try:
        signature = base64.urlsafe_b64decode(signature_b64 + "==")
        pubkey: Ed25519PublicKey = load_pem_public_key(public_key_pem.encode())  # type: ignore
        pubkey.verify(signature, message)
        return True
    except InvalidSignature:
        return False
    except Exception as exc:
        logger.warning("Signature verification error: %s", exc)
        return False


def fingerprint(data: bytes) -> str:
    """SHA-256 hex digest — used for audit integrity checks."""
    return hashlib.sha256(data).hexdigest()
