const std = @import("std");
const win = @import("win.zig");
const strings = @import("strings.zig");
const c = win.c;

pub const max_file_size = 64 * 1024;

pub fn siblingPath(module: c.HMODULE, filename: []const u8, out: []u8) ?[:0]u8 {
    if (out.len < 2) return null;
    @memset(out, 0);
    const got_raw = c.GetModuleFileNameA(module, out.ptr, @intCast(out.len));
    const got: usize = @intCast(got_raw);
    if (got == 0 or got >= out.len) return null;
    const dir = strings.basenameDir(out[0..got]);
    if (dir.len + filename.len + 1 > out.len) return null;
    @memcpy(out[dir.len .. dir.len + filename.len], filename);
    out[dir.len + filename.len] = 0;
    return out[0 .. dir.len + filename.len :0];
}

pub fn readFile(path: [*:0]const u8, buffer: []u8) ?[]u8 {
    const h = c.CreateFileA(path, c.GENERIC_READ, c.FILE_SHARE_READ | c.FILE_SHARE_WRITE, null, c.OPEN_EXISTING, c.FILE_ATTRIBUTE_NORMAL, null);
    if (h == c.INVALID_HANDLE_VALUE) return null;
    defer _ = c.CloseHandle(h);
    var got: c.DWORD = 0;
    if (buffer.len == 0 or c.ReadFile(h, buffer.ptr, @intCast(buffer.len), &got, null) == 0) return null;
    return buffer[0..@as(usize, @intCast(got))];
}

pub fn loadFlat(module: c.HMODULE, filename: []const u8, context: anytype, comptime apply: anytype) bool {
    var path: [c.MAX_PATH]u8 = undefined;
    const zpath = siblingPath(module, filename, path[0..]) orelse return false;
    var storage: [max_file_size]u8 = undefined;
    const bytes = readFile(zpath.ptr, storage[0..]) orelse return false;
    var lines = std.mem.splitScalar(u8, bytes, '\n');
    while (lines.next()) |raw| {
        var line = strings.trim(raw);
        if (line.len == 0 or line[0] == '#' or line[0] == ';' or line[0] == '[') continue;
        if (std.mem.findScalar(u8, line, '#')) |idx| {
            line = strings.trim(line[0..idx]);
        }
        if (std.mem.findScalar(u8, line, ';')) |idx| {
            line = strings.trim(line[0..idx]);
        }
        const eq = std.mem.findScalar(u8, line, '=') orelse continue;
        const key = strings.trim(line[0..eq]);
        const value = strings.trim(line[eq + 1 ..]);
        if (key.len == 0 or value.len == 0) continue;
        apply(context, key, value);
    }
    return true;
}

pub fn loadIni(module: c.HMODULE, filename: []const u8, context: anytype, comptime apply: anytype) bool {
    var path: [c.MAX_PATH]u8 = undefined;
    const zpath = siblingPath(module, filename, path[0..]) orelse return false;
    var storage: [max_file_size]u8 = undefined;
    const bytes = readFile(zpath.ptr, storage[0..]) orelse return false;
    var section: []const u8 = "";
    var lines = std.mem.splitScalar(u8, bytes, '\n');
    while (lines.next()) |raw| {
        var line = strings.trim(raw);
        if (line.len == 0 or line[0] == '#' or line[0] == ';') continue;
        if (line[0] == '[' and line[line.len - 1] == ']') {
            section = strings.trim(line[1 .. line.len - 1]);
            continue;
        }
        if (std.mem.findScalar(u8, line, '#')) |idx| {
            line = strings.trim(line[0..idx]);
        }
        if (std.mem.findScalar(u8, line, ';')) |idx| {
            line = strings.trim(line[0..idx]);
        }
        const eq = std.mem.findScalar(u8, line, '=') orelse continue;
        const key = strings.trim(line[0..eq]);
        const value = strings.trim(line[eq + 1 ..]);
        if (key.len == 0) continue;
        apply(context, section, key, value);
    }
    return true;
}
