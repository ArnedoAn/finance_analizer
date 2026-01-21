"""
Security Utilities

Provides encryption/decryption for sensitive data like OAuth tokens.
Uses Fernet symmetric encryption from the cryptography library.
"""

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class TokenEncryption:
    """
    Handles encryption and decryption of sensitive tokens.
    
    Uses Fernet symmetric encryption with a key derived from
    the application's TOKEN_ENCRYPTION_KEY setting.
    """
    
    def __init__(self) -> None:
        settings = get_settings()
        self._fernet = self._create_fernet(settings.token_encryption_key)
    
    def _create_fernet(self, key: SecretStr) -> Fernet:
        """Create Fernet instance from encryption key."""
        # Derive a 32-byte key using SHA-256
        key_bytes = key.get_secret_value().encode()
        derived_key = hashlib.sha256(key_bytes).digest()
        # Fernet requires base64-encoded 32-byte key
        fernet_key = base64.urlsafe_b64encode(derived_key)
        return Fernet(fernet_key)
    
    def encrypt(self, data: str) -> str:
        """
        Encrypt a string value.
        
        Args:
            data: Plain text to encrypt.
            
        Returns:
            Base64-encoded encrypted string.
        """
        try:
            encrypted = self._fernet.encrypt(data.encode())
            return encrypted.decode()
        except Exception as e:
            logger.error("encryption_failed", error=str(e))
            raise ValueError("Failed to encrypt data") from e
    
    def decrypt(self, encrypted_data: str) -> str:
        """
        Decrypt an encrypted string.
        
        Args:
            encrypted_data: Base64-encoded encrypted string.
            
        Returns:
            Decrypted plain text.
            
        Raises:
            ValueError: If decryption fails.
        """
        try:
            decrypted = self._fernet.decrypt(encrypted_data.encode())
            return decrypted.decode()
        except InvalidToken as e:
            logger.error("decryption_failed", error="Invalid token")
            raise ValueError("Failed to decrypt data: invalid token") from e
        except Exception as e:
            logger.error("decryption_failed", error=str(e))
            raise ValueError("Failed to decrypt data") from e
    
    def encrypt_dict(self, data: dict[str, Any]) -> str:
        """Encrypt a dictionary as JSON."""
        import json
        return self.encrypt(json.dumps(data))
    
    def decrypt_dict(self, encrypted_data: str) -> dict[str, Any]:
        """Decrypt a JSON string to dictionary."""
        import json
        return json.loads(self.decrypt(encrypted_data))


# Singleton instance
_encryption: TokenEncryption | None = None


def get_token_encryption() -> TokenEncryption:
    """Get the token encryption singleton instance."""
    global _encryption
    if _encryption is None:
        _encryption = TokenEncryption()
    return _encryption
