const std = @import("std");
const win = @import("shared").win;
const endpoint_routing = @import("shared").endpoint_routing;
const net = @import("shared").net_util;
const logger = @import("shared").logger;
const relay_wire = @import("shared").relay_wire;
const udp_trace = @import("shared").udp_trace;
const state = @import("state.zig");
const c = win.c;
const game_udp_port: u16 = 3658;

pub fn relayAddress() c.sockaddr_in {
    return state.race_addr;
}

fn traceWord(word: u32) bool {
    return word == 1 or word == 2 or word == 5;
}

fn logTraceAddress(
    comptime event: []const u8,
    socket: c.SOCKET,
    peer: *align(1) const c.sockaddr_in,
    word: ?u32,
    length: c_int,
) void {
    udp_trace.log("MW", state.udp_trace, traceWord, false, event, socket, peer, word, length);
}

fn relayIsLoopback() bool {
    if (state.race_addr.sin_family != c.AF_INET) return false;
    const host = c.ntohl(net.ipv4Address(&state.race_addr));
    return (host & 0xff000000) == 0x7f000000;
}

pub fn udpBindRedirect(original: *const c.sockaddr_in, output: *c.sockaddr_in) bool {
    if (!state.network_enabled or original.sin_family != c.AF_INET) return false;
    if (c.ntohs(original.sin_port) != game_udp_port) return false;

    const relay_base_port: u16 = 20000;
    const relay_channel_count: u16 = 6;
    if (state.race.port < relay_base_port or state.race.port >= relay_base_port + relay_channel_count) return false;
    const race_channel: u32 = state.race.port - relay_base_port;
    const original_host = c.ntohl(net.ipv4Address(original));
    if (original_host != 0 and (original_host & 0xff000000) != 0x7f000000) return false;

    output.* = original.*;
    if (relayIsLoopback()) {
        // Local clients need one address per viewer channel while preserving
        // the stock game port. This path is valid only when the relay itself
        // is loopback; a loopback-bound socket cannot send to a LAN/WAN relay.
        const local_host: u32 = 0x7f000002 + race_channel;
        net.setIpv4Address(output, std.mem.nativeToBig(u32, local_host));
        logger.line(
            "MW local race bind channel={d} relay_port={d} host=127.0.0.{d} port={d}",
            .{ race_channel, state.race.port, 2 + race_channel, game_udp_port },
        );
    } else {
        // For LAN/WAN relays use a wildcard ephemeral source port. A 127/8
        // bind here makes Winsock/Wine reject every datagram whose destination
        // is outside 127/8. The viewer-specific relay ports 20000-20005
        // already identify the participant server-side.
        net.setIpv4Address(output, 0);
        output.sin_port = 0;
        logger.line(
            "MW remote race bind channel={d} relay={s}:{d} host=0.0.0.0 port=ephemeral",
            .{ race_channel, state.race.hostSlice(), state.race.port },
        );
    }
    return true;
}

pub fn tcpRedirect(original: *const c.sockaddr_in, output: *c.sockaddr_in) bool {
    const port = c.ntohs(original.sin_port);
    var endpoint: ?*const net.Endpoint = null;

    if (state.network_enabled and endpoint_routing.portMatches(port, 30921, state.bootstrap.port)) {
        endpoint = &state.bootstrap;
    } else if (state.network_enabled and endpoint_routing.portMatches(port, 30920, state.lobby.port)) {
        endpoint = &state.lobby;
    } else if (state.lan_enabled and (port == 9900 or port == state.lan.port)) {
        endpoint = &state.lan;
    }

    if (endpoint == null) {
        const network_control = state.network_enabled and endpoint_routing.portMatches(port, 20923, state.control.port);
        const lan_control = state.lan_enabled and endpoint_routing.portMatches(port, 20923, state.lan_control.port);
        if (network_control or lan_control) {
            endpoint = if (network_control and lan_control)
                endpoint_routing.chooseOverlapping(original, &state.control, &state.control_addr, &state.lan_control, &state.lan_control_addr)
            else if (network_control)
                &state.control
            else
                &state.lan_control;
        }
    }

    if (endpoint == null) {
        const network_alias = state.network_enabled and endpoint_routing.portMatches(port, 13505, state.control_alias.port);
        const lan_alias = state.lan_enabled and endpoint_routing.portMatches(port, 13505, state.lan_control_alias.port);
        if (network_alias or lan_alias) {
            endpoint = if (network_alias and lan_alias)
                endpoint_routing.chooseOverlapping(original, &state.control_alias, &state.control_alias_addr, &state.lan_control_alias, &state.lan_control_alias_addr)
            else if (network_alias)
                &state.control_alias
            else
                &state.lan_control_alias;
        }
    }

    const selected = endpoint orelse return false;
    if (!net.resolve(selected, output)) return false;
    return !net.addressEquals(original, output);
}

pub fn udpConnectRedirect(_: c.SOCKET, original: *const c.sockaddr_in, output: *c.sockaddr_in) bool {
    if (!state.network_enabled) return false;
    if (isBroadcastOrMulticast(original) or c.ntohs(original.sin_port) == game_udp_port) return false;
    output.* = state.race_addr;
    return state.race_addr.sin_family == c.AF_INET and state.race_addr.sin_port != 0;
}

pub fn normalizeUdpPeer(socket: c.SOCKET, peer: *c.sockaddr_in, peer_get: anytype) void {
    if (net.addressEquals(peer, &state.race_addr)) {
        var cached: c.sockaddr_in = undefined;
        if (peer_get(socket, &cached)) {
            peer.* = cached;
        }
    }
}

