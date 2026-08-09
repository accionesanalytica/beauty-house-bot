"""Secure storage and lookup for the Tiendanube OAuth credential.

The existing Railway token remains a fallback. Once the owner authorizes the
real store through the assisted OAuth route, its token is encrypted before it
is saved in Supabase and is preferred for all production API calls.
"""

import base64
import hashlib
import os
from typing import Dict

import psycopg2
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()


class TiendanubeCredentialError(RuntimeError):
    """The API credential cannot be safely used."""


def _connect():
    database_url = os.getenv("SUPABASE_DB_URL", "").strip()
    if not database_url:
        raise TiendanubeCredentialError("Falta SUPABASE_DB_URL en Railway.")
    return psycopg2.connect(database_url, connect_timeout=10)


def _cipher() -> Fernet:
    """Derive a stable encryption key from the app secret kept in Railway."""

    client_secret = os.getenv("TIENDANUBE_CLIENT_SECRET", "").strip()
    if not client_secret:
        raise TiendanubeCredentialError(
            "Falta TIENDANUBE_CLIENT_SECRET para proteger la autorización."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(client_secret.encode("utf-8")).digest())
    return Fernet(key)


def _ensure_storage(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_credentials (
            provider TEXT PRIMARY KEY,
            store_id TEXT NOT NULL,
            encrypted_access_token TEXT NOT NULL,
            scopes TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def save_tiendanube_credential(
    store_id: str,
    access_token: str,
    scopes: str = "",
) -> None:
    """Store a newly authorized token only for the configured production store."""

    expected_store_id = os.getenv("TIENDANUBE_STORE_ID", "").strip()
    if not expected_store_id:
        raise TiendanubeCredentialError("Falta TIENDANUBE_STORE_ID en Railway.")
    if store_id != expected_store_id:
        raise TiendanubeCredentialError(
            "La autorización corresponde a otra tienda ({}), no a Beauty House.".format(
                store_id
            )
        )
    if not access_token:
        raise TiendanubeCredentialError("Tiendanube no devolvió un access token.")

    encrypted_token = _cipher().encrypt(access_token.encode("utf-8")).decode("utf-8")
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                INSERT INTO integration_credentials (
                    provider, store_id, encrypted_access_token, scopes, updated_at
                ) VALUES ('tiendanube', %s, %s, %s, now())
                ON CONFLICT (provider) DO UPDATE
                SET store_id = EXCLUDED.store_id,
                    encrypted_access_token = EXCLUDED.encrypted_access_token,
                    scopes = EXCLUDED.scopes,
                    updated_at = now()
                """,
                (store_id, encrypted_token, scopes),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _authorized_credential() -> Dict[str, str]:
    """Load the encrypted credential, if the owner has completed OAuth."""

    if os.getenv("TIENDANUBE_OAUTH_CREDENTIALS_ENABLED", "true").lower() != "true":
        return {}

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT store_id, encrypted_access_token
                FROM integration_credentials
                WHERE provider = 'tiendanube'
                """
            )
            row = cursor.fetchone()
        connection.commit()
    except psycopg2.errors.UndefinedTable:
        connection.rollback()
        return {}
    finally:
        connection.close()

    if not row:
        return {}
    try:
        token = _cipher().decrypt(row[1].encode("utf-8")).decode("utf-8")
    except InvalidToken as error:
        raise TiendanubeCredentialError(
            "La autorización guardada no se puede descifrar. Conectá Tiendanube nuevamente."
        ) from error
    return {"store_id": row[0], "access_token": token}


def get_tiendanube_configuration() -> Dict[str, str]:
    """Return the current production credential without exposing it in logs."""

    user_agent = os.getenv(
        "TIENDANUBE_USER_AGENT", "BeautyHouseBot (support@example.com)"
    ).strip()
    authorized = _authorized_credential()
    if authorized:
        authorized["user_agent"] = user_agent
        return authorized

    store_id = os.getenv("TIENDANUBE_STORE_ID", "").strip()
    access_token = os.getenv("TIENDANUBE_ACCESS_TOKEN", "").strip()
    if not store_id or not access_token:
        raise TiendanubeCredentialError("Falta la configuración de Tiendanube en Railway.")
    return {
        "store_id": store_id,
        "access_token": access_token,
        "user_agent": user_agent,
    }
