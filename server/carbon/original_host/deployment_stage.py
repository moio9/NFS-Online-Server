"""Stage dedicated Carbon service accounts and configuration without switching."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from carbon.accounts.sqlite_backend import SQLiteCredentialStore
from carbon.original_host.config_stage import stage_config
from carbon.original_host.provision import provision_seed_account
from carbon.original_host.provision_host import provision_host
from common.accounts import SQLiteAccountDatabase
from common.config import load_configuration, package_root_for, resolve_package_path


def stage_deployment(source, target, host_config, metadata):
    source, target, host_config, metadata = map(lambda p: Path(p).absolute(),
                                                (source, target, host_config, metadata))
    if len({source, target, host_config, metadata}) != 4:
        raise ValueError('deployment paths must be distinct')
    if source.parent.resolve() != target.parent.resolve():
        raise ValueError('candidate config must be next to the source config')
    for path in (target, host_config, metadata):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f'target already exists: {path}')
        if not path.parent.is_dir():
            raise ValueError('all target directories must already exist')
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    values = load_configuration(source)['global']
    if values['AUTH_MODE'].casefold() != 'password':
        raise ValueError('original host requires password authentication')
    package_root = package_root_for(source)
    db_path = resolve_package_path(package_root, values['ACCOUNT_DB'])
    files_path = resolve_package_path(package_root, values['ACCOUNT_FILES'])
    if not db_path.is_file() or not files_path.is_dir():
        raise ValueError('configured account database and account-files directory must exist')
    database = SQLiteAccountDatabase(db_path, files_path,
        busy_timeout_ms=int(values['ACCOUNT_SQLITE_BUSY_TIMEOUT_MS']))
    credentials = SQLiteCredentialStore(database)
    host = None
    target_created = False
    try:
        host = provision_host(credentials, host_config)
        seed = provision_seed_account(credentials, host)
        stage_config(source, target, host_account=host, seed_account=seed)
        target_created = True
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError('source config changed while staging')
        info = {'source': str(source), 'source_sha256': source_hash,
                'candidate': str(target), 'candidate_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                'host_config': str(host_config), 'host_account': host, 'seed_account': seed}
        fd = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(info, out, sort_keys=True)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            metadata.unlink(missing_ok=True)
            raise
        return info
    except BaseException:
        if host is not None:
            credentials.set_enabled(host, False)
        if target_created:
            target.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Stage an opt-in Carbon original-host deployment')
    for name in ('source', 'target', 'host-config', 'metadata'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    info = stage_deployment(**vars(args))
    print('Staged Carbon deployment for service account', info['host_account'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
