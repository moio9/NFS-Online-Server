"""Coordinate the opt-in Carbon service and original host components.

The existing launcher can monitor this process as its Carbon child. It owns no
Classic services and never edits the live launcher registry or configuration.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import fcntl
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from carbon.core.config import Endpoint, ServerSettings
from common.config import load_service_values
from carbon.original_host.log_filter import read_secret


@dataclass(frozen=True)
class Child:
    name: str
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    log: Path
    ports: tuple[int, ...] = ()
    log_marker: bytes = b''
    timeout: float = 30
    marker_log: Path | None = None


def _private_log(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    file = os.fdopen(fd, 'ab', buffering=0)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        file.close()
        raise ValueError('service log must be a private regular file')
    return file


def _port_ready(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


class ManagedStack:
    def __init__(self, children, ready_file: Path, *, max_backoff=30):
        self.children = tuple(children)
        if not self.children or len({child.name for child in self.children}) != len(self.children):
            raise ValueError('stack requires uniquely named children')
        self.ready_file = Path(ready_file)
        self.max_backoff = max_backoff
        self.stopping = threading.Event()
        self.processes = {}
        self.handles = []

    def stop(self):
        self.stopping.set()

    def _start(self, child):
        out = _private_log(child.log)
        self.handles.append(out)
        marker_log = child.marker_log or child.log
        offset = marker_log.stat().st_size if marker_log.exists() else 0
        proc = subprocess.Popen(child.command, cwd=child.cwd, env=child.env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
        self.processes[child.name] = proc
        deadline = time.monotonic() + child.timeout
        while time.monotonic() < deadline and not self.stopping.is_set():
            failed = next((name for name, process in self.processes.items() if process.poll() is not None), None)
            if failed is not None:
                raise RuntimeError(f'{failed} exited during startup')
            ports_ok = all(_port_ready(port) for port in child.ports)
            marker_ok = True
            if child.log_marker:
                marker_ok = False
                if marker_log.exists():
                    with marker_log.open('rb') as source:
                        source.seek(offset)
                        marker_ok = child.log_marker in source.read()
            if ports_ok and marker_ok:
                # Give startup errors such as a failed bind time to surface.
                if self.stopping.wait(0.2):
                    break
                if proc.poll() is not None:
                    raise RuntimeError(f'{child.name} exited during startup')
                return
            self.stopping.wait(0.1)
        raise RuntimeError(f'{child.name} did not become ready')

    def _publish_ready(self):
        fd = os.open(self.ready_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as out:
            st = os.fstat(out.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                raise ValueError('ready file must be a private regular file')
            json.dump({'pid': os.getpid(), 'services': {k: p.pid for k, p in self.processes.items()}}, out)
            out.flush()
            os.fsync(out.fileno())

    def _shutdown(self):
        self.ready_file.unlink(missing_ok=True)
        for proc in reversed(tuple(self.processes.values())):
            if proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError: pass
        for proc in reversed(tuple(self.processes.values())):
            try: proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait(timeout=5)
        self.processes.clear()
        for handle in self.handles:
            handle.close()
        self.handles.clear()

    def run(self):
        lock_path = self.ready_file.with_name('stack.lock')
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                raise ValueError('stack lock must be a private regular file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.ready_file.unlink(missing_ok=True)
            self._run_locked()
        finally:
            os.close(fd)

    def _run_locked(self):
        ever_ready = False
        backoff = 0.5
        try:
            while not self.stopping.is_set():
                try:
                    for child in self.children:
                        self._start(child)
                    self._publish_ready()
                    ever_ready = True
                    start = time.monotonic()
                    while not self.stopping.wait(0.25):
                        if any(p.poll() is not None for p in self.processes.values()):
                            break
                    if time.monotonic() - start >= 30:
                        backoff = 0.5
                except Exception as exc:
                    if self.stopping.is_set():
                        break
                    if not ever_ready:
                        raise
                    print(f'Carbon stack restart failed: {type(exc).__name__}', file=sys.stderr, flush=True)
                finally:
                    self._shutdown()
                if not self.stopping.is_set():
                    self.stopping.wait(backoff)
                    backoff = min(self.max_backoff, backoff * 2)
        finally:
            self._shutdown()


def make_children(*, release: Path, config: Path, host_config: Path, prefix: Path,
                  wine: Path):
    release, config, host_config = release.resolve(), config.resolve(), host_config.absolute()
    root = release / 'server'
    runtime = release / 'runtime'
    if not runtime.is_dir() or not root.is_dir():
        raise ValueError('release server and runtime directories must exist')
    settings = ServerSettings.load(config)
    if not settings.carbon_original_host_account or not settings.carbon_original_seed_account:
        raise ValueError('config must enable the original host and initializer')
    if settings.carbon_original_host_udp_port != 19140 or settings.carbon_original_stream_port != 19141:
        raise ValueError('public transport requires the configured original-host ports')
    read_secret(host_config)
    config_lines = host_config.read_text(encoding='ascii').splitlines()
    for key in ('LOGIN_NAME', 'SUBACCOUNT'):
        matches = [line.strip() for line in config_lines if line.startswith('SERVER.' + key + ':')]
        if matches != [f'SERVER.{key}: "{settings.carbon_original_host_account}"']:
            raise ValueError('host config does not match server host account')
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(root), str(release), str(release / 'deps')))
    env['PYTHONUNBUFFERED'] = '1'
    env['NFS_LOG_LEVEL'] = 'INFO'
    non_carbon = {key: value for key, value in env.items() if key != 'NFS_IPC_SECRET'}
    messenger_values = load_service_values(config, 'classic')
    messenger_port = Endpoint.parse(messenger_values.get('MESSENGER_LISTEN', '0.0.0.0:13505')).port
    transport_env = non_carbon.copy()
    for service, port in (('FESL', settings.fesl_listen.port),
                          ('THEATER', settings.theater_listen.port),
                          ('MESSENGER', messenger_port),
                          ('RACE', settings.carbon_original_stream_port)):
        if not 1 <= port <= 65535:
            raise ValueError(f'invalid transport target port for {service.lower()}')
        transport_env[f'CARBON_TRANSPORT_{service}_PORT'] = str(port)
    if getattr(settings, 'mad_listen', None) is not None:
        transport_env['CARBON_TRANSPORT_MAD_PORT'] = str(settings.mad_listen.port)
    python = sys.executable
    carbon_log = runtime / 'carbon.log'
    host_log = runtime / 'host.log'
    marker = Path(settings.carbon_original_restart_marker)
    return (
        Child('carbon', (python, '-u', '-m', 'carbon', '--config', str(config)), root / 'carbon', env,
              carbon_log, (settings.fesl_listen.port, settings.theater_listen.port, 19141)),
        Child('relay', (python, '-u', '-m', 'carbon.original_host.relay',
                        '--fesl-target', str(settings.fesl_listen.port),
                        '--theater-target', str(settings.theater_listen.port)), root, non_carbon,
              runtime / 'relay.log', (17220, 17225)),
        Child('host', (python, '-u', '-m', 'carbon.original_host.supervisor',
                       '--reference', str(release / 'reference'), '--config', str(host_config),
                       '--prefix', str(prefix), '--wine', str(wine), '--marker', str(marker),
                       '--lock', str(runtime / 'host.lock'), '--log', str(runtime / 'original.log'),
                       '--messenger-port', str(settings.carbon_original_host_udp_port)), root, non_carbon,
              host_log, (), (f'Carbon Theater CGAM created: persona={settings.carbon_original_host_account}').encode(),
              marker_log=carbon_log),
        Child('transport', (python, '-u', '-m', 'uvicorn', 'carbon.transport.web_app:app',
                            '--host', '127.0.0.1', '--port', '38081', '--ws', 'websockets',
                            '--no-access-log'), release, transport_env, runtime / 'transport.log', (38081,)),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the Carbon original-host stack')
    for name in ('release', 'config', 'host-config', 'prefix', 'wine'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    children = make_children(**vars(args))
    stack = ManagedStack(children, args.release / 'runtime' / 'stack-ready.json')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: stack.stop())
    stack.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
