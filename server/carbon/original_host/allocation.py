"""Short-lived PlayNow ownership while Theater admission is pending.

Callers must hold the game directory's lock while reading or changing a game.
"""
import time


def active_allocation(games, game):
    reservation = getattr(game, 'original_host_allocation', None)
    if reservation is None:
        return None
    clock = getattr(games, 'original_host_allocation_clock', time.monotonic)
    if reservation[1] <= clock():
        game.original_host_allocation = None
        return None
    return reservation


def release_allocation(games, gid, user_id):
    with games._lock:
        game = games.get(gid)
        if game is None:
            return False
        reservation = active_allocation(games, game)
        if reservation is not None and reservation[0] == user_id:
            game.original_host_allocation = None
            return True
        return False
