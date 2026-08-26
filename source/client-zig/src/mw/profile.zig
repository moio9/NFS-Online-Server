const std = @import("std");
const win = @import("shared").win;
const net = @import("shared").net_util;
const logger = @import("shared").logger;
const state = @import("state.zig");
const c = win.c;

pub fn relayAddress() c.sockaddr_in {
    return state.race_addr;
}

fn firstWord(data: [*]const u8, length: c_int) ?u32 {
    if (length < 4) return null;
    return @as(u32, data[0]) |
        (@as(u32, data[1]) << 8) |
        (@as(u32, data[2]) << 16) |
        (@as(u32, data[3]) << 24);
}

fn traceWord(word: u32) bool {
    return word == 1 or word == 2 or word == 5;
}

fn localAddress(socket: c.SOCKET) c.sockaddr_in {
    var local: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
    var length: c_int = @sizeOf(c.sockaddr_in);
    if (c.getsockname(socket, @ptrCast(&local), &length) != 0) {
        return std.mem.zeroes(c.sockaddr_in);
    }
    return local;
}

fn logTraceAddress(
    comptime event: []const u8,
    socket: c.SOCKET,
    peer: *align(1) const c.sockaddr_in,
    word: ?u32,
    length: c_int,
) void {
    if (!state.udp_trace) return;
    const value = word orelse return;
    if (!traceWord(value)) return;
    const local = localAddress(socket);
    const local_host = c.ntohl(net.ipv4Address(&local));
    const peer_host = c.ntohl(net.ipv4Address(peer));
    logger.line(
        "MW UDP TRACE {s} socket=0x{x} local={d}.{d}.{d}.{d}:{d} peer={d}.{d}.{d}.{d}:{d} w0=0x{x:0>8} bytes={d}",
        .{
            event,
            socket,
            @as(u8, @truncate(local_host >> 24)),
            @as(u8, @truncate(local_host >> 16)),
            @as(u8, @truncate(local_host >> 8)),
            @as(u8, @truncate(local_host)),
            c.ntohs(local.sin_port),
            @as(u8, @truncate(peer_host >> 24)),
            @as(u8, @truncate(peer_host >> 16)),
            @as(u8, @truncate(peer_host >> 8)),
            @as(u8, @truncate(peer_host)),
            c.ntohs(peer.sin_port),
            value,
            length,
        },
    );
}

fn relayIsLoopback() bool {
    if (state.race_addr.sin_family != c.AF_INET) return false;
    const host = c.ntohl(net.ipv4Address(&state.race_addr));
    return (host & 0xff000000) == 0x7f000000;
}

