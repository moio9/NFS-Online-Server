const std = @import("std");
const win = @import("shared").win;
const detour = @import("shared").detour;
const strings = @import("shared").strings;
const logger = @import("shared").logger;
const state = @import("state.zig");
const c = win.c;

var hook_record = detour.Inline{};

fn readable(address: usize, length: usize) bool {
    if (address < 0x10000 or length == 0) return false;
    var current = address;
    const end = address +% length;
    if (end < address) return false;
    while (current < end) {
        var info: c.MEMORY_BASIC_INFORMATION = std.mem.zeroes(c.MEMORY_BASIC_INFORMATION);
        if (c.VirtualQuery(@ptrFromInt(current), &info, @sizeOf(c.MEMORY_BASIC_INFORMATION)) != @sizeOf(c.MEMORY_BASIC_INFORMATION)) return false;
        if (info.State != c.MEM_COMMIT or (info.Protect & (c.PAGE_NOACCESS | c.PAGE_GUARD)) != 0) return false;
        const region_base = info.BaseAddress orelse return false;
        const next = @intFromPtr(region_base) + info.RegionSize;
        if (next <= current) return false;
        current = next;
    }
    return true;
}

fn writable(address: usize, length: usize) bool {
    if (!readable(address, length)) return false;
    var info: c.MEMORY_BASIC_INFORMATION = std.mem.zeroes(c.MEMORY_BASIC_INFORMATION);
    if (c.VirtualQuery(@ptrFromInt(address), &info, @sizeOf(c.MEMORY_BASIC_INFORMATION)) == 0) return false;
    const protection = info.Protect & 0xff;
    return protection == c.PAGE_READWRITE or protection == c.PAGE_WRITECOPY or
        protection == c.PAGE_EXECUTE_READWRITE or protection == c.PAGE_EXECUTE_WRITECOPY;
}

fn read32(address: usize) u32 {
    return @as(*align(1) const volatile u32, @ptrFromInt(address)).*;
}
fn write32(address: usize, value: u32) void {
    @as(*align(1) volatile u32, @ptrFromInt(address)).* = value;
}
fn readPtr(address: usize) usize {
    return @as(usize, @intCast(read32(address)));
}
fn writePtr(address: usize, value: usize) void {
    write32(address, @intCast(value));
}

fn syntheticHandle(name: []const u8, port: u16) u32 {
    var hash: u32 = 2166136261;
    for (name) |raw| {
        const ch = strings.lower(raw);
        hash ^= ch;
        hash *%= 16777619;
    }
    hash ^= @as(u32, port);
    hash *%= 16777619;
    return if (hash == 0 or hash == 0xffffffff) 0x01020304 ^ @as(u32, port) else hash;
}

fn ready() bool {
    return state.lan_enabled and state.inject_server and state.lan.port != 0 and state.lan.hostSlice().len != 0;
}

