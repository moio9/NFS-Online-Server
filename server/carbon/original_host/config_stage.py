"""Create a private, opt-in server config next to the live configuration."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import tomllib

from carbon.original_host.provision import _ACCOUNT
from common.config import load_configuration

_HEADER = re.compile(r'^\s*\[carbon\]\s*(?:#.*)?$', re.IGNORECASE)


def stage_config(source, target, *, host_account, seed_account,
                 marker='runtime/carbon-original-host.restart', udp_port=19140, stream_port=19141):
    source, target = Path(source).absolute(), Path(target).absolute()
    if source.parent.resolve() != target.parent.resolve() or source == target:
        raise ValueError('new config must be a different file next to the original')
    if (not isinstance(host_account, str) or not isinstance(seed_account, str)
            or not _ACCOUNT.fullmatch(host_account) or not _ACCOUNT.fullmatch(seed_account)
            or host_account.casefold() == seed_account.casefold()):
        raise ValueError('host and initializer must be distinct valid accounts')
    if not isinstance(marker, str) or not marker or any(c in marker for c in '\r\n\0'):
        raise ValueError('invalid restart marker')
    if (isinstance(udp_port, bool) or isinstance(stream_port, bool)
            or not isinstance(udp_port, int) or not isinstance(stream_port, int)
            or not 1024 <= udp_port <= 65535 or not 1024 <= stream_port <= 65535
            or udp_port == stream_port):
        raise ValueError('original-host ports must be distinct unprivileged ports')
    raw = source.read_text(encoding='utf-8')
    parsed = tomllib.loads(raw)
    sections = [key for key in parsed if key.casefold() == 'carbon']
    if len(sections) != 1 or not isinstance(parsed[sections[0]], dict):
        raise ValueError('source must contain one Carbon section')
    section = sections[0]
    added = {'ORIGINAL_HOST_ACCOUNT': host_account, 'ORIGINAL_SEED_ACCOUNT': seed_account,
             'ORIGINAL_PLAY_NOW_EARLY_START': True, 'ORIGINAL_HOST_UDP_PORT': udp_port,
             'ORIGINAL_STREAM_PORT': stream_port, 'ORIGINAL_RESTART_MARKER': marker}
    if any(str(key).upper() in added for key in parsed[section]):
        raise ValueError('source already has original-host settings')
    lines = raw.splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if _HEADER.match(line.rstrip('\r\n'))]
    if len(positions) != 1:
        raise ValueError('cannot safely locate Carbon section')
    new_lines = [f'{key} = {json.dumps(value) if isinstance(value, str) else str(value).lower()}\n'
                 for key, value in added.items()]
    lines[positions[0] + 1:positions[0] + 1] = new_lines
    staged = ''.join(lines)
    expected = copy.deepcopy(parsed)
    expected[section].update(added)
    if tomllib.loads(staged) != expected:
        raise ValueError('staging would change unrelated configuration')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as output:
            output.write(staged)
            output.flush()
            os.fsync(output.fileno())
        load_configuration(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description='Stage an opt-in Carbon original-host configuration')
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--host-account', required=True)
    parser.add_argument('--seed-account', required=True)
    parser.add_argument('--marker', default='runtime/carbon-original-host.restart')
    parser.add_argument('--udp-port', type=int, default=19140)
    parser.add_argument('--stream-port', type=int, default=19141)
    args = parser.parse_args(argv)
    stage_config(**vars(args))
    print(args.target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
