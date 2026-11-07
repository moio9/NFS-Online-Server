"""Provision a dedicated original-RB login and private loopback config.

No existing account or configuration is replaced.  Accounts are inserted
without login permission and enabled only after the private config is durable.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets

from carbon.accounts.credentials import CredentialAccountExistsError
from carbon.accounts.sqlite_backend import SQLiteCredentialStore
from carbon.core.config import ServerSettings
from carbon.original_host.provision import _ACCOUNT
from common.accounts import SQLiteAccountDatabase


def _config(account: str, password: str, udp_port: int) -> str:
    values = (
        ('PLATFORM', 'XBOX360'), ('ENV', 'fesl-test'), ('CLIENTTYPE', 'server'),
        ('SSL', 'off'), ('THELOCALE', 'en_US'), ('VERSIONSTR', '295_prod_server'),
        ('FESLHOST', '127.0.0.1'), ('LOCATION', 'local'),
        ('SERVICEID', '0x45410000'), ('LISTENPORT', str(udp_port)),
        ('LOGIN_NAME', account), ('PASSWORD', password), ('SUBACCOUNT', account),
        ('GAMENAME', 'CarbonOriginalHost'), ('OPEN', 'true'),
    )
    return ''.join(f'SERVER.{key}: "{value}"\n' for key, value in values)


def provision_host(credentials, config_path, *, udp_port=19140, name=None, name_factory=None):
    if not isinstance(credentials, SQLiteCredentialStore):
        raise TypeError('host provisioning requires shared SQLite credentials')
    if isinstance(udp_port, bool) or not isinstance(udp_port, int) or not 1024 <= udp_port <= 65535:
        raise ValueError('host UDP port must be an unprivileged port')
    path = Path(config_path)
    if not path.parent.is_dir():
        raise ValueError('config directory must already exist')
    # The original executable's login-name buffer is small. Account names are
    # identifiers, not credentials; a short random suffix avoids collisions.
    factory = name_factory or (lambda: 'CarbonHost_' + secrets.token_hex(4))
    # Reserve the path before any DB mutation. Never follow or replace a file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    created_account = None
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            for _ in range(10):
                candidate = name if name is not None else factory()
                if not isinstance(candidate, str) or len(candidate) > 20 or _ACCOUNT.fullmatch(candidate) is None:
                    raise ValueError('invalid host account name')
                # Original RB stores this field in a fixed-size C buffer; keep the
                # 144-bit password below its limit (24 URL-safe characters).
                password = secrets.token_urlsafe(18)
                try:
                    record = credentials.create_account(candidate, password, enabled=False, replace=False)
                except CredentialAccountExistsError:
                    if name is not None:
                        raise
                    continue
                created_account = record.account_name
                if record.enabled:
                    raise RuntimeError('host account was not created disabled')
                out.write(_config(record.account_name, password, udp_port))
                out.flush()
                os.fsync(out.fileno())
                # The secret must remain available to recover even if the
                # subsequent DB operation fails after committing.
                enabled = credentials.set_enabled(record.account_name, True)
                if not enabled.enabled:
                    raise RuntimeError('host account could not be enabled')
                return record.account_name
            raise RuntimeError('could not allocate a unique host account')
    except BaseException:
        # A failed enable may have committed before raising; disable the new
        # account if the database is reachable. Keep its config for recovery.
        if created_account:
            try:
                credentials.set_enabled(created_account, False)
            except Exception:
                pass
        if not created_account:
            path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Provision a private Carbon original-host login')
    parser.add_argument('--config', required=True, help='Existing server configuration path')
    parser.add_argument('--host-config', required=True, help='New private RB configuration path')
    parser.add_argument('--account', help='Optional unused account name')
    parser.add_argument('--udp-port', type=int, default=19140)
    args = parser.parse_args(argv)
    settings = ServerSettings.load(args.config)
    if settings.auth_mode != 'password' or not settings.account_db_path or not settings.account_files_path:
        parser.error('requires password authentication and shared SQLite configuration')
    db_path = Path(settings.account_db_path).expanduser().resolve()
    files_path = Path(settings.account_files_path).expanduser().resolve()
    if not db_path.is_file() or not files_path.is_dir():
        parser.error('configured account database and account-files directory must already exist')
    database = SQLiteAccountDatabase(db_path, files_path, busy_timeout_ms=settings.account_sqlite_busy_timeout_ms)
    account = provision_host(SQLiteCredentialStore(database), args.host_config,
                             udp_port=args.udp_port, name=args.account)
    print(account)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
