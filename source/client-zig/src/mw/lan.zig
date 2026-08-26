const std = @import("std");
const win = @import("shared").win;
const detour = @import("shared").detour;
const logger = @import("shared").logger;
const net = @import("shared").net_util;
const strings = @import("shared").strings;
const state = @import("state.zig");
const c = win.c;

const image_base: usize = 0x00400000;
fn va(address: usize) *anyopaque {
    const base = c.GetModuleHandleA(null) orelse return @ptrFromInt(address);
    return @ptrFromInt(@intFromPtr(base) + (address - image_base));
}

const LanNode = extern struct {
    next: ?*LanNode,
    prev: ?*LanNode,
    name: [13]u8,
    padding0: [3]u8,
    meta: u32,
    short_value: u16,
    padding1: u16,
    id: u32,
};
comptime {
    if (@sizeOf(LanNode) != 0x24) @compileError("MW LAN node layout changed");
}

// These routines take a single object pointer in ECX. Zig's x86 fastcall
// lowering produced an invalid stack value under the Windows GNU target,
// while x86_thiscall matches the actual one-register ABI used here.
const ParseFn = *const fn (*anyopaque) callconv(win.THISCALL) void;
const RebuildFn = *const fn (*anyopaque) callconv(win.THISCALL) void;
const RefreshFn = *const fn (*anyopaque) callconv(win.THISCALL) u32;
const SetHostFn = *const fn ([*:0]const u8) callconv(.winapi) void;
const HostGetterFn = *const fn () callconv(.c) [*:0]const u8;
const PortGetterFn = *const fn () callconv(.c) u32;
const GameMallocFn = *const fn (usize) callconv(.c) ?*anyopaque;

var parse_detour = detour.Inline{};
var set_host_detour = detour.Inline{};
var host_getter_detour = detour.Inline{};
var port_getter_detour = detour.Inline{};
var in_parse = false;
var parse_calls: u32 = 0;

fn original(comptime T: type, record: *const detour.Inline) T {
    return @ptrCast(record.trampoline.?);
}
fn listHead(self: *anyopaque) *LanNode {
    return @ptrFromInt(@intFromPtr(self) + 0x8f0);
}

fn count(self: *anyopaque) u32 {
    const head = listHead(self);
    var node = head.next;
    var n: u32 = 0;
    while (node != null and node != head and n < 128) : (n += 1) node = node.?.next;
    return n;
}

fn contains(self: *anyopaque, wanted: []const u8) bool {
    const head = listHead(self);
    var node = head.next;
    var guard: usize = 0;
    while (node != null and node != head and guard < 128) : (guard += 1) {
        if (strings.eqlIgnoreCase(strings.sliceZ(node.?.name[0..]), wanted)) return true;
        node = node.?.next;
    }
    return false;
}

fn inject(self: *anyopaque) bool {
    if (!state.inject_server) return false;
    const wanted = strings.sliceZ(state.server_name[0..]);
    if (wanted.len == 0 or contains(self, wanted)) return false;
    var address: c.sockaddr_in = undefined;
    if (!net.resolve(&state.lan, &address)) return false;
    const malloc_fn: GameMallocFn = @ptrCast(va(0x00652AD0));
    const raw = malloc_fn(@sizeOf(LanNode)) orelse return false;
    const node: *LanNode = @ptrCast(@alignCast(raw));
    node.* = std.mem.zeroes(LanNode);
    strings.copyZ(node.name[0..], wanted);
    node.meta = c.ntohl(net.ipv4Address(&address));
    node.short_value = state.lan.port;
    node.id = state.server_racers;
    const head = listHead(self);
    const tail = head.prev orelse head;
    tail.next = node;
    head.prev = node;
    node.prev = tail;
    node.next = head;
    const n = count(self);
    @as(*align(1) u32, @ptrFromInt(@intFromPtr(self) + 0x8f8)).* = n;
    @as(*align(1) u32, @ptrFromInt(@intFromPtr(self) + 0x8fc)).* = n;
    if (n > 0) {
        @as(*align(1) u32, @ptrFromInt(@intFromPtr(self) + 0x33c)).* = 1;
        @as(*align(1) u32, @ptrFromInt(@intFromPtr(self) + 0x340)).* = 1;
        @as(*align(1) u32, @ptrFromInt(@intFromPtr(self) + 0x348)).* = n;
        @as(*align(1) u32, @ptrFromInt(@intFromPtr(self) + 0x34c)).* = 0;
    }
    logger.line("MW LAN server inserted: {s}:{d} racers={d}", .{ wanted, state.lan.port, state.server_racers });
    return true;
}

