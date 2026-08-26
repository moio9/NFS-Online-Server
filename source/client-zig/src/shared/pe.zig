const std = @import("std");
const win = @import("win.zig");
const strings = @import("strings.zig");
const c = win.c;

const dos_magic: u16 = 0x5A4D;
const pe_magic: u32 = 0x00004550;
const ordinal_flag32: u32 = 0x80000000;

fn read(comptime T: type, address: usize) T {
    return @as(*align(1) const T, @ptrFromInt(address)).*;
}

fn write(comptime T: type, address: usize, value: T) void {
    @as(*align(1) T, @ptrFromInt(address)).* = value;
}

fn ntAddress(module: c.HMODULE) ?usize {
    const base = win.moduleBase(module) orelse return null;
    const base_addr = @intFromPtr(base);
    if (read(u16, base_addr) != dos_magic) return null;
    const pe_offset: usize = @intCast(read(u32, base_addr + 0x3c));
    const nt = base_addr + pe_offset;
    if (read(u32, nt) != pe_magic) return null;
    return nt;
}

pub fn imageSize(module: c.HMODULE) ?usize {
    const nt = ntAddress(module) orelse return null;
    // IMAGE_OPTIONAL_HEADER32.SizeOfImage: OptionalHeader(+0x18) + 0x38.
    return @intCast(read(u32, nt + 0x50));
}

pub fn image(module: c.HMODULE) ?[]u8 {
    const base = win.moduleBase(module) orelse return null;
    const size = imageSize(module) orelse return null;
    return base[0..size];
}

pub fn textSection(module: c.HMODULE) ?[]u8 {
    const base = win.moduleBase(module) orelse return null;
    const base_addr = @intFromPtr(base);
    const nt = ntAddress(module) orelse return null;
    const section_count: usize = @intCast(read(u16, nt + 6));
    const optional_size: usize = @intCast(read(u16, nt + 20));
    const first_section = nt + 24 + optional_size;
    var index: usize = 0;
    while (index < section_count) : (index += 1) {
        const section = first_section + index * 40;
        const name: [*]const u8 = @ptrFromInt(section);
        if (name[0] == '.' and name[1] == 't' and name[2] == 'e' and name[3] == 'x' and name[4] == 't') {
            const virtual_size: usize = @intCast(read(u32, section + 8));
            const virtual_address: usize = @intCast(read(u32, section + 12));
            const raw_size: usize = @intCast(read(u32, section + 16));
            const size: usize = @max(virtual_size, raw_size);
            const data: [*]u8 = @ptrFromInt(base_addr + virtual_address);
            return data[0..size];
        }
    }
    return null;
}

pub fn findMasked(haystack: []u8, bytes: []const u8, mask: []const u8) ?[*]u8 {
    if (bytes.len == 0 or bytes.len != mask.len or bytes.len > haystack.len) return null;
    var i: usize = 0;
    while (i + bytes.len <= haystack.len) : (i += 1) {
        var ok = true;
        for (bytes, mask, 0..) |byte, required, j| {
            if (required != 0 and haystack[i + j] != byte) {
                ok = false;
                break;
            }
        }
        if (ok) return haystack.ptr + i;
    }
    return null;
}

pub fn parseSignature(signature: []const u8, out_bytes: []u8, out_mask: []u8) ?usize {
    var count: usize = 0;
    var tokens = std.mem.tokenizeScalar(u8, signature, ' ');
    while (tokens.next()) |token| {
        if (count >= out_bytes.len or count >= out_mask.len) return null;
        if (token.len == 0) continue;
        if (token[0] == '?') {
            out_bytes[count] = 0;
            out_mask[count] = 0;
        } else {
            out_bytes[count] = std.fmt.parseUnsigned(u8, token, 16) catch return null;
            out_mask[count] = 0xff;
        }
        count += 1;
    }
    return if (count == 0) null else count;
}

pub fn findSignature(haystack: []u8, signature: []const u8) ?[*]u8 {
    var bytes: [256]u8 = undefined;
    var mask: [256]u8 = undefined;
    const count = parseSignature(signature, bytes[0..], mask[0..]) orelse return null;
    return findMasked(haystack, bytes[0..count], mask[0..count]);
}

