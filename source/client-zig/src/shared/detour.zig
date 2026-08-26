const std = @import("std");
const win = @import("win.zig");
const memory = @import("memory.zig");
const c = win.c;

pub const Inline = struct {
    target: ?*anyopaque = null,
    replacement: ?*const anyopaque = null,
    trampoline: ?*anyopaque = null,
    length: usize = 0,
};

fn jumpBytes(from: usize, to: usize, length: usize, output: []u8) bool {
    if (length < 5 or output.len < length) return false;
    output[0] = 0xE9;
    const delta = @as(i64, @intCast(to)) - @as(i64, @intCast(from + 5));
    if (delta < std.math.minInt(i32) or delta > std.math.maxInt(i32)) return false;
    const rel: i32 = @intCast(delta);
    @as(*align(1) i32, @ptrCast(output.ptr + 1)).* = rel;
    @memset(output[5..length], 0x90);
    return true;
}

pub fn install(record: *Inline, target: *anyopaque, replacement: *const anyopaque, stolen_len: usize) bool {
    if (record.trampoline != null) return true;
    if (stolen_len < 5 or stolen_len > 64) return false;
    const trampoline = c.VirtualAlloc(null, stolen_len + 5, c.MEM_COMMIT | c.MEM_RESERVE, c.PAGE_EXECUTE_READWRITE) orelse return false;
    const source: [*]const u8 = @ptrCast(target);
    // Never detour an already-detoured entrypoint. Copying an existing E9
    // into a second trampoline creates a jump chain with invalid state.
    if (source[0] == 0xE9) return false;
    const tramp_bytes: [*]u8 = @ptrCast(trampoline);
    @memcpy(tramp_bytes[0..stolen_len], source[0..stolen_len]);
    var back: [69]u8 = undefined;
    if (!jumpBytes(@intFromPtr(trampoline) + stolen_len, @intFromPtr(target) + stolen_len, 5, back[0..5])) return false;
    @memcpy(tramp_bytes[stolen_len .. stolen_len + 5], back[0..5]);
    var patch: [64]u8 = undefined;
    if (!jumpBytes(@intFromPtr(target), @intFromPtr(replacement), stolen_len, patch[0..stolen_len])) return false;
    if (!memory.protectWrite(target, patch[0..stolen_len])) return false;
    record.* = .{ .target = target, .replacement = replacement, .trampoline = trampoline, .length = stolen_len };
    return true;
}
