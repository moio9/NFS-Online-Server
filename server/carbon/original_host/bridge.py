"""Experimental Theater admission for an explicitly configured original host.

The caller supplies the routable UDP endpoint for each player. This module
does not expose or reserve a public UDP port and is not enabled by default.
"""
from threading import Event, RLock
import logging
from carbon.fesl.frame import FESLFrame
from carbon.original_host.allocation import active_allocation, release_allocation

log = logging.getLogger(__name__)

def _frame(command, fields):
    return FESLFrame.from_fields(command, fields, transaction=0)


def install(app, *, host_account, player_endpoint, timeout=3, retire_member=None,
            retire_room=None, retire_connection=None, on_idle_closed=None,
            client_udp_port=None):
    """Connect one authenticated loopback host to its Theater-created rooms."""
    if not host_account or timeout <= 0:
        raise ValueError('host account and positive timeout required')
    theater = app.theater
    games = theater.games
    original_dispatch = theater.dispatch
    original_disconnect = theater.disconnect
    lock = RLock()
    rooms = {}  # gid -> (connection, host user id)
    pending = {}  # (gid, player id) -> (connection, event, mutable result)
    restart_pending = set()
    retire_member = retire_member or (lambda gid, user_id: None)
    retire_room = retire_room or (lambda gid: None)
    retire_connection = retire_connection or (lambda connection: None)

    def maybe_restart(gid):
        if on_idle_closed is None or not gid:
            return
        # Never replace the original process while it has a participant or
        # an admission in flight. The directory lock precedes our lock.
        with games._lock:
            game = games.get(gid)
            if (game is None or game.properties.get('J') != 'C'
                    or any(uid != game.host.user_id for uid in game.participants)):
                return
            with lock:
                if gid not in rooms or gid in restart_pending or any(key[0] == gid for key in pending):
                    return
                restart_pending.add(gid)
        try:
            if not on_idle_closed(gid):
                with lock:
                    restart_pending.discard(gid)
        except Exception:
            with lock:
                restart_pending.discard(gid)
            log.exception('Original host idle restart request failed')

    def dispatch(frame, connection):
        fields = frame.fields
        command = frame.command
        identity = connection.identity
        admission_replies = None
        if command == 'EGAM' and identity is not None:
            gid = fields.get('GID', '') or getattr(connection, 'selected_gid', '')
            with lock:
                restarting = gid in restart_pending
            if restarting:
                release_allocation(games, gid, identity.user_id)
                return [_frame('EGAM', {'TID': fields.get('TID', '0'), 'ERR': 'HOST_UNAVAILABLE'})]
            # Check reservation and create the directory participant under one
            # lock, so a direct Theater join cannot race a PlayNow allocation.
            with games._lock:
                game = games.get(gid)
                if game is not None and getattr(game, 'original_host_managed', False):
                    reservation = active_allocation(games, game)
                    if reservation is not None and reservation[0] != identity.user_id:
                        return [_frame('EGAM', {'TID': fields.get('TID', '0'),
                                                'ERR': 'HOST_UNAVAILABLE'})]
                    admission_replies = original_dispatch(frame, connection)
        if command == 'ECNL' and identity is not None:
            gid = fields.get('GID', '') or getattr(connection, 'selected_gid', '')
            with lock:
                is_original = gid in rooms
            if is_original:
                # Retail can send ECNL while transitioning during a race.
                # The actual host is authoritative and later sends PLVT.
                log.info('Original host: deferred ECNL for room %s', gid)
                return [_frame('ECNL', {'TID': fields.get('TID', '0'),
                                        'LID': fields.get('LID', '257') or '257', 'GID': gid})]
        if command in ('EGRS', 'PENT', 'PLVT', 'UBRA', 'UGAM', 'UGDE'):
            gid = fields.get('GID', '')
            with lock:
                host = rooms.get(gid)
                # EGRS may omit GID only if it can be unambiguously correlated.
                if host is None and command == 'EGRS' and not gid:
                    matches = [(key, value) for key, value in pending.items()
                               if key[1] == fields.get('PID') and value[0] is connection]
                    if len(matches) == 1:
                        gid = matches[0][0][0]
                        host = rooms.get(gid)
                if host is None or host[0] is not connection:
                    return original_dispatch(frame, connection)
                if command == 'EGRS':
                    waiter = pending.get((gid, fields.get('PID', '')))
                    if waiter and waiter[0] is connection:
                        waiter[2]['allowed'] = fields.get('ALLOWED') == '1'
                        waiter[1].set()
            if command == 'UGAM':
                with games._lock:
                    game = games.get(gid)
                    if game:
                        join = fields.get('JOIN', fields.get('J', ''))
                        if join in ('O', 'C'):
                            if game.properties.get('J') != join:
                                log.info('Original host reported join state %s for room %s', join, gid)
                            game.properties['J'] = join
                        mode = fields.get('B-U-game_type')
                        if mode in ('0', '1', '2'):
                            game.properties['B-U-game_type'] = mode
                            log.info('Original host reported game type %s for room %s', mode, gid)
                        state = fields.get('B-U-matchmaking_state')
                        if state in ('0', '1'):
                            game.properties['B-U-matchmaking_state'] = state
                            game.original_host_matchmaking_published = True
                        # Publish only bounded, concrete preference values
                        # supplied by the authenticated original host.
                        for name in ('help_type', 'game_mode', 'car_tier',
                                     'collision_detection', 'n2o', 'length',
                                     'team_play', 'race_type_circuit',
                                     'race_type_knockout', 'race_type_speedtrap',
                                     'race_type_pursuit_tag', 'race_type_canyon_due',
                                     'race_type_sprint', 'max_online_player',
                                     'player_dnf', 'skill'):
                            key = 'B-U-' + name
                            value = fields.get(key)
                            if value is not None and len(value) <= 64 and '|' not in value:
                                game.properties[key] = value
                                if name == 'game_mode':
                                    log.info('Original host reported game mode %s for room %s', value, gid)
                if fields.get('JOIN', fields.get('J', '')) == 'C':
                    maybe_restart(gid)
            if command == 'PLVT':
                departed = None
                with games._lock:
                    game = games.get(gid)
                    participant = next((p for p in game.participants.values()
                        if str(p.player_id) == fields.get('PID')
                        and p.identity.user_id != game.host.user_id), None) if game else None
                    if participant:
                        games.leave(gid, participant.identity.user_id, reason='original-host-left')
                        departed = participant.identity.user_id
                if departed is not None:
                    log.info('Original host: PLVT received for room %s', gid)
                    retire_member(gid, departed)
                maybe_restart(gid)
            reply = {'TID': fields.get('TID', '0')}
            if command == 'PENT' and 'PID' in fields:
                reply['PID'] = fields['PID']
            return [_frame(command, reply)]

        replies = admission_replies if admission_replies is not None else original_dispatch(frame, connection)
        if (command == 'CGAM' and identity is not None
                and identity.account_name.casefold() == host_account.casefold()
                and connection.peer_ip == '127.0.0.1'):
            created = next((r for r in replies if r.command == 'CGAM' and not r.fields.get('ERR')), None)
            if created and created.fields.get('GID'):
                gid = created.fields['GID']
                with lock:
                    rooms[gid] = (connection, identity.user_id)
                # The original host starts as ranked unless it publishes a mode.
                if fields.get('B-U-game_type') not in ('0', '1', '2'):
                    with games._lock:
                        game = games.get(gid)
                        if game:
                            game.properties['B-U-game_type'] = '0'
                with games._lock:
                    game = games.get(gid)
                    if game:
                        game.original_host_managed = True
        if command != 'EGAM' or identity is None:
            return replies
        result = next((r for r in replies if r.command == 'EGEG'), None)
        if result is None:
            release_allocation(games, fields.get('GID', '') or getattr(connection, 'selected_gid', ''), identity.user_id)
            return replies
        data = result.fields
        gid, pid = data.get('GID', ''), data.get('PID', '')
        with lock:
            host = rooms.get(gid)
        if host is None:
            release_allocation(games, gid, identity.user_id)
            return replies
        endpoint = player_endpoint(connection, {**fields, 'GID': gid, 'PID': pid})
        if endpoint is None:
            games.leave(gid, identity.user_id, reason='original-host-endpoint-unavailable')
            release_allocation(games, gid, identity.user_id)
            return [_frame('EGAM', {'TID': fields.get('TID', '0'), 'ERR': 'HOST_UNAVAILABLE'})]
        ip, port = endpoint
        request = {'R-INT-PORT': str(port), 'R-INT-IP': str(ip), 'PORT': str(port),
                   'NAME': identity.persona, 'PTYPE': fields.get('PTYPE', '0'),
                   'TICKET': data['TICKET'], 'PID': pid, 'UID': str(identity.user_id),
                   'IP': str(ip), 'LID': data['LID'], 'GID': gid}
        key = (gid, pid)
        event, response = Event(), {}
        with lock:
            unavailable = rooms.get(gid) != host or gid in restart_pending or key in pending
            if not unavailable:
                pending[key] = (host[0], event, response)
        if unavailable:
            games.leave(gid, identity.user_id, reason='original-host-unavailable')
            retire_member(gid, identity.user_id)
            release_allocation(games, gid, identity.user_id)
            return [_frame('EGAM', {'TID': fields.get('TID', '0'), 'ERR': 'HOST_UNAVAILABLE'})]
        try:
            sent = host[0].deliver(_frame('EGRQ', request))
            answered = sent and event.wait(timeout)
        except BaseException:
            release_allocation(games, gid, identity.user_id)
            raise
        finally:
            with lock:
                pending.pop(key, None)
        if not answered or not response.get('allowed'):
            games.leave(gid, identity.user_id, reason='original-host-rejected')
            retire_member(gid, identity.user_id)
            release_allocation(games, gid, identity.user_id)
            maybe_restart(gid)
            error = 'HOST_REJECTED' if answered else 'HOST_TIMEOUT'
            return [_frame('EGAM', {'TID': fields.get('TID', '0'), 'ERR': error})]
        data['HUID'] = str(host[1])
        release_allocation(games, gid, identity.user_id)
        if client_udp_port is not None:
            # The Xbox client uses its authenticated local tunnel, not the
            # production server's PC UDP listener.
            data.update({'I': '127.0.0.1', 'P': str(client_udp_port),
                         'INT-IP': '127.0.0.1', 'INT-PORT': str(client_udp_port)})
        return [_frame('EGEG', data) if reply is result else reply for reply in replies]

    def disconnect(connection):
        with lock:
            lost = [gid for gid, host in rooms.items() if host[0] is connection]
            for gid in lost:
                rooms.pop(gid, None)
                restart_pending.discard(gid)
            for (gid, _), waiter in pending.items():
                if gid in lost:
                    waiter[1].set()
        for gid in lost:
            retire_room(gid)
            games.retire(gid, reason='original-host-disconnected')
        retire_connection(connection)
        return original_disconnect(connection)

    theater.dispatch = dispatch
    theater.disconnect = disconnect