pub fn udpBindRedirect(original: *const c.sockaddr_in, output: *c.sockaddr_in) bool {
    if (!state.network_enabled or original.sin_family != c.AF_INET) return false;
    if (c.ntohs(original.sin_port) != 3658) return false;

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
            "MW local race bind channel={d} relay_port={d} host=127.0.0.{d} port=3658",
            .{ race_channel, state.race.port, 2 + race_channel },
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

fn portMatches(port: u16, built_in: u16, configured: u16) bool {
    return port == built_in or port == configured;
}

fn addressMatches(original: *const c.sockaddr_in, resolved: *const c.sockaddr_in) bool {
    return resolved.sin_family == c.AF_INET and net.ipv4Address(original) == net.ipv4Address(resolved);
}

fn chooseOverlapping(
    original: *const c.sockaddr_in,
    network_endpoint: *const net.Endpoint,
    network_address: *const c.sockaddr_in,
    lan_endpoint: *const net.Endpoint,
    lan_address: *const c.sockaddr_in,
) *const net.Endpoint {
    const network_match = addressMatches(original, network_address);
    const lan_match = addressMatches(original, lan_address);
    return if (lan_match and !network_match) lan_endpoint else network_endpoint;
}

pub fn tcpRedirect(original: *const c.sockaddr_in, output: *c.sockaddr_in) bool {
    const port = c.ntohs(original.sin_port);
    var endpoint: ?*const net.Endpoint = null;

    if (state.network_enabled and portMatches(port, 30921, state.bootstrap.port)) {
        endpoint = &state.bootstrap;
    } else if (state.network_enabled and portMatches(port, 30920, state.lobby.port)) {
        endpoint = &state.lobby;
    } else if (state.lan_enabled and (port == 9900 or port == state.lan.port)) {
        endpoint = &state.lan;
    }

    if (endpoint == null) {
        const network_control = state.network_enabled and portMatches(port, 20923, state.control.port);
        const lan_control = state.lan_enabled and portMatches(port, 20923, state.lan_control.port);
        if (network_control or lan_control) {
            endpoint = if (network_control and lan_control)
                chooseOverlapping(original, &state.control, &state.control_addr, &state.lan_control, &state.lan_control_addr)
            else if (network_control)
                &state.control
            else
                &state.lan_control;
        }
    }

    if (endpoint == null) {
        const network_alias = state.network_enabled and portMatches(port, 13505, state.control_alias.port);
        const lan_alias = state.lan_enabled and portMatches(port, 13505, state.lan_control_alias.port);
        if (network_alias or lan_alias) {
            endpoint = if (network_alias and lan_alias)
                chooseOverlapping(original, &state.control_alias, &state.control_alias_addr, &state.lan_control_alias, &state.lan_control_alias_addr)
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
    if (isBroadcastOrMulticast(original) or c.ntohs(original.sin_port) == state.discovery.port) return false;
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

pub fn beforeUdpSend(socket: c.SOCKET, data: [*]const u8, length: c_int, flags: c_int, peer: *const c.sockaddr_in, real_sendto: anytype) ?c_int {
    if (!state.lan_enabled or !state.discovery_mirror or length <= 0 or state.discovery_addr.sin_family != c.AF_INET or !isBroadcastOrMulticast(peer)) return null;
    var target = state.discovery_addr;
    if (target.sin_port == 0) {
        target.sin_port = peer.sin_port;
    }
    _ = real_sendto(socket, data, length, flags, @ptrCast(&target), @sizeOf(c.sockaddr_in));
    return null;
}

pub fn sendUdp(socket: c.SOCKET, data: [*]const u8, length: c_int, flags: c_int, peer: *const c.sockaddr_in, real_sendto: anytype) c_int {
    if (!state.network_enabled)
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    if (isBroadcastOrMulticast(peer) or looksLikeLanDiscovery(data, length) or state.race_addr.sin_family != c.AF_INET)
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    if (net.addressEquals(peer, &state.race_addr))
        return real_sendto(socket, data, length, flags, @ptrCast(peer), @sizeOf(c.sockaddr_in));
    logTraceAddress("send", socket, peer, firstWord(data, length), length);
    const total: usize = @as(usize, @intCast(length)) + 6;
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
    @memcpy(output[0..2], @as([*]const u8, @ptrCast(&peer.sin_port))[0..2]);
    const peer_ip = net.ipv4Address(peer);
    @memcpy(output[2..6], std.mem.asBytes(&peer_ip));
    @memcpy(output[6..total], data[0..@intCast(length)]);
    const result = real_sendto(socket, output, @intCast(total), flags, @ptrCast(&state.race_addr), @sizeOf(c.sockaddr_in));
    return if (result >= 6) result - 6 else result;
}

fn looksWrapped(data: [*]const u8, length: c_int) bool {
    if (length <= 6) return false;
    const port: u16 = (@as(u16, data[0]) << 8) | @as(u16, data[1]);
    if (port == 0 or data[2] == 0 or data[2] >= 224) return false;
    if ((data[0] >> 4) == 4 and length >= 20) {
        const ihl: u16 = @as(u16, data[0] & 0x0f) * 4;
        const total: u16 = (@as(u16, data[2]) << 8) | @as(u16, data[3]);
        const packet_length: u16 = @intCast(@min(length, std.math.maxInt(u16)));
        if (ihl >= 20 and total >= ihl and total <= packet_length) return false;
    }
    return true;
}

pub fn recvUdp(socket: c.SOCKET, data: [*]u8, length: c_int, source_raw: *c.sockaddr, source_length: *c_int, _: anytype) c_int {
    if (!state.network_enabled) return length;
    if (length <= 6 or source_length.* < @sizeOf(c.sockaddr_in)) return length;
    const source: *align(1) c.sockaddr_in = @ptrCast(source_raw);
    if (net.ipv4Address(source) != net.ipv4Address(&state.race_addr)) return length;
    if (!looksWrapped(data, length)) return length;
    var decoded: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
    decoded.sin_family = c.AF_INET;
    @memcpy(@as([*]u8, @ptrCast(&decoded.sin_port))[0..2], data[0..2]);
    var decoded_ip: u32 = 0;
    @memcpy(std.mem.asBytes(&decoded_ip), data[2..6]);
    net.setIpv4Address(&decoded, decoded_ip);
    const payload: usize = @intCast(length - 6);
    std.mem.copyForwards(u8, data[0..payload], data[6 .. 6 + payload]);
    source.* = decoded;
    source_length.* = @sizeOf(c.sockaddr_in);
    logTraceAddress("recv", socket, &decoded, firstWord(data, @intCast(payload)), @intCast(payload));
    return @intCast(payload);
}

fn isLanTcpPeer(socket: c.SOCKET) bool {
    if (!state.lan_enabled) return false;
    var peer: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
    var length: c_int = @sizeOf(c.sockaddr_in);
    if (c.getpeername(socket, @ptrCast(&peer), &length) != 0 or peer.sin_family != c.AF_INET) return false;
    const port = c.ntohs(peer.sin_port);
    if (port != 9900 and port != state.lan.port) return false;
    return state.lan_addr.sin_family != c.AF_INET or net.ipv4Address(&peer) == net.ipv4Address(&state.lan_addr);
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
