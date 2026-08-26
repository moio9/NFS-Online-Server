const std = @import("std");

pub fn lenZ(s: [*:0]const u8) usize {
    return std.mem.len(s);
}

pub fn lower(c: u8) u8 {
    return if (c >= 'A' and c <= 'Z') c + ('a' - 'A') else c;
}

pub fn eqlIgnoreCase(a: []const u8, b: []const u8) bool {
    if (a.len != b.len) return false;
    for (a, b) |ca, cb| if (lower(ca) != lower(cb)) return false;
    return true;
}

pub fn eqlZIgnoreCase(a: [*:0]const u8, b: [*:0]const u8) bool {
    return eqlIgnoreCase(std.mem.span(a), std.mem.span(b));
}

pub fn trim(value: []const u8) []const u8 {
    return std.mem.trim(u8, value, " \t\r\n");
}

pub fn parseBool(value: []const u8) ?bool {
    if (eqlIgnoreCase(value, "1") or eqlIgnoreCase(value, "true") or
        eqlIgnoreCase(value, "yes") or eqlIgnoreCase(value, "on")) return true;
    if (eqlIgnoreCase(value, "0") or eqlIgnoreCase(value, "false") or
        eqlIgnoreCase(value, "no") or eqlIgnoreCase(value, "off")) return false;
    return null;
}

pub fn parseU16(value: []const u8) ?u16 {
    const n = std.fmt.parseUnsigned(u32, value, 0) catch return null;
    if (n == 0 or n > 65535) return null;
    return @intCast(n);
}

pub fn parseU32(value: []const u8) ?u32 {
    return std.fmt.parseUnsigned(u32, value, 0) catch null;
}

pub fn copyZ(dest: []u8, source: []const u8) void {
    if (dest.len == 0) return;
    const n = @min(dest.len - 1, source.len);
    @memcpy(dest[0..n], source[0..n]);
    dest[n] = 0;
    if (n + 1 < dest.len) @memset(dest[n + 1 ..], 0);
}

pub fn sliceZ(buf: []const u8) []const u8 {
    const end = std.mem.findScalar(u8, buf, 0) orelse buf.len;
    return buf[0..end];
}

pub fn basenameDir(path: []const u8) []const u8 {
    var last: usize = 0;
    for (path, 0..) |ch, i| {
        if (ch == '\\' or ch == '/') last = i + 1;
    }
    return path[0..last];
}

pub fn indexOfIgnoreCase(haystack: []const u8, needle: []const u8) ?usize {
    if (needle.len == 0 or needle.len > haystack.len) return null;
    var i: usize = 0;
    while (i + needle.len <= haystack.len) : (i += 1) {
        if (eqlIgnoreCase(haystack[i .. i + needle.len], needle)) return i;
    }
    return null;
}
