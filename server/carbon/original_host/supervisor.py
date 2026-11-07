"""Supervise one original RB in a non-writable directory with redacted logs."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import signal
import stat
import subprocess
import threading
import time

from carbon.original_host.log_filter import filter_stream, read_secret


def _private_append(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    stream = os.fdopen(fd, 'ab', buffering=0)
    st = os.fstat(stream.fileno())
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        stream.close()
        raise ValueError('host log must be a private regular file')
    return stream


class HostSupervisor:
    def __init__(self, *, reference: Path, config: Path, prefix: Path, wine: Path,
                 marker: Path, lock: Path, log: Path, messenger_port=19140,
                 poll_interval=0.2, max_backoff=30.0, env=None):
        self.reference = Path(reference).resolve()
        self.config = Path(config).absolute()
        self.prefix = Path(prefix).resolve()
        self.wine = Path(wine).resolve()
        self.marker = Path(marker).absolute()
        self.lock = Path(lock).absolute()
        self.log = Path(log).absolute()
        if not (1024 <= messenger_port <= 65535):
            raise ValueError('invalid messenger port')
        self.messenger_port = messenger_port
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff
        self.env = dict(os.environ if env is None else env)
        self.stopping = threading.Event()
        self.process = None

    def stop(self):
        self.stopping.set()

    def _check(self):
        st = self.reference.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o222:
            raise ValueError('original host directory must be non-writable')
        if any(child.is_symlink() or not child.is_file() for child in self.reference.iterdir()):
            raise ValueError('original host directory must contain only regular files')
        exe = self.reference / 'rebroadcaster.exe'
        if not exe.is_file() or not self.wine.is_file() or not self.prefix.is_dir():
            raise ValueError('original host binary, Wine or Wine prefix is missing')
        secret = read_secret(self.config)
        # Credentials may only be sent over the local relay; SSL is disabled
        # for that loopback leg. Refuse a changed or ambiguous destination.
        lines = self.config.read_text(encoding='ascii').splitlines()
        for key, value in (('FESLHOST', '127.0.0.1'), ('SSL', 'off'),
                           ('LISTENPORT', str(self.messenger_port))):
            matches = [line.strip() for line in lines if line.startswith('SERVER.' + key + ':')]
            if matches != [f'SERVER.{key}: "{value}"']:
                raise ValueError('host config must use the loopback relay')
        return secret

    def _terminate(self, proc):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

    def run(self):
        secret = self._check()
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            lock_stat = os.fstat(fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_mode & 0o077:
                raise ValueError('supervisor lock must be a private regular file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with _private_append(self.log) as out:
                # A previous instance may have terminated after a close request.
                self.marker.unlink(missing_ok=True)
                backoff = 0.5
                while not self.stopping.is_set():
                    env = self.env.copy()
                    env.update(WINEPREFIX=str(self.prefix), WINEDEBUG='-all', DISPLAY='')
                    started = time.monotonic()
                    proc = subprocess.Popen(
                        [str(self.wine), 'rebroadcaster.exe', '-config', str(self.config),
                         '-host', '127.0.0.1;127.0.0.1;127.0.0.1',
                         '-mssport', str(self.messenger_port)],
                        cwd=self.reference, env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                    self.process = proc
                    filter_errors = []
                    def filter_output():
                        try:
                            filter_stream(proc.stdout, out, secret)
                        except Exception as exc:
                            filter_errors.append(exc)
                    thread = threading.Thread(target=filter_output,
                                              name='original-host-log-filter', daemon=True)
                    thread.start()
                    requested = False
                    output_closed = False
                    try:
                        while not self.stopping.wait(self.poll_interval):
                            if self.marker.exists():
                                requested = True
                                break
                            if proc.poll() is not None:
                                break
                            if not thread.is_alive():
                                output_closed = True
                                break
                    finally:
                        self._terminate(proc)
                        thread.join(timeout=5)
                        if thread.is_alive():
                            raise RuntimeError('original host output filter did not stop')
                        if proc.stdout is not None:
                            proc.stdout.close()
                        self.process = None
                    if filter_errors:
                        raise RuntimeError('original host output filter failed') from filter_errors[0]
                    if output_closed:
                        raise RuntimeError('original host output closed unexpectedly')
                    if requested:
                        self.marker.unlink(missing_ok=True)
                        backoff = 0.5
                        continue
                    if not self.stopping.is_set():
                        if time.monotonic() - started >= 30:
                            backoff = 0.5
                        self.stopping.wait(backoff)
                        backoff = min(self.max_backoff, backoff * 2)
        finally:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Supervise the Carbon original host')
    for name in ('reference', 'config', 'prefix', 'wine', 'marker', 'lock', 'log'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--messenger-port', type=int, default=19140)
    args = parser.parse_args(argv)
    service = HostSupervisor(**vars(args))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: service.stop())
    service.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
