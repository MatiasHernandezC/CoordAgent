import base64
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialEncryptionError(RuntimeError):
    pass


def decode_master_key(encoded_key: str) -> bytes:
    value = encoded_key.strip()
    if not value:
        raise CredentialEncryptionError("Falta LLM_KEYS_MASTER_KEY.")
    try:
        padding = "=" * (-len(value) % 4)
        key = base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as error:
        raise CredentialEncryptionError("LLM_KEYS_MASTER_KEY no es base64 valido.") from error
    if len(key) != 32:
        raise CredentialEncryptionError("LLM_KEYS_MASTER_KEY debe contener exactamente 32 bytes.")
    return key


def encrypt_secret(secret: str, encoded_key: str, credential_id: str) -> str:
    plaintext = secret.strip().encode("utf-8")
    if not plaintext:
        raise CredentialEncryptionError("La llave Gemini esta vacia.")
    key = decode_master_key(encoded_key)
    nonce = os.urandom(12)
    aad = _aad(credential_id)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return "v1.{nonce}.{ciphertext}".format(
        nonce=_encode(nonce),
        ciphertext=_encode(ciphertext),
    )


def decrypt_secret(payload: str, encoded_key: str, credential_id: str) -> str:
    try:
        version, nonce_text, ciphertext_text = payload.split(".", 2)
        if version != "v1":
            raise ValueError("version")
        nonce = _decode(nonce_text)
        ciphertext = _decode(ciphertext_text)
        plaintext = AESGCM(decode_master_key(encoded_key)).decrypt(
            nonce,
            ciphertext,
            _aad(credential_id),
        )
        return plaintext.decode("utf-8")
    except (ValueError, UnicodeDecodeError, InvalidTag) as error:
        raise CredentialEncryptionError(
            "No fue posible descifrar la llave Gemini con la llave maestra actual."
        ) from error


def secret_fingerprint(secret: str, encoded_key: str) -> str:
    key = decode_master_key(encoded_key)
    return hmac.new(key, secret.strip().encode("utf-8"), hashlib.sha256).hexdigest()


def _aad(credential_id: str) -> bytes:
    return f"coordina:gemini-key:{credential_id}:v1".encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
