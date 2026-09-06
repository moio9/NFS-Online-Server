const std = @import("std");
const win = @import("win.zig");
const net = @import("net_util.zig");
const logger = @import("logger.zig");
const c = win.c;

fn localAddress(socket: c.SOCKET) c.sockaddr_in {
    var local: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
    var length: c_int = @sizeOf(c.sockaddr_in);
    if (c.getsockname(socket, @ptrCast(&local), &length) != 0) {
        return std.mem.zeroes(c.sockaddr_in);
    }
    return local;
}

pub fn log(
    comptime game: []const u8,
    enabled: bool,
    comptime acceptsWord: fn (u32) bool,
    comptime allowAddressOnly: bool,
    comptime event: []const u8,
    socket: c.SOCKET,
    peer: *align(1) const c.sockaddr_in,
    word: ?u32,
    length: c_int,
) void {
    if (!enabled) return;
    if (word) |value| {
        if (!acceptsWord(value)) return;
    } else if (!allowAddressOnly) {
        return;
    }

    const local = localAddress(socket);
    const local_host = c.ntohl(net.ipv4Address(&local));
    const peer_host = c.ntohl(net.ipv4Address(peer));
    if (word) |value| {
        logger.line(
            game ++ " UDP TRACE {s} socket=0x{x} local={d}.{d}.{d}.{d}:{d} peer={d}.{d}.{d}.{d}:{d} w0=0x{x:0>8} bytes={d}",
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
        return;
    }

    logger.line(
        game ++ " UDP TRACE {s} socket=0x{x} local={d}.{d}.{d}.{d}:{d} peer={d}.{d}.{d}.{d}:{d}",
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
        },
    );
}
