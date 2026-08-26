const std = @import("std");
const win = @import("win.zig");
const c = win.c;

const CreateMutexAFn = *const fn (
    c.LPVOID,
    c.BOOL,
    c.LPCSTR,
) callconv(.winapi) c.HANDLE;
const GetLastErrorFn = *const fn () callconv(.winapi) c.DWORD;

const error_already_exists: c.DWORD = 183;
var guard_handle: c.HANDLE = null;

/// Acquires a process-wide named guard for one game profile.
/// This also protects against the same ASI being loaded from two directories.
pub fn acquire(comptime tag: []const u8) bool {
    if (guard_handle != null) return true;

    const kernel = c.GetModuleHandleA("kernel32.dll") orelse return false;
    const create_mutex = win.proc(CreateMutexAFn, kernel, "CreateMutexA") orelse return false;
    const get_last_error = win.proc(GetLastErrorFn, kernel, "GetLastError") orelse return false;

    var name_buffer: [96]u8 = undefined;
    const name = std.fmt.bufPrintZ(
        &name_buffer,
        "Local\\NFSNet_{s}_{d}",
        .{ tag, c.GetCurrentProcessId() },
    ) catch return false;

    const handle = create_mutex(null, c.TRUE, @ptrCast(name.ptr)) orelse return false;
    if (get_last_error() == error_already_exists) {
        _ = c.CloseHandle(handle);
        return false;
    }

    // Keep the mutex alive for the entire process lifetime.
    guard_handle = handle;
    return true;
}