fn validSelf(self: *anyopaque) bool {
    return @intFromPtr(self) >= 0x10000;
}

fn hookParse(self: *anyopaque) callconv(win.THISCALL) void {
    if (!validSelf(self)) {
        logger.line("MW LAN parse rejected invalid self=0x{x}", .{@intFromPtr(self)});
        return;
    }

    if (in_parse) {
        logger.line("MW LAN parse reentry self=0x{x}", .{@intFromPtr(self)});
        original(ParseFn, &parse_detour)(self);
        return;
    }

    in_parse = true;
    defer in_parse = false;
    parse_calls +%= 1;
    logger.line("MW LAN parse enter call={d} self=0x{x}", .{ parse_calls, @intFromPtr(self) });
    original(ParseFn, &parse_detour)(self);
    logger.line("MW LAN parse original returned call={d} self=0x{x}", .{ parse_calls, @intFromPtr(self) });

    const inserted = inject(self);
    logger.line("MW LAN parse inject call={d} inserted={}", .{ parse_calls, inserted });
    if (inserted) {
        const rebuild: RebuildFn = @ptrCast(va(0x0054E560));
        logger.line("MW LAN rebuild enter call={d} self=0x{x}", .{ parse_calls, @intFromPtr(self) });
        rebuild(self);
        logger.line("MW LAN rebuild leave call={d} self=0x{x}", .{ parse_calls, @intFromPtr(self) });
        if (state.refresh_state) {
            const refresh: RefreshFn = @ptrCast(va(0x00558910));
            logger.line("MW LAN refresh enter call={d} self=0x{x}", .{ parse_calls, @intFromPtr(self) });
            _ = refresh(self);
            logger.line("MW LAN refresh leave call={d} self=0x{x}", .{ parse_calls, @intFromPtr(self) });
        }
    }
}

fn hookSetHost(host: [*:0]const u8) callconv(.winapi) void {
    var selected = host;
    if (state.lan_enabled and state.lan.hostSlice().len != 0) {
        selected = @ptrCast(state.lan.host[0..].ptr);
    }
    original(SetHostFn, &set_host_detour)(selected);
}

fn hookHostGetter() callconv(.c) [*:0]const u8 {
    const value = original(HostGetterFn, &host_getter_detour)();
    const current = std.mem.span(value);
    if (state.network_enabled and (current.len == 0 or current[0] == '*' or strings.indexOfIgnoreCase(current, "pcnfs06.ea.com") != null))
        return @ptrCast(state.bootstrap.host[0..].ptr);
    return value;
}

fn hookPortGetter() callconv(.c) u32 {
    const value = original(PortGetterFn, &port_getter_detour)();
    const port: u16 = @truncate(value);
    const high = value & 0xffff0000;

    // The LAN selector exposes the online entry as 30920, but joining it must
    // begin at the bootstrap service. The bootstrap response then advertises
    // the real lobby endpoint (normally 30920).
    if (state.network_enabled and (port == 30920 or port == state.lobby.port)) {
        logger.line("MW port route lobby={d} -> bootstrap={d}", .{ port, state.bootstrap.port });
        return high | @as(u32, state.bootstrap.port);
    }

    if (state.lan_enabled and state.lan.port != 0) {
        logger.line("MW port route default={d} -> lan={d}", .{ port, state.lan.port });
        return high | @as(u32, state.lan.port);
    }

    return value;
}

pub fn install() bool {
    if (!state.lan_enabled and !state.network_enabled) return true;
    var ok = true;
    if (state.lan_enabled) {
        ok = detour.install(&parse_detour, va(0x00558DF0), @ptrCast(&hookParse), 6) and ok;
        if (state.selected_host_hook)
            ok = detour.install(&set_host_detour, va(0x0056E480), @ptrCast(&hookSetHost), 6) and ok;
    }
    if (state.network_enabled) {
        ok = detour.install(&host_getter_detour, va(0x0056E440), @ptrCast(&hookHostGetter), 5) and ok;
        ok = detour.install(&port_getter_detour, va(0x0056E460), @ptrCast(&hookPortGetter), 5) and ok;
    }
    if (ok) {
        logger.line("MW internal hooks installed (parser-only LAN mode)", .{});
        logger.line("MW LAN constructor hook intentionally disabled", .{});
    }
    return ok;
}
