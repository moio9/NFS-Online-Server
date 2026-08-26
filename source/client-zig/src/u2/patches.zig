const std = @import("std");
const win = @import("shared").win;
const pe = @import("shared").pe;
const memory = @import("shared").memory;
const state = @import("state.zig");
const logger = @import("shared").logger;
const c = win.c;

var host_buffer: [0x3c]u8 = [_]u8{0} ** 0x3c;

pub fn install() bool {
    if (!state.patches_enabled) return true;
    const module = c.GetModuleHandleA(null) orelse return false;
    const base = win.moduleBase(module) orelse return false;
    const size = pe.imageSize(module) orelse return false;
    const image = base[0..size];
    const ea = pe.findSignature(image, "68 ? ? ? ? 50 E8 ? ? ? ? 8B F8 83 C4 ? 85 FF 7D") orelse return false;
    const multi = pe.findSignature(image, "6A ? E8 ? ? ? ? 8B 44 24 ? 8B 4C 24 ? 50") orelse
        pe.findSignature(image, "90 90 90 90 90 90 90 8B 44 24 ? 8B 4C 24 ? 50") orelse return false;
    const ssl = pe.findSignature(image, "7D ? C7 86 ? ? ? ? ? ? ? ? EB ? 03 7C 24") orelse
        pe.findSignature(image, "7E ? C7 86 ? ? ? ? ? ? ? ? EB ? 03 7C 24") orelse return false;
    const year = pe.findSignature(image, "B8 ? ? ? ? C3 90 90 90 90 90 90 90 90 90 90 56 57 8B 7C 24 ? 81 FF") orelse return false;
    host_buffer[0] = 0x2a;
    const host = state.bootstrap.hostSlice();
    const n = @min(host_buffer.len - 2, host.len);
    @memcpy(host_buffer[1 .. 1 + n], host[0..n]);
    host_buffer[1 + n] = 0;
    if (multi[0] != 0x90 and !memory.fillNop(@ptrCast(multi), 7)) return false;
    if (ssl[0] != 0x7e and !memory.protectWrite(@ptrCast(ssl), &[_]u8{0x7e})) return false;
    const host_ptr: u32 = @intCast(@intFromPtr(&host_buffer));
    const year_value: u32 = 0x802;
    if (!memory.protectWrite(@ptrCast(ea + 1), @as(*const [4]u8, @ptrCast(&host_ptr))[0..])) return false;
    if (!memory.protectWrite(@ptrCast(year + 1), @as(*const [4]u8, @ptrCast(&year_value))[0..])) return false;
    logger.line("U2 base patches installed", .{});
    return true;
}
