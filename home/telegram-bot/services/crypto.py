"""
Шифрование приватных ключей WireGuard/AmneziaWG в SQLite.

Использует Fernet (AES-128-CBC + HMAC-SHA256) из библиотеки cryptography.
Ключ берётся из переменной окружения DB_ENCRYPTION_KEY (base64url, 32 байта).

Формат хранения: "enc:<fernet_token>" — позволяет безопасно
мигрировать существующие незашифрованные значения.
"""
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:"


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key = os.environ.get("DB_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("DB_ENCRYPTION_KEY не задан в окружении")
    return Fernet(key.encode())


def encrypt_key(plaintext: str) -> str:
    """Зашифровать приватный ключ. Возвращает 'enc:<token>'."""
    token = _get_fernet().encrypt(plaintext.encode()).decode()
    return _PREFIX + token


def decrypt_key(value: str) -> str:
    """
    Расшифровать приватный ключ.
    Если значение не начинается с 'enc:' — возвращает как есть
    (обратная совместимость со старыми незашифрованными записями).
    """
    if not value.startswith(_PREFIX):
        return value
    token = value[len(_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError(f"Не удалось расшифровать ключ: {exc}") from exc
