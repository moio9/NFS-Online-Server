const std = @import("std");
const win = @import("shared").win;
const detour = @import("shared").detour;
const logger = @import("shared").logger;
const strings = @import("shared").strings;
const state = @import("state.zig");
const c = win.c;

const rva_resolve: usize = 0x0047150A;
const rva_port: usize = 0x0046E9E9;
const rva_session_key: usize = 0x0047585F;
const rva_game_free_ptr: usize = 0x0067B940;
const rva_game_malloc_ptr: usize = 0x0067B944;
const session_key = "7ab3f388bbf5200ebf5e501260ecddcc\x00";

const ResolveFn = *const fn (?*anyopaque) callconv(.c) c_int;
const PortFn = *const fn (u32) callconv(.c) u16;
const MallocFn = *const fn (u32) callconv(.c) ?[*]u8;
const FreeFn = *const fn (?*anyopaque) callconv(.c) void;

var resolve_detour = detour.Inline{};
var port_detour = detour.Inline{};
var key_detour = detour.Inline{};

fn address(rva: usize) usize {
    return @intFromPtr(state.game_base.?) + rva;
}
fn bytesEqual(address_value: usize, expected: []const u8) bool {
    const current: [*]const u8 = @ptrFromInt(address_value);
    return std.mem.eql(u8, current[0..expected.len], expected);
}
fn original(comptime T: type, record: *const detour.Inline) T {
    return @ptrCast(record.trampoline.?);
}
fn isMadId(id: u32) bool {
    return id == 1 or (state.force_all_mad_ports and (id == 3 or id == 4 or id == 5));
}

fn boundedCStringLength(pointer: [*]const u8, limit: usize) usize {
    var length: usize = 0;
    while (length < limit and pointer[length] != 0) : (length += 1) {}
    return length;
}

fn replaceGameString(slot: *align(1) ?[*]u8, replacement: []const u8) bool {
    const malloc_address = @as(*align(1) usize, @ptrFromInt(address(rva_game_malloc_ptr))).*;
    const free_address = @as(*align(1) usize, @ptrFromInt(address(rva_game_free_ptr))).*;
    if (malloc_address != 0) {
        const game_malloc: MallocFn = @ptrFromInt(malloc_address);
        const fresh = game_malloc(@intCast(replacement.len + 1)) orelse return false;
        @memcpy(fresh[0..replacement.len], replacement);
        fresh[replacement.len] = 0;
        if (slot.*) |old| {
            if (free_address != 0) {
                const game_free: FreeFn = @ptrFromInt(free_address);
                game_free(@ptrCast(old));
            }
        }
        slot.* = fresh;
        return true;
    }
    if (slot.*) |old| {
        if (boundedCStringLength(old, 4096) >= replacement.len) {
            @memcpy(old[0..replacement.len], replacement);
            old[replacement.len] = 0;
            return true;
        }
    }
    return false;
}

fn hookResolve(server_list: ?*anyopaque) callconv(.c) c_int {
    if (state.mad_enabled and server_list != null) {
        var node = @as(*align(1) ?[*]u8, @ptrCast(server_list.?)).*;
        var guard: usize = 0;
        while (node != null and guard < 64) : (guard += 1) {
            const current = node.?;
            const pair = @as(*align(1) ?[*]u8, @ptrCast(current + 8)).*;
            if (pair) |entry| {
                const id: u32 = entry[0x14];
                if (isMadId(id)) {
                    const slot: *align(1) ?[*]u8 = @ptrCast(entry + 0x18);
                    _ = replaceGameString(slot, strings.sliceZ(state.mad_host[0..]));
                }
            }
            node = @as(*align(1) ?[*]u8, @ptrCast(current)).*;
        }
    }
    return original(ResolveFn, &resolve_detour)(server_list);
}

fn hookPort(id: u32) callconv(.c) u16 {
    if (state.mad_enabled and isMadId(id)) return state.mad_port;
    return original(PortFn, &port_detour)(id);
}

fn hookSessionKey(output: ?[*]u8) callconv(.c) void {
    if (output) |target| @memcpy(target[0..session_key.len], session_key);
}

pub fn signaturesReady() bool {
    if (state.game_base == null) return false;
    return bytesEqual(address(rva_resolve), &[_]u8{ 0x55, 0x8b, 0xec, 0x57, 0x8b, 0x7d, 0x08 }) and
        bytesEqual(address(rva_port), &[_]u8{ 0x56, 0x57, 0xbf, 0xe4, 0xe3, 0x9f, 0x00 }) and
        bytesEqual(address(rva_session_key), &[_]u8{ 0x55, 0x8d, 0x6c, 0x24, 0x8c });
}

pub fn install() bool {
    if (!state.mad_enabled) return true;
    if (!signaturesReady()) return false;
    var ok = detour.install(&resolve_detour, @ptrFromInt(address(rva_resolve)), @ptrCast(&hookResolve), 7);
    ok = detour.install(&port_detour, @ptrFromInt(address(rva_port)), @ptrCast(&hookPort), 7) and ok;
    ok = detour.install(&key_detour, @ptrFromInt(address(rva_session_key)), @ptrCast(&hookSessionKey), 5) and ok;
    if (ok) logger.line("Carbon MAD hooks installed", .{});
    return ok;
}
