const win = @import("win.zig");
const strings = @import("strings.zig");
const c = win.c;

pub const Endpoint = struct {
    host: [128]u8 = [_]u8{0} ** 128,
    port: u16 = 0,

    pub fn init(host: []const u8, port: u16) Endpoint {
        var result = Endpoint{ .port = port };
        strings.copyZ(result.host[0..], host);
        return result;
    }

    pub fn hostSlice(self: *const Endpoint) []const u8 {
        return strings.sliceZ(self.host[0..]);
    }

    pub fn setHost(self: *Endpoint, host: []const u8) void {
        if (host.len != 0) strings.copyZ(self.host[0..], host);
    }
};

fn addressSlot(address: *align(1) c.sockaddr_in) *align(1) u32 {
    return @ptrFromInt(@intFromPtr(address) + 4);
}

fn constAddressSlot(address: *align(1) const c.sockaddr_in) *align(1) const u32 {
    return @ptrFromInt(@intFromPtr(address) + 4);
}

pub fn resolve(endpoint: *const Endpoint, out: *c.sockaddr_in) bool {
    const bytes: [*]u8 = @ptrCast(out);
    @memset(bytes[0..@sizeOf(c.sockaddr_in)], 0);
    out.sin_family = c.AF_INET;
    out.sin_port = c.htons(endpoint.port);

    var host_z: [129]u8 = [_]u8{0} ** 129;
    strings.copyZ(host_z[0..], endpoint.hostSlice());
    const direct = c.inet_addr(host_z[0..].ptr);
    if (direct != c.INADDR_NONE) {
        addressSlot(out).* = direct;
        return true;
    }

    const entry = c.gethostbyname(host_z[0..].ptr) orelse return false;
    if (entry.*.h_addr_list == null or entry.*.h_addr_list[0] == null) return false;
    const source: [*]const u8 = @ptrCast(entry.*.h_addr_list[0]);
    const target: [*]u8 = @ptrCast(addressSlot(out));
    @memcpy(target[0..4], source[0..4]);
    return true;
}

pub fn isSocketType(socket: c.SOCKET, wanted: c_int) bool {
    var kind: c_int = 0;
    var length: c_int = @sizeOf(c_int);
    return c.getsockopt(socket, c.SOL_SOCKET, c.SO_TYPE, @ptrCast(&kind), &length) == 0 and kind == wanted;
}

pub fn ipv4Address(address: *align(1) const c.sockaddr_in) u32 {
    return constAddressSlot(address).*;
}

pub fn setIpv4Address(address: *align(1) c.sockaddr_in, value: u32) void {
    addressSlot(address).* = value;
}

pub fn addressEquals(a: *align(1) const c.sockaddr_in, b: *align(1) const c.sockaddr_in) bool {
    return a.sin_family == b.sin_family and a.sin_port == b.sin_port and ipv4Address(a) == ipv4Address(b);
}

pub fn copyField(data: []const u8, key: []const u8, output: []u8) bool {
    const start = strings.indexOfIgnoreCase(data, key) orelse return false;
    var i = start + key.len;
    var n: usize = 0;
    while (i < data.len and n + 1 < output.len) : (i += 1) {
        const ch = data[i];
        if (ch == '\r' or ch == '\n' or ch == '\t' or ch == 0 or ch == '|' or ch == '&') break;
        output[n] = ch;
        n += 1;
    }
    if (output.len != 0) {
        output[n] = 0;
    }
    return n != 0;
}

pub fn applyAdvertised(
    data: []const u8,
    endpoint: *Endpoint,
    host_keys: []const []const u8,
    port_keys: []const []const u8,
) bool {
    var changed = false;
    var value: [128]u8 = [_]u8{0} ** 128;
    for (host_keys) |key| {
        if (copyField(data, key, value[0..])) {
            endpoint.setHost(strings.sliceZ(value[0..]));
            changed = true;
            break;
        }
    }
    var port_value: [32]u8 = [_]u8{0} ** 32;
    for (port_keys) |key| {
        if (copyField(data, key, port_value[0..])) {
            if (strings.parseU16(strings.sliceZ(port_value[0..]))) |port| {
                endpoint.port = port;
                changed = true;
            }
            break;
        }
    }
    return changed;
}
