"""Opt-in coordinator for an Unranked original-host initializer.

The caller provides the authenticated seed worker. This component is not
installed automatically and does not create accounts or expose a listener.
"""
import logging
import threading
import time

from carbon.accounts.identity import ClientPlatform
from carbon.theater.matchmaking import parse_request

log = logging.getLogger(__name__)


class OriginalSeedCoordinator:
    def __init__(self, games, stream, *, seed_account, start_seed, seed_running,
                 release_seed, timeout=12.0):
        if not seed_account or timeout <= 0:
            raise ValueError('seed account and positive timeout required')
        self.games = games
        self.stream = stream
        self.seed_account = seed_account.casefold()
        self.start_seed = start_seed
        self.seed_running = seed_running
        self.release_seed = release_seed
        self.timeout = timeout
        self._serial = threading.Lock()
        self._expected_lock = threading.Lock()
        self._expected = None
        self._stopped = threading.Event()
        self._previous_resolve = games.resolve_play_now
        self._previous_observer = stream.on_roster_confirmed
        self._resolve_hook = self.resolve
        self._observer_hook = self.roster_confirmed
        games.resolve_play_now = self._resolve_hook
        stream.on_roster_confirmed = self._observer_hook

    def resolve(self, identity, fields):
        request = parse_request(fields)
        if (identity.client_platform is not ClientPlatform.XBOX360
                or not request.creates_dedicated or request.concrete_game_type != '1'
                or identity.account_name.casefold() == self.seed_account):
            return self._previous_resolve(identity, fields)
        with self._serial:
            deadline = time.monotonic() + self.timeout
            while not self._stopped.is_set() and time.monotonic() < deadline:
                with self.games._lock:
                    candidates = [g for g in self.games.list()
                                  if getattr(g, 'original_host_managed', False)
                                  and g.properties.get('J') == 'O'
                                  and (not request.version or g.properties.get('B-U-version') == request.version)]
                    game = candidates[0] if candidates else None
                    seed = next((p for p in game.participants.values()
                                 if p.identity.account_name.casefold() == self.seed_account), None) if game else None
                    occupied = bool(game and any(p.identity.user_id != game.host.user_id
                                    and p.identity.account_name.casefold() != self.seed_account
                                    for p in game.participants.values()))
                    ready = bool(game and seed and not occupied and game.properties.get('B-U-game_type') == '1')
                    can_start = bool(game and not occupied and not seed
                                     and game.properties.get('B-U-game_type') == '0')
                    if ready:
                        game.original_host_seed_user_id = seed.identity.user_id
                if ready or occupied:
                    break
                if can_start and not self.seed_running():
                    try:
                        self.start_seed(request.version, str(game.gid))
                    except Exception:
                        log.exception('Original host initializer failed to start')
                        break
                self._stopped.wait(0.05)
            if self._stopped.is_set():
                return None
            result = self._previous_resolve(identity, fields)
            if result is not None:
                game = result.game
                with self.games._lock:
                    seed_id = getattr(game, 'original_host_seed_user_id', None)
                    seeded = seed_id in game.participants
                if seeded:
                    with self._expected_lock:
                        self._expected = (str(game.gid), identity.user_id)
            return result

    def roster_confirmed(self, gid, user_id):
        with self._expected_lock:
            if not self._stopped.is_set() and self._expected == (str(gid), user_id):
                with self.games._lock:
                    game = self.games.get(gid)
                    seed_id = getattr(game, 'original_host_seed_user_id', None) if game else None
                    guest = game.participants.get(user_id) if game else None
                    valid = bool(game and getattr(game, 'original_host_managed', False)
                                 and game.properties.get('J') == 'O'
                                 and seed_id in game.participants and guest is not None
                                 and guest.identity.client_platform is ClientPlatform.XBOX360)
                if valid:
                    self.release_seed()
                    self._expected = None
                    log.info('Original host initializer released after allocating player acknowledged self-join')
        if self._previous_observer is not None:
            self._previous_observer(gid, user_id)

    def close(self):
        self._stopped.set()
        if self.games.resolve_play_now is self._resolve_hook:
            self.games.resolve_play_now = self._previous_resolve
        if self.stream.on_roster_confirmed is self._observer_hook:
            self.stream.on_roster_confirmed = self._previous_observer
        with self._expected_lock:
            self._expected = None