fn isBroadcastOrMulticast(peer: *const c.sockaddr_in) bool {
    const raw = net.ipv4Address(peer);
    const host = c.ntohl(raw);
    return raw == c.INADDR_BROADCAST or host == 0xffffffff or (host >= 0xe0000000 and host <= 0xefffffff);
}

fn looksLikeLanDiscovery(data: [*]const u8, length: c_int) bool {
    return length >= 3 and data[0] == 'g' and data[1] == 'E' and data[2] == 'A';
}

pub fn sendUdp(socket: c.SOCKET, data: [*]const u8, length: c_int, flags: c_int, peer: *const c.sockaddr_in, real_sendto: anytype) c_int {
    if (!state.network_enabled)
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    if (isBroadcastOrMulticast(peer) or looksLikeLanDiscovery(data, length) or state.race_addr.sin_family != c.AF_INET)
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    if (net.addressEquals(peer, &state.race_addr))
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    logTraceAddress("send", socket, peer, relay_wire.firstWord(data, length), length);
    const total: usize = @as(usize, @intCast(length)) + relay_wire.header_size;
    var stack: [2048]u8 = undefined;
    var allocated: ?*anyopaque = null;
    const output: [*]u8 = if (total <= stack.len) stack[0..].ptr else blk: {
        allocated = c.HeapAlloc(c.GetProcessHeap(), 0, total);
        break :blk @ptrCast(allocated orelse return c.SOCKET_ERROR);
    };
    defer {
        if (allocated) |mem| {
            _ = c.HeapFree(c.GetProcessHeap(), 0, mem);
        }
    }
    relay_wire.writePeerHeader(output, peer);
    @memcpy(output[relay_wire.header_size..total], data[0..@intCast(length)]);
    const result = real_sendto(socket, output, @intCast(total), flags, @ptrCast(&state.race_addr), @sizeOf(c.sockaddr_in));
    return if (result >= relay_wire.header_size) result - relay_wire.header_size else result;
}

pub fn recvUdp(socket: c.SOCKET, data: [*]u8, length: c_int, source_raw: *c.sockaddr, source_length: *c_int, _: anytype) c_int {
    if (!state.network_enabled) return length;
    const payload = relay_wire.unwrap(data, length, source_raw, source_length, &state.race_addr) orelse return length;
    const source: *align(1) c.sockaddr_in = @ptrCast(source_raw);
    logTraceAddress("recv", socket, source, relay_wire.firstWord(data, payload), payload);
    return payload;
}

fn isLanTcpPeer(socket: c.SOCKET) bool {
    return endpoint_routing.isTcpPeer(socket, state.lan_enabled, 9900, &state.lan, &state.lan_addr);
}

pub fn tcpData(socket: c.SOCKET, data: []u8) void {
    if (!state.network_enabled) return;

    const race_hosts = [_][]const u8{ "UDPHOST=", "RLYHOST=", "RACEHOST=", "RACE_HOST=" };
    const race_ports = [_][]const u8{ "UDPPORT=", "RLYPORT=", "RACEPORT=", "RACE_PORT=" };
    var changed = net.applyAdvertised(data, &state.race, &race_hosts, &race_ports);

    if (!isLanTcpPeer(socket)) {
        const bootstrap_hosts = [_][]const u8{ "BOOTSTRAPHOST=", "BOOTSTRAP_HOST=", "ONLINEHOST=", "ONLINE_HOST=" };
        const bootstrap_ports = [_][]const u8{ "BOOTSTRAPPORT=", "BOOTSTRAP_PORT=", "ONLINEPORT=", "ONLINE_PORT=" };
        const lobby_hosts = [_][]const u8{ "LOBBYHOST=", "LOBBY_HOST=", "LOBBYTCPHOST=" };
        const lobby_ports = [_][]const u8{ "LOBBYTCP=", "LOBBYPORT=", "LOBBY_PORT=", "LOBBY_TCP_PORT=" };
        const control_hosts = [_][]const u8{ "CONTROLHOST=", "CONTROL_HOST=", "BUDDY_SERVER=" };
        const control_ports = [_][]const u8{ "CONTROLPORT=", "CONTROL_PORT=", "BUDDY_PORT=" };
        const alias_hosts = [_][]const u8{ "CONTROLALIASHOST=", "CONTROL_ALIAS_HOST=", "CONTROLALIAS_HOST=", "BUDDY_ALIAS_SERVER=" };
        const alias_ports = [_][]const u8{ "CONTROLALIASPORT=", "CONTROL_ALIAS_PORT=", "CONTROLALIAS_PORT=", "BUDDY_ALIAS_PORT=" };

        changed = net.applyAdvertised(data, &state.bootstrap, &bootstrap_hosts, &bootstrap_ports) or changed;
        changed = net.applyAdvertised(data, &state.lobby, &lobby_hosts, &lobby_ports) or changed;
        changed = net.applyAdvertised(data, &state.control, &control_hosts, &control_ports) or changed;
        changed = net.applyAdvertised(data, &state.control_alias, &alias_hosts, &alias_ports) or changed;
    }

    if (changed) {
        state.refreshAddresses();
        logger.line("MW advertised endpoints bootstrap={s}:{d} lobby={s}:{d} control={s}:{d} alias={s}:{d} race={s}:{d}", .{
            state.bootstrap.hostSlice(),     state.bootstrap.port,
            state.lobby.hostSlice(),         state.lobby.port,
            state.control.hostSlice(),       state.control.port,
            state.control_alias.hostSlice(), state.control_alias.port,
            state.race.hostSlice(),          state.race.port,
        });
    }
}

pub fn socketClosed(_: c.SOCKET) void {}
