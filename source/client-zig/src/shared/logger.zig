const std = @import("std");
const win = @import("win.zig");
const strings = @import("strings.zig");
const c = win.c;

var handle: c.HANDLE = c.INVALID_HANDLE_VALUE;
var lock: c.CRITICAL_SECTION = undefined;
var lock_ready = false;
var enabled = true;

pub fn init(module: c.HMODULE, filename: []const u8) void {
    if (!lock_ready) {
        c.InitializeCriticalSection(&lock);
        lock_ready = true;
    }
    var module_path: [c.MAX_PATH]u8 = [_]u8{0} ** c.MAX_PATH;
    const got_raw = c.GetModuleFileNameA(module, module_path[0..].ptr, @intCast(module_path.len));
    const got: usize = @intCast(got_raw);
    if (got == 0) return;
    const dir = strings.basenameDir(module_path[0..got]);
    var full: [c.MAX_PATH]u8 = [_]u8{0} ** c.MAX_PATH;
    const need = @min(full.len - 1, dir.len + filename.len);
    const dir_n = @min(dir.len, need);
    @memcpy(full[0..dir_n], dir[0..dir_n]);
    const file_n = @min(filename.len, need - dir_n);
    @memcpy(full[dir_n .. dir_n + file_n], filename[0..file_n]);
    full[dir_n + file_n] = 0;
    handle = c.CreateFileA(full[0..].ptr, c.FILE_APPEND_DATA | c.GENERIC_WRITE, c.FILE_SHARE_READ | c.FILE_SHARE_WRITE, null, c.OPEN_ALWAYS, c.FILE_ATTRIBUTE_NORMAL, null);
    if (handle != c.INVALID_HANDLE_VALUE) {
        _ = c.SetFilePointer(handle, 0, null, c.FILE_END);
    }
}

pub fn setEnabled(value: bool) void {
    enabled = value;
}
pub fn isEnabled() bool {
    return enabled;
}

pub fn line(comptime fmt: []const u8, args: anytype) void {
    if (!enabled or handle == c.INVALID_HANDLE_VALUE) return;
    var body: [1536]u8 = undefined;
    const text = std.fmt.bufPrint(&body, fmt, args) catch return;
    var output: [1700]u8 = undefined;
    const pid = c.GetCurrentProcessId();
    const final = std.fmt.bufPrint(&output, "[net pid={d}] {s}\r\n", .{ pid, text }) catch return;
    if (lock_ready) c.EnterCriticalSection(&lock);
    var written: c.DWORD = 0;
    _ = c.WriteFile(handle, final.ptr, @intCast(final.len), &written, null);
    if (lock_ready) c.LeaveCriticalSection(&lock);
}

pub fn close() void {
    if (handle != c.INVALID_HANDLE_VALUE) {
        _ = c.CloseHandle(handle);
        handle = c.INVALID_HANDLE_VALUE;
    }
    if (lock_ready) {
        c.DeleteCriticalSection(&lock);
        lock_ready = false;
    }
}
