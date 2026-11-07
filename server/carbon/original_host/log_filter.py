"""Redact original rebroadcaster credentials before persisting its output."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys

MAX_LINE = 65536
_PASSWORD_FIELD = re.compile(rb'(?i)(\bpassword\s*[=:]\s*)[^,}\r\n]*')
_CONFIG_PASSWORD = re.compile(rb'^SERVER\.PASSWORD:\s*"([A-Za-z0-9_-]{1,24})"\s*$')


def read_secret(path: Path) -> bytes:
    # Open without following a substituted symlink, and inspect the descriptor.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as config:
        st = os.fstat(config.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
            raise ValueError('host config must be a private regular file')
        data = config.read(MAX_LINE + 1)
        if len(data) > MAX_LINE:
            raise ValueError('host config is too large')
    matches = [_CONFIG_PASSWORD.fullmatch(line) for line in data.splitlines()]
    passwords = [match.group(1) for match in matches if match]
    if len(passwords) != 1:
        raise ValueError('host config must contain one valid password')
    return passwords[0]


def redact_line(line: bytes, secret: bytes) -> bytes:
    if not secret:
        raise ValueError('empty secret')
    line = line.replace(secret, b'[redacted]')
    return _PASSWORD_FIELD.sub(rb'\1[redacted]', line)


def filter_stream(source, destination, secret: bytes):
    while True:
        line = source.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            # Never emit a partial long line: it might split a credential.
            while line and not line.endswith(b'\n'):
                line = source.readline(MAX_LINE + 1)
            destination.write(b'[oversized original-host log line omitted]\n')
        else:
            destination.write(redact_line(line, secret))
        destination.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Filter original host logs')
    parser.add_argument('--host-config', required=True)
    args = parser.parse_args(argv)
    secret = read_secret(Path(args.host_config))
    filter_stream(sys.stdin.buffer, sys.stdout.buffer, secret)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
