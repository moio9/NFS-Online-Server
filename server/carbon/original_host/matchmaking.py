"""Opt-in PlayNow selection for sessions using the original host."""
import time

from carbon.accounts.identity import ClientPlatform
from carbon.original_host.allocation import active_allocation
from carbon.theater.directory import CarbonPlayNowResolution
from carbon.theater.matchmaking import match_fit, parse_request, strict_match


def install_matchmaking(games, *, clock=time.monotonic, reservation_seconds=30.0):
    if reservation_seconds <= 0:
        raise ValueError('reservation duration must be positive')
    games.original_host_allocation_clock = clock
    original_resolve = games.resolve_play_now

    def resolve(identity, fields):
        if identity.client_platform is not ClientPlatform.XBOX360:
            return original_resolve(identity, fields)
        request = parse_request(fields)
        if not (request.is_find or request.creates_dedicated):
            return original_resolve(identity, fields)
        with games._lock:
            candidates = []
            for game in games.list():
                if not getattr(game, 'original_host_managed', False):
                    continue
                if game.properties.get('J') != 'O' or game.quick_join_locked:
                    continue
                if identity.user_id in game.participants or len(game.participants) >= game.session.capacity:
                    continue
                # A successful reset selects a host before the player sends
                # Theater EGAM. Reserve that short gap so two simultaneous
                # reset requests do not both allocate the same empty host.
                reservation = active_allocation(games, game)
                if reservation is not None:
                    owner, _ = reservation
                    if owner != identity.user_id:
                        continue
                mode = game.properties.get('B-U-game_type')
                if mode not in {'0', '1'} or mode not in request.allowed_game_types:
                    continue
                if request.version and game.properties.get('B-U-version') != request.version:
                    continue
                guest_ids = {uid for uid in game.participants if uid != game.host.user_id}
                guests = len(guest_ids)
                maximum = game.properties.get('B-U-max_online_player', '')
                if maximum.isdecimal() and 1 <= int(maximum) <= 8 and guests >= int(maximum):
                    continue
                if request.creates_dedicated:
                    # An empty original host receives its initial attributes
                    # from the first player. Never reuse a running session or
                    # silently put an unranked allocation into a ranked host.
                    seed_id = getattr(game, 'original_host_seed_user_id', None)
                    seed_only = mode == '1' and seed_id is not None and guest_ids == {seed_id}
                    if (guests and not seed_only) or request.concrete_game_type != mode:
                        continue
                    fit = 1.0
                else:
                    # Generic initial properties are not an authoritative game
                    # snapshot. Search only after the original host publishes
                    # its actual matchmaking state.
                    if not getattr(game, 'original_host_matchmaking_published', False):
                        continue
                    if not strict_match(request, game.properties):
                        continue
                    fit = match_fit(fields, game.properties)
                    if fit is None or fit < request.fit_threshold:
                        continue
                candidates.append((fit, game))
            if not candidates:
                return None
            fit, game = max(candidates, key=lambda item: (item[0], -int(item[1].gid)))
            if request.creates_dedicated:
                game.original_host_allocation = (identity.user_id, clock() + reservation_seconds)
            return CarbonPlayNowResolution(game, fit, False)

    games.resolve_play_now = resolve
