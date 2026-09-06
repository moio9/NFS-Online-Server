const std = @import("std");
const win = @import("shared").win;
const endpoint_routing = @import("shared").endpoint_routing;
const net = @import("shared").net_util;
const logger = @import("shared").logger;
const relay_wire = @import("shared").relay_wire;
const strings = @import("shared").strings;
const udp_trace = @import("shared").udp_trace;
const state = @import("state.zig");
const c = win.c;

const identity_marker = [_]u8{ 'U', '2', 'I', '1' };
const identity_header_size: usize = 14;

pub fn relayAddress() c.sockaddr_in {
    return state.race_addr;
}

fn traceWord(word: u32) bool {
    return word == 1 or word == 2 or word == 5 or word == 0x65 or word == 0x66;
}

fn logTraceAddress(
    comptime event: []const u8,
    socket: c.SOCKET,
    peer: *align(1) const c.sockaddr_in,
    word: ?u32,
    length: c_int,
) void {
    udp_trace.log("U2", state.udp_trace, traceWord, true, event, socket, peer, word, length);
}

pub fn tcpRedirect(original: *const c.sockaddr_in, output: *c.sockaddr_in) bool {
    const port = c.ntohs(original.sin_port);
    var endpoint: ?*const net.Endpoint = null;

    if (state.network_enabled and endpoint_routing.portMatches(port, 20921, state.bootstrap.port)) {
        endpoint = &state.bootstrap;
    } else {
        const network_lobby = state.network_enabled and endpoint_routing.portMatches(port, 20922, state.lobby.port);
        const lan_lobby = state.lan_enabled and (port == 9900 or port == state.lan.port);
        if (network_lobby or lan_lobby) {
            endpoint = if (network_lobby and lan_lobby)
                endpoint_routing.chooseOverlapping(original, &state.lobby, &state.lobby_addr, &state.lan, &state.lan_addr)
            else if (network_lobby)
                &state.lobby
            else
                &state.lan;
        }
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

pub fn udpConnectRedirect(socket: c.SOCKET, original: *const c.sockaddr_in, output: *c.sockaddr_in) bool {
    if (!state.network_enabled) return false;
    output.* = state.race_addr;
    if (state.race_addr.sin_family != c.AF_INET or state.race_addr.sin_port == 0) return false;
    logTraceAddress("connect-peer", socket, original, null, 0);
    logTraceAddress("connect-relay", socket, &state.race_addr, null, 0);
    return true;
}

pub fn normalizeUdpPeer(socket: c.SOCKET, peer: *c.sockaddr_in, peer_get: anytype) void {
    if (net.addressEquals(peer, &state.race_addr)) {
        var cached: c.sockaddr_in = undefined;
        if (peer_get(socket, &cached)) {
            peer.* = cached;
        }
    }
}

pub fn sendUdp(socket: c.SOCKET, data: [*]const u8, length: c_int, flags: c_int, peer: *const c.sockaddr_in, real_sendto: anytype) c_int {
    if (!state.network_enabled)
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    if (length <= 0 or state.race_addr.sin_family != c.AF_INET)
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    logTraceAddress("send", socket, peer, relay_wire.firstWord(data, length), length);
    const header_size: usize = if (state.race_identity_ip != 0) identity_header_size else relay_wire.header_size;
    const total: usize = @as(usize, @intCast(length)) + header_size;
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
    if (header_size == identity_header_size) {
        @memcpy(output[6..10], identity_marker[0..]);
        @memcpy(output[10..14], std.mem.asBytes(&state.race_identity_ip));
    }
    @memcpy(output[header_size..total], data[0..@intCast(length)]);
    const result = real_sendto(socket, output, @intCast(total), flags, @ptrCast(&state.race_addr), @sizeOf(c.sockaddr_in));
    return if (result >= @as(c_int, @intCast(header_size))) result - @as(c_int, @intCast(header_size)) else result;
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

    // In every viewer-specific U2 game-start record OP/ADDR slot zero is the
    // local player. Only accept the relay's CGNAT identity range so ordinary
    // browser rows containing a real LAN/public ADDR0 cannot replace it.
    var identity_value: [64]u8 = [_]u8{0} ** 64;
    if (net.copyField(data, "ADDR0=", identity_value[0..])) {
        const identity_ip = c.inet_addr(identity_value[0..].ptr);
        if (identity_ip != c.INADDR_NONE and identity_ip != 0) {
            const host_order = c.ntohl(identity_ip);
            const first: u8 = @truncate(host_order >> 24);
            const second: u8 = @truncate(host_order >> 16);
            if (first == 100 and second >= 64 and second <= 127) {
                if (state.race_identity_ip != identity_ip) {
                    state.race_identity_ip = identity_ip;
                    logger.line("U2 shared relay identity learned ADDR0={s}", .{strings.sliceZ(identity_value[0..])});
                }
            }
        }
    }

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
        logger.line("U2 advertised endpoints bootstrap={s}:{d} lobby={s}:{d} control={s}:{d} alias={s}:{d} race={s}:{d}", .{
            state.bootstrap.hostSlice(),     state.bootstrap.port,
            state.lobby.hostSlice(),         state.lobby.port,
            state.control.hostSlice(),       state.control.port,
            state.control_alias.hostSlice(), state.control_alias.port,
            state.race.hostSlice(),          state.race.port,
        });
    }
}

pub fn socketClosed(socket: c.SOCKET) void {
    if (!state.udp_trace or !net.isSocketType(socket, c.SOCK_DGRAM)) return;
    var local: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
    var length: c_int = @sizeOf(c.sockaddr_in);
    if (c.getsockname(socket, @ptrCast(&local), &length) != 0) return;
    logTraceAddress("close", socket, &local, null, 0);
}