fn patchThunk(slot_address: usize, replacement: *const anyopaque, old_out: ?*?*anyopaque) bool {
    const current_value = read(u32, slot_address);
    if (current_value == @as(u32, @truncate(@intFromPtr(replacement)))) return false;
    var old: c.DWORD = 0;
    const slot: *anyopaque = @ptrFromInt(slot_address);
    if (c.VirtualProtect(slot, @sizeOf(u32), c.PAGE_EXECUTE_READWRITE, &old) == 0) return false;
    if (old_out) |output| {
        if (output.* == null) {
            output.* = @ptrFromInt(@as(usize, @intCast(current_value)));
        }
    }
    write(u32, slot_address, @truncate(@intFromPtr(replacement)));
    var ignored: c.DWORD = 0;
    _ = c.VirtualProtect(slot, @sizeOf(u32), old, &ignored);
    _ = c.FlushInstructionCache(c.GetCurrentProcess(), slot, @sizeOf(u32));
    return true;
}

pub fn hookIat(
    module: c.HMODULE,
    dll_name: []const u8,
    function_name: ?[]const u8,
    ordinal: u16,
    replacement: *const anyopaque,
    old_out: ?*?*anyopaque,
) bool {
    const base = win.moduleBase(module) orelse return false;
    const base_addr = @intFromPtr(base);
    const nt = ntAddress(module) orelse return false;
    // Import directory: OptionalHeader(+0x18) + DataDirectory(+0x60) + entry 1(+0x08).
    const import_rva: usize = @intCast(read(u32, nt + 0x80));
    if (import_rva == 0) return false;

    var hit = false;
    var descriptor = base_addr + import_rva;
    while (read(u32, descriptor + 12) != 0) : (descriptor += 20) {
        const name_rva: usize = @intCast(read(u32, descriptor + 12));
        const imported_name: [*:0]const u8 = @ptrFromInt(base_addr + name_rva);
        if (!strings.eqlIgnoreCase(std.mem.span(imported_name), dll_name)) continue;

        const original_rva: usize = @intCast(read(u32, descriptor + 0));
        const first_rva: usize = @intCast(read(u32, descriptor + 16));
        var original = base_addr + (if (original_rva != 0) original_rva else first_rva);
        var first = base_addr + first_rva;
        while (read(u32, original) != 0) : ({
            original += 4;
            first += 4;
        }) {
            const raw = read(u32, original);
            var match = false;
            if ((raw & ordinal_flag32) != 0) {
                match = ordinal != 0 and @as(u16, @truncate(raw)) == ordinal;
            } else if (function_name) |wanted| {
                // IMAGE_IMPORT_BY_NAME: u16 hint followed by a zero-terminated name.
                const imported: [*:0]const u8 = @ptrFromInt(base_addr + @as(usize, @intCast(raw)) + 2);
                match = strings.eqlIgnoreCase(std.mem.span(imported), wanted);
            }
            if (match) {
                hit = patchThunk(first, replacement, old_out) or hit;
            }
        }
    }
    return hit;
}

pub fn hookLoadedModules(self_module: c.HMODULE, callback: *const fn (c.HMODULE) void) void {
    const snapshot = c.CreateToolhelp32Snapshot(c.TH32CS_SNAPMODULE, c.GetCurrentProcessId());
    if (snapshot == c.INVALID_HANDLE_VALUE) {
        callback(c.GetModuleHandleA(null));
        return;
    }
    defer _ = c.CloseHandle(snapshot);
    var entry: c.MODULEENTRY32 = std.mem.zeroes(c.MODULEENTRY32);
    entry.dwSize = @sizeOf(c.MODULEENTRY32);
    if (c.Module32First(snapshot, &entry) == 0) return;
    while (true) {
        if (entry.hModule != self_module) callback(entry.hModule);
        if (c.Module32Next(snapshot, &entry) == 0) break;
    }
}
