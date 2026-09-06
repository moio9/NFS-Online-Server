const std = @import("std");
const win = @import("win.zig");
const net = @import("net_util.zig");
const c = win.c;

pub fn portMatches(port: u16, built_in: u16, configured: u16) bool {
    return port == built_in or port == configured;
}

pub fn chooseOverlapping(
    original: *const c.sockaddr_in,
    network_endpoint: *const net.Endpoint,
    network_address: *const c.sockaddr_in,
    lan_endpoint: *const net.Endpoint,
    lan_address: *const c.sockaddr_in,
) *const net.Endpoint {
    const network_match = network_address.sin_family == c.AF_INET and
        net.ipv4Address(original) == net.ipv4Address(network_address);
    const lan_match = lan_address.sin_family == c.AF_INET and
        net.ipv4Address(original) == net.ipv4Address(lan_address);
    return if (lan_match and !network_match) lan_endpoint else network_endpoint;
}

pub fn isTcpPeer(
    socket: c.SOCKET,
    enabled: bool,
    built_in_port: u16,
    endpoint: *const net.Endpoint,
    resolved: *const c.sockaddr_in,
) bool {
    if (!enabled) return false;
    var peer: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
    var length: c_int = @sizeOf(c.sockaddr_in);
    if (c.getpeername(socket, @ptrCast(&peer), &length) != 0 or peer.sin_family != c.AF_INET) return false;
    const port = c.ntohs(peer.sin_port);
    if (port != built_in_port and port != endpoint.port) return false;
    return resolved.sin_family != c.AF_INET or net.ipv4Address(&peer) == net.ipv4Address(resolved);
}
