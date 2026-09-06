const std = @import("std");
const win = @import("win.zig");
const net = @import("net_util.zig");
const c = win.c;

pub const header_size: usize = 6;

pub fn firstWord(data: [*]const u8, length: c_int) ?u32 {
    if (length < 4) return null;
    return @as(u32, data[0]) |
        (@as(u32, data[1]) << 8) |
        (@as(u32, data[2]) << 16) |
        (@as(u32, data[3]) << 24);
}

pub fn writePeerHeader(output: [*]u8, peer: *align(1) const c.sockaddr_in) void {
    @memcpy(output[0..2], @as([*]const u8, @ptrCast(&peer.sin_port))[0..2]);
    const peer_ip = net.ipv4Address(peer);
    @memcpy(output[2..header_size], std.mem.asBytes(&peer_ip));
}

fn looksWrapped(data: [*]const u8, length: c_int) bool {
    if (length <= header_size) return false;
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

pub fn unwrap(
    data: [*]u8,
    length: c_int,
    source_raw: *c.sockaddr,
    source_length: *c_int,
    relay: *const c.sockaddr_in,
) ?c_int {
    if (length <= header_size or source_length.* < @sizeOf(c.sockaddr_in)) return null;
    const source: *align(1) c.sockaddr_in = @ptrCast(source_raw);
    if (net.ipv4Address(source) != net.ipv4Address(relay)) return null;
    if (!looksWrapped(data, length)) return null;

    var decoded: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
    decoded.sin_family = c.AF_INET;
    @memcpy(@as([*]u8, @ptrCast(&decoded.sin_port))[0..2], data[0..2]);
    var decoded_ip: u32 = 0;
    @memcpy(std.mem.asBytes(&decoded_ip), data[2..header_size]);
    net.setIpv4Address(&decoded, decoded_ip);

    const payload: usize = @intCast(length - header_size);
    std.mem.copyForwards(u8, data[0..payload], data[header_size .. header_size + payload]);
    source.* = decoded;
    source_length.* = @sizeOf(c.sockaddr_in);
    return @intCast(payload);
}