fn seedProvider(self: usize) void {
    if (!ready() or self < 0x10000) return;
    const base = @intFromPtr(c.GetModuleHandleA(null) orelse return);
    const AllocFn = *const fn (usize) callconv(.c) ?*anyopaque;
    const LockFn = *const fn (usize) callconv(.c) void;
    const game_alloc: AllocFn = @ptrFromInt(base + 0x00350160);
    const provider_lock: LockFn = @ptrFromInt(base + 0x0034B7E0);
    const provider_unlock: LockFn = @ptrFromInt(base + 0x0034B870);
    if (!readable(self + 0xfc, 4)) return;
    const owner = readPtr(self + 0xfc);
    if (!readable(owner, 0x68) or !writable(owner + 100, 4)) return;
    var provider = readPtr(owner + 100);
    if (provider == 0) {
        provider = @intFromPtr(game_alloc(0x10) orelse return);
        writePtr(owner + 100, provider);
    }
    if (!readable(provider, 0x30)) return;
    const start = readPtr(provider + 0x28);
    const end = readPtr(provider + 0x2c);
    if (start < 0x10000 or end <= start or !writable(start, end - start)) return;
    const tag_address = self + 0x918;
    const tag = if (readable(tag_address, 1) and @as(*const u8, @ptrFromInt(tag_address)).* != 0)
        std.mem.span(@as([*:0]const u8, @ptrFromInt(tag_address)))
    else
        "GmUtil";
    var name_buf: [13]u8 = [_]u8{0} ** 13;
    strings.copyZ(name_buf[0..], state.lan.hostSlice());
    const name = strings.sliceZ(name_buf[0..]);
    provider_lock(provider);
    defer provider_unlock(provider);
    var entry: usize = 0;
    var free_entry: usize = 0;
    var cursor: usize = start;
    while (cursor + 0x1a4 <= end) : (cursor += 0x1a4) {
        if (free_entry == 0 and @as(*const u8, @ptrFromInt(cursor + 0x28)).* == 0) {
            free_entry = cursor;
        }
        const entry_tag = std.mem.span(@as([*:0]const u8, @ptrFromInt(cursor + 0x08)));
        const entry_name = std.mem.span(@as([*:0]const u8, @ptrFromInt(cursor + 0x28)));
        if (strings.eqlIgnoreCase(entry_tag, tag) and strings.eqlIgnoreCase(entry_name, name)) {
            entry = cursor;
            break;
        }
    }
    if (entry == 0) {
        entry = free_entry;
    }
    if (entry == 0) return;
    if (entry == free_entry) @memset(@as([*]u8, @ptrFromInt(entry))[0..0x1a4], 0);
    var field_buf: [32]u8 = undefined;
    const field = std.fmt.bufPrintZ(&field_buf, "{d}|0", .{state.lan.port}) catch return;
    strings.copyZ(@as([*]u8, @ptrFromInt(entry + 0x08))[0..0x20], tag);
    strings.copyZ(@as([*]u8, @ptrFromInt(entry + 0x28))[0..0x20], name);
    strings.copyZ(@as([*]u8, @ptrFromInt(entry + 0x48))[0..0xc0], field);
    strings.copyZ(@as([*]u8, @ptrFromInt(entry + 0x108))[0..0x78], "TCP:1");
    write32(entry + 0x180, c.GetTickCount() + 3600000);
    const handle = syntheticHandle(name, state.lan.port);
    write32(entry + 0x194, handle);
    write32(entry + 0x198, handle);
    write32(entry + 0x19c, 0);
}

