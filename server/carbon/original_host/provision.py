"""Create a disabled, uniquely named original-host initializer account.

This command never changes server configuration or existing accounts. It
creates one account in the explicitly configured shared SQLite database; the
random password is discarded and is never printed or stored in plaintext.
"""
import argparse
from pathlib import Path
import re
import secrets

from carbon.accounts.credentials import CredentialAccountExistsError
from carbon.accounts.sqlite_backend import SQLiteCredentialStore
from carbon.core.config import ServerSettings
from common.accounts import SQLiteAccountDatabase

_ACCOUNT = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,47}\Z', re.ASCII)


def provision_seed_account(credentials, host_account, *, name=None, name_factory=None):
    if not isinstance(credentials, SQLiteCredentialStore):
        raise TypeError('initializer provisioning requires shared SQLite credentials')
    if not host_account:
        raise ValueError('an original-host account must be configured')
    factory = name_factory or (lambda: 'CarbonSeed_' + secrets.token_hex(4))
    for _ in range(10):
        candidate = name if name is not None else factory()
        if not isinstance(candidate, str) or _ACCOUNT.fullmatch(candidate) is None:
            raise ValueError('invalid initializer account name')
        if candidate.casefold() == host_account.casefold():
            raise ValueError('initializer account must differ from host account')
        try:
            record = credentials.create_account(candidate, secrets.token_urlsafe(48),
                                                enabled=False, replace=False)
        except CredentialAccountExistsError:
            if name is not None:
                raise
            continue
        if record.enabled:
            raise RuntimeError('initializer account was not disabled')
        return record.account_name
    raise RuntimeError('could not allocate a unique initializer account')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Provision a disabled Carbon original-host service account')
    parser.add_argument('--config', required=True, help='Server configuration path')
    parser.add_argument('--account', help='Optional new service-account name; generated if omitted')
    args = parser.parse_args(argv)
    settings = ServerSettings.load(args.config)
    if (settings.auth_mode != 'password' or not settings.carbon_original_host_account
            or not settings.account_db_path or not settings.account_files_path):
        parser.error('requires password authentication, an original host and shared SQLite configuration')
    database_path = Path(settings.account_db_path).expanduser().resolve()
    files_path = Path(settings.account_files_path).expanduser().resolve()
    if not database_path.is_file() or not files_path.is_dir():
        parser.error('configured account database and account-files directory must already exist')
    database = SQLiteAccountDatabase(database_path, files_path,
                                     busy_timeout_ms=settings.account_sqlite_busy_timeout_ms)
    name = provision_seed_account(SQLiteCredentialStore(database), settings.carbon_original_host_account,
                                  name=args.account)
    print(name)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
