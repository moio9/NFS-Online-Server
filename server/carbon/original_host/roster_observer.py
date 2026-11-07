"""Observe a clear-mode self-join and its subsequent client acknowledgement.

This observes packets without altering them; it is not an authentication
mechanism and a missing signal must never be treated as proof of failure.
"""
from carbon.transport.commudp import CommUDPActive, parse_channel_one
from carbon.transport.prototunnel import TunnelPacket

_MASK = 0x0fffffff
_HALF = 0x08000000


def _clear_packets(data):
    """Conservatively parse one unambiguous clear ProtoTunnel datagram."""
    if not 4 <= len(data) <= 1000:
        return ()
    candidates = []
    for count in range(1, min(9, (len(data) - 2) // 2) + 1):
        headers = []
        for i in range(count):
            first, second = data[2 + 2*i:4 + 2*i]
            length, channel = (first << 4) | (second >> 4), second & 15
            if not 0 < length <= 0x3e3 or channel not in (0, 1, 2, 3, 7):
                break
            headers.append((channel, length))
        if len(headers) != count or 2 + count*2 + sum(n for _, n in headers) != len(data):
            continue
        identities = [n for channel, n in headers if channel == 7]
        if len(identities) > 1 or (identities and identities[0] != 6) or count - len(identities) > 8:
            continue
        position = 2 + count*2
        packets = []
        for channel, length in headers:
            packets.append(TunnelPacket(channel, data[position:position+length]))
            position += length
        candidates.append(tuple(packets))
    return candidates[0] if len(candidates) == 1 else ()


class RosterAckObserver:
    def __init__(self, player_id):
        if not 0 < player_id <= 0xffff:
            raise ValueError('invalid player id')
        self.player_id = player_id
        self.join_sequence = None
        self.confirmed = False

    @staticmethod
    def _active(data):
        return tuple(item for packet in _clear_packets(data)
                     if isinstance((item := parse_channel_one(packet)), CommUDPActive))

    def from_host(self, data):
        if self.confirmed:
            return
        for item in self._active(data):
            message = item.game_manager
            if (message is not None and message.message_type == 5 and len(message.body) >= 4
                    and int.from_bytes(message.body[2:4], 'big') == self.player_id):
                self.join_sequence = item.sequence & _MASK

    def from_guest(self, data):
        if self.confirmed or self.join_sequence is None:
            return False
        for item in self._active(data):
            # 28-bit serial-number comparison, including sequence wraparound.
            difference = ((item.acknowledgement & _MASK) - self.join_sequence) & _MASK
            if difference < _HALF:
                self.confirmed = True
                return True
        return False