fn handleLan(self: usize) void {
    if (!ready() or self < 0x10000) return;
    if (!readable(self + 0xfc, 4) or !readable(self + 0x908, 16) or !writable(self + 0x100, 0x81c)) return;
    const base = @intFromPtr(c.GetModuleHandleA(null) orelse return);
    const ReadProviderFn = *const fn (usize, usize, *anyopaque, c_int) callconv(.c) c_int;
    const ResolveProviderFn = *const fn (usize, usize, *anyopaque) callconv(.c) c_int;
    const FreeObjectFn = *const fn (usize) callconv(.c) void;
    const RefreshFn = *const fn (usize) callconv(win.THISCALL_MINGW) void;
    const read_provider: ReadProviderFn = @ptrFromInt(base + 0x00343aa0);
    const resolve_provider: ResolveProviderFn = @ptrFromInt(base + 0x00343a50);
    const free_object: FreeObjectFn = @ptrFromInt(base + 0x00140a60);
    const refresh: RefreshFn = @ptrFromInt(base + 0x000e8a00);

    var name_buf: [13]u8 = [_]u8{0} ** 13;
    strings.copyZ(name_buf[0..], state.lan.hostSlice());
    const display_name = strings.sliceZ(name_buf[0..]);
    var blob: [0x800]u8 = [_]u8{0} ** 0x800;
    var count = read_provider(readPtr(self + 0xfc), self + 0x918, &blob, @intCast(blob.len));
    if (count < 0) {
        count = 0;
    }
    var blob_len = std.mem.findScalar(u8, blob[0..], 0) orelse blob.len;
    var present = false;
    var lines = std.mem.splitScalar(u8, blob[0..blob_len], '\n');
    while (lines.next()) |line| {
        const tab = std.mem.findScalar(u8, line, '\t') orelse continue;
        if (strings.eqlIgnoreCase(line[0..tab], display_name)) {
            present = true;
            break;
        }
    }
    if (!present) {
        if (blob_len != 0 and blob[blob_len - 1] != '\n') {
            if (blob_len + 1 >= blob.len) return;
            blob[blob_len] = '\n';
            blob_len += 1;
        }
        var line_buffer: [64]u8 = undefined;
        const line = std.fmt.bufPrint(&line_buffer, "{s}\t{d}|0\t\n", .{ display_name, state.lan.port }) catch return;
        if (blob_len + line.len + 1 >= blob.len) return;
        @memcpy(blob[blob_len .. blob_len + line.len], line);
        blob_len += line.len;
        blob[blob_len] = 0;
        count += 1;
    }
    if (count == @as(c_int, @bitCast(read32(self + 0x914))) and std.mem.eql(u8, @as([*]const u8, @ptrFromInt(self + 0x100))[0..blob.len], blob[0..])) return;
    @memcpy(@as([*]u8, @ptrFromInt(self + 0x100))[0..blob.len], blob[0..]);
    write32(self + 0x914, @bitCast(count));

    const list_head = self + 0x908;
    while (readPtr(list_head) != list_head) {
        const node = readPtr(list_head);
        const next = readPtr(node);
        const prev = readPtr(node + 4);
        writePtr(prev, next);
        writePtr(next + 4, prev);
        _ = c.HeapFree(c.GetProcessHeap(), 0, @ptrFromInt(node));
    }

    var cursor: usize = 0;
    var built: c_int = 0;
    while (built < count and cursor < blob_len) : (built += 1) {
        const rest = blob[cursor..blob_len];
        const first_tab_rel = std.mem.findScalar(u8, rest, '\t') orelse break;
        const first_tab = cursor + first_tab_rel;
        const second_rest = blob[first_tab + 1 .. blob_len];
        const second_tab_rel = std.mem.findScalar(u8, second_rest, '\t') orelse break;
        const second_tab = first_tab + 1 + second_tab_rel;
        const raw_entry = c.HeapAlloc(c.GetProcessHeap(), 0, 0x24) orelse break;
        const entry = @intFromPtr(raw_entry);
        @memset(@as([*]u8, @ptrCast(raw_entry))[0..0x24], 0);
        strings.copyZ(@as([*]u8, @ptrFromInt(entry + 8))[0..13], blob[cursor..first_tab]);
        const field = blob[first_tab + 1 .. second_tab];
        const pipe = std.mem.findScalar(u8, field, '|');
        if (pipe) |p| {
            const port = std.fmt.parseUnsigned(u16, field[0..p], 10) catch 0;
            @as(*align(1) u16, @ptrFromInt(entry + 28)).* = port;
            write32(entry + 32, std.fmt.parseUnsigned(u32, field[p + 1 ..], 10) catch 0);
        }
        var name_z: [13]u8 = [_]u8{0} ** 13;
        strings.copyZ(name_z[0..], blob[cursor..first_tab]);
        var meta = resolve_provider(readPtr(self + 0xfc), self + 0x918, @ptrCast(&name_z));
        if (meta == 0) {
            meta = @bitCast(syntheticHandle(strings.sliceZ(name_z[0..]), @as(*align(1) u16, @ptrFromInt(entry + 28)).*));
        }
        write32(entry + 24, @bitCast(meta));
        const tail = readPtr(self + 0x90c);
        writePtr(tail, entry);
        writePtr(self + 0x90c, entry);
        writePtr(entry + 4, tail);
        writePtr(entry, list_head);
        const newline_rel = std.mem.findScalar(u8, blob[second_tab + 1 .. blob_len], '\n') orelse break;
        cursor = second_tab + 1 + newline_rel + 1;
    }
    refresh(self);
    const object = readPtr(self + 0x904);
    if (object != 0) {
        free_object(object);
        write32(self + 0x904, 0);
        write32(self + 0x900, 0);
    }
}

var hook_logged = false;

noinline fn hookBody(self: usize) void {
    if (!hook_logged) {
        hook_logged = true;
        logger.line("U2 LAN hook entered self=0x{x}", .{self});
    }
    if (self < 0x10000) {
        logger.line("U2 LAN hook rejected invalid self=0x{x}", .{self});
        return;
    }
    seedProvider(self);
    if (hook_record.trampoline) |raw| {
        const Original = *const fn (usize) callconv(win.THISCALL) void;
        const original: Original = @ptrCast(raw);
        original(self);
    }
    handleLan(self);
}

// The game enters LAN_F3040 as a thiscall method, with `self` in ECX.
// Zig's x86_fastcall wrapper placed the single parameter on the stack for
// this target, producing self=0. Match the original method ABI directly.
fn hook(self: usize) callconv(win.THISCALL) void {
    hookBody(self);
}

pub fn install() bool {
    if (!ready()) return true;
    const base = @intFromPtr(c.GetModuleHandleA(null) orelse return false);
    const target: *anyopaque = @ptrFromInt(base + 0x000f3040);
    const target_bytes: [*]const u8 = @ptrCast(target);
    if (target_bytes[0] == 0xE9) {
        logger.line("U2 LAN injection already installed; duplicate instance skipped", .{});
        return true;
    }
    const ok = detour.install(&hook_record, target, @ptrCast(&hook), 6);
    logger.line("U2 LAN injection {s}", .{if (ok) "installed" else "failed"});
    return ok;
}
