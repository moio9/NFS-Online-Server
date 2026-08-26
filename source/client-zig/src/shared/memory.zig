const std = @import("std");
const win = @import("win.zig");
const c = win.c;

pub fn protectWrite(destination: *anyopaque, source: []const u8) bool {
    if (source.len == 0) return false;
    var old: c.DWORD = 0;
    if (c.VirtualProtect(destination, source.len, c.PAGE_EXECUTE_READWRITE, &old) == 0) return false;
    const dst: [*]u8 = @ptrCast(destination);
    @memcpy(dst[0..source.len], source);
    var ignored: c.DWORD = 0;
    _ = c.VirtualProtect(destination, source.len, old, &ignored);
    _ = c.FlushInstructionCache(c.GetCurrentProcess(), destination, source.len);
    return true;
}

pub fn writeValue(comptime T: type, destination: *anyopaque, value: T) bool {
    var copy = value;
    return protectWrite(destination, std.mem.asBytes(&copy));
}

pub fn fillNop(destination: *anyopaque, count: usize) bool {
    if (count == 0 or count > 64) return false;
    const bytes: [64]u8 = [_]u8{0x90} ** 64;
    return protectWrite(destination, bytes[0..count]);
}

pub fn patchRelCall(call_site: *anyopaque, replacement: *const anyopaque) bool {
    const site = @intFromPtr(call_site);
    const target = @intFromPtr(replacement);
    const delta = @as(i64, @intCast(target)) - @as(i64, @intCast(site + 5));
    if (delta < std.math.minInt(i32) or delta > std.math.maxInt(i32)) return false;
    const rel: i32 = @intCast(delta);
    var bytes: [5]u8 = undefined;
    bytes[0] = 0xE8;
    @as(*align(1) i32, @ptrCast(bytes[1..].ptr)).* = rel;
    return protectWrite(call_site, bytes[0..]);
}

pub fn readU32(address: usize) u32 {
    return @as(*align(1) const volatile u32, @ptrFromInt(address)).*;
}

pub fn writeU32(address: usize, value: u32) void {
    @as(*align(1) volatile u32, @ptrFromInt(address)).* = value;
}
