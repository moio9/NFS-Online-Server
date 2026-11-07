"""Opt-in loopback runtime for the original Carbon host."""
import asyncio
from pathlib import Path
import threading

from carbon.original_host.bridge import install
from carbon.original_host.matchmaking import install_matchmaking
from carbon.original_host.seed_coordinator import OriginalSeedCoordinator
from carbon.original_host.seed_worker import OriginalSeedWorker
from carbon.original_host.stream import OriginalHostStream


class OriginalHostRuntime:
    def __init__(self, app, *, account, host_port, stream_port, restart_marker):
        if not account or not restart_marker or app.settings.auth_mode != 'password':
            raise ValueError('original host requires an account, restart marker, and password authentication')
        if not 1024 <= host_port <= 65535 or not 1024 <= stream_port <= 65535 or host_port == stream_port:
            raise ValueError('invalid original host ports')
        self.app = app
        self.seed_account = getattr(app.settings, 'carbon_original_seed_account', '')
        if self.seed_account:
            if self.seed_account.casefold() == account.casefold():
                raise ValueError('initializer account must differ from host account')
            seed = app.credentials.get_account(self.seed_account)
            if seed is None or seed.enabled:
                raise ValueError('initializer requires an existing disabled service account')
        self.port = stream_port
        self.marker = Path(restart_marker)
        self.stream = OriginalHostStream(app.identities, original_port=host_port)
        self._thread = None
        self._loop = None
        self._stop = None
        self._ready = threading.Event()
        self._error = None
        self._seed_lock = threading.Lock()
        self._seed_shutdown = threading.Event()
        self.seed_worker = None
        self.seed_coordinator = None
        install(app, host_account=account, player_endpoint=self.stream.endpoint_for,
                retire_member=self.stream.retire_member, retire_room=self.stream.retire_room,
                retire_connection=self.stream.retire_connection, on_idle_closed=self.request_restart,
                client_udp_port=19140)
        install_matchmaking(app.games)

    def request_restart(self, gid):
        try:
            with self.marker.open('x', encoding='ascii') as file:
                file.write(str(gid) + '\n')
        except FileExistsError:
            return True
        return True

    def start(self):
        if self._thread is not None:
            raise RuntimeError('original host runtime already started')
        self._ready.clear()
        self._error = None
        self._seed_shutdown.clear()
        self._thread = threading.Thread(target=self._thread_main, name='carbon-original-host', daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.stop()
            raise TimeoutError('original host stream did not start')
        if self._error is not None:
            self.stop()
            raise RuntimeError('original host stream startup failed') from self._error
        if self.seed_account:
            try:
                self.seed_coordinator = OriginalSeedCoordinator(
                    self.app.games, self.stream, seed_account=self.seed_account,
                    start_seed=self._start_seed, seed_running=self._seed_running,
                    release_seed=self._release_seed)
            except Exception:
                self.stop()
                raise

    def _start_seed(self, version, gid):
        with self._seed_lock:
            if self._seed_shutdown.is_set():
                raise RuntimeError('original host runtime is stopping')
            if self.seed_worker is None:
                self.seed_worker = OriginalSeedWorker(
                    self.app.identities, self.app.credentials, self.app.games, account=self.seed_account,
                    theater_port=self.app.theater_listener.bound_endpoint.port, stream_port=self.port)
            self.seed_worker.start(version, gid)

    def _seed_running(self):
        with self._seed_lock:
            return self.seed_worker is not None and self.seed_worker.is_running()

    def _release_seed(self):
        with self._seed_lock:
            if self.seed_worker is not None:
                self.seed_worker.release()

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._error = exc
            self._ready.set()

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        server = await asyncio.start_server(self.stream.handle, '127.0.0.1', self.port)
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            server.close()
            await server.wait_closed()

    def stop(self):
        self._seed_shutdown.set()
        if self.seed_coordinator is not None:
            self.seed_coordinator.close()
            self.seed_coordinator = None
        with self._seed_lock:
            worker, self.seed_worker = self.seed_worker, None
        try:
            if worker is not None:
                worker.close()
        finally:
            if self._loop is not None and self._stop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._stop.set)
            if self._thread is not None:
                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    raise TimeoutError('original host runtime failed to stop')
                self._thread = None
