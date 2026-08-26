const std = @import("std");

// Minimal Win32/Winsock declarations used by this project.
// Keeping these declarations in Zig avoids translating the full MinGW Windows
// headers, which currently fails for the x86-windows-gnu target in Zig 0.16.
pub const c = @This();

pub const BOOL = i32;
pub const BYTE = u8;
pub const WORD = u16;
pub const DWORD = u32;
pub const LONG = i32;
pub const ULONG = u32;
pub const SIZE_T = usize;
pub const SOCKET = usize;

pub const HANDLE = ?*anyopaque;
pub const HMODULE = HANDLE;
pub const LPVOID = ?*anyopaque;
pub const LPCVOID = ?*const anyopaque;
pub const LPCSTR = [*c]const u8;
pub const LPDWORD = [*c]DWORD;
pub const FARPROC = ?*const fn () callconv(.winapi) void;

pub const TRUE: BOOL = 1;
pub const FALSE: BOOL = 0;
pub const DLL_PROCESS_DETACH: DWORD = 0;
pub const DLL_PROCESS_ATTACH: DWORD = 1;

pub const MAX_PATH: usize = 260;
pub const INVALID_HANDLE_VALUE: HANDLE = @as(*anyopaque, @ptrFromInt(std.math.maxInt(usize)));
pub const INVALID_SOCKET: SOCKET = std.math.maxInt(SOCKET);
pub const SOCKET_ERROR: c_int = -1;

pub const GENERIC_READ: DWORD = 0x80000000;
pub const GENERIC_WRITE: DWORD = 0x40000000;
pub const FILE_APPEND_DATA: DWORD = 0x00000004;
pub const FILE_SHARE_READ: DWORD = 0x00000001;
pub const FILE_SHARE_WRITE: DWORD = 0x00000002;
pub const OPEN_EXISTING: DWORD = 3;
pub const OPEN_ALWAYS: DWORD = 4;
pub const FILE_ATTRIBUTE_NORMAL: DWORD = 0x00000080;
pub const FILE_END: DWORD = 2;

pub const PAGE_NOACCESS: DWORD = 0x01;
pub const PAGE_READWRITE: DWORD = 0x04;
pub const PAGE_WRITECOPY: DWORD = 0x08;
pub const PAGE_EXECUTE_READWRITE: DWORD = 0x40;
pub const PAGE_EXECUTE_WRITECOPY: DWORD = 0x80;
pub const PAGE_GUARD: DWORD = 0x100;
pub const MEM_COMMIT: DWORD = 0x1000;
pub const MEM_RESERVE: DWORD = 0x2000;

pub const TH32CS_SNAPMODULE: DWORD = 0x00000008;

pub const AF_INET: u16 = 2;
pub const SOCK_STREAM: c_int = 1;
pub const SOCK_DGRAM: c_int = 2;
pub const SOL_SOCKET: c_int = 0xffff;
pub const SO_TYPE: c_int = 0x1008;
pub const INADDR_NONE: u32 = 0xffffffff;
pub const INADDR_BROADCAST: u32 = 0xffffffff;

pub const sockaddr = extern struct {
    sa_family: u16,
    sa_data: [14]u8,
};

pub const sockaddr_in = extern struct {
    sin_family: u16,
    sin_port: u16,
    sin_addr: u32,
    sin_zero: [8]u8,
};

pub const hostent = extern struct {
    h_name: [*c]u8,
    h_aliases: [*c][*c]u8,
    h_addrtype: i16,
    h_length: i16,
    h_addr_list: [*c][*c]u8,
};

pub const WSADATA = extern struct {
    wVersion: WORD,
    wHighVersion: WORD,
    szDescription: [257]u8,
    szSystemStatus: [129]u8,
    iMaxSockets: WORD,
    iMaxUdpDg: WORD,
    lpVendorInfo: [*c]u8,
};

pub const WSABUF = extern struct {
    len: ULONG,
    buf: [*c]u8,
};

pub const LPWSABUF = [*c]WSABUF;
pub const LPQOS = ?*anyopaque;
pub const LPWSAOVERLAPPED = ?*anyopaque;
pub const LPWSAOVERLAPPED_COMPLETION_ROUTINE = ?*const fn (
    DWORD,
    DWORD,
    LPWSAOVERLAPPED,
    DWORD,
) callconv(.winapi) void;

pub const CRITICAL_SECTION = extern struct {
    DebugInfo: ?*anyopaque,
    LockCount: LONG,
    RecursionCount: LONG,
    OwningThread: HANDLE,
    LockSemaphore: HANDLE,
    SpinCount: usize,
};

pub const MEMORY_BASIC_INFORMATION = extern struct {
    BaseAddress: LPVOID,
    AllocationBase: LPVOID,
    AllocationProtect: DWORD,
    RegionSize: SIZE_T,
    State: DWORD,
    Protect: DWORD,
    Type: DWORD,
};

pub const MODULEENTRY32 = extern struct {
    dwSize: DWORD,
    th32ModuleID: DWORD,
    th32ProcessID: DWORD,
    GlblcntUsage: DWORD,
    ProccntUsage: DWORD,
    modBaseAddr: ?[*]u8,
    modBaseSize: DWORD,
    hModule: HMODULE,
    szModule: [256]u8,
    szExePath: [MAX_PATH]u8,
};

pub const ThreadProc = *const fn (LPVOID) callconv(.winapi) DWORD;

pub extern "kernel32" fn VirtualProtect(
    address: LPVOID,
    size: SIZE_T,
    new_protect: DWORD,
    old_protect: *DWORD,
) callconv(.winapi) BOOL;
pub extern "kernel32" fn VirtualAlloc(
    address: LPVOID,
    size: SIZE_T,
    allocation_type: DWORD,
    protect: DWORD,
) callconv(.winapi) LPVOID;
pub extern "kernel32" fn VirtualQuery(
    address: LPCVOID,
    buffer: *MEMORY_BASIC_INFORMATION,
    length: SIZE_T,
) callconv(.winapi) SIZE_T;
pub extern "kernel32" fn FlushInstructionCache(
    process: HANDLE,
    base_address: LPCVOID,
    size: SIZE_T,
) callconv(.winapi) BOOL;
pub extern "kernel32" fn GetCurrentProcess() callconv(.winapi) HANDLE;
pub extern "kernel32" fn GetCurrentProcessId() callconv(.winapi) DWORD;
pub extern "kernel32" fn GetTickCount() callconv(.winapi) DWORD;
pub extern "kernel32" fn Sleep(milliseconds: DWORD) callconv(.winapi) void;

pub extern "kernel32" fn GetModuleHandleA(name: LPCSTR) callconv(.winapi) HMODULE;
pub extern "kernel32" fn LoadLibraryA(name: LPCSTR) callconv(.winapi) HMODULE;
pub extern "kernel32" fn GetModuleFileNameA(
    module: HMODULE,
    filename: [*c]u8,
    size: DWORD,
) callconv(.winapi) DWORD;
pub extern "kernel32" fn GetProcAddress(module: HMODULE, name: LPCSTR) callconv(.winapi) FARPROC;
pub extern "kernel32" fn DisableThreadLibraryCalls(module: HMODULE) callconv(.winapi) BOOL;

pub extern "kernel32" fn CreateThread(
    attributes: LPVOID,
    stack_size: SIZE_T,
    start_address: ThreadProc,
    parameter: LPVOID,
    creation_flags: DWORD,
    thread_id: LPDWORD,
) callconv(.winapi) HANDLE;
pub extern "kernel32" fn CreateFileA(
    filename: LPCSTR,
    desired_access: DWORD,
    share_mode: DWORD,
    security_attributes: LPVOID,
    creation_disposition: DWORD,
    flags_and_attributes: DWORD,
    template_file: HANDLE,
) callconv(.winapi) HANDLE;
pub extern "kernel32" fn ReadFile(
    file: HANDLE,
    buffer: LPVOID,
    bytes_to_read: DWORD,
    bytes_read: LPDWORD,
    overlapped: LPVOID,
) callconv(.winapi) BOOL;
pub extern "kernel32" fn WriteFile(
    file: HANDLE,
    buffer: LPCVOID,
    bytes_to_write: DWORD,
    bytes_written: LPDWORD,
    overlapped: LPVOID,
) callconv(.winapi) BOOL;
pub extern "kernel32" fn SetFilePointer(
    file: HANDLE,
    distance_low: LONG,
    distance_high: ?*LONG,
    move_method: DWORD,
) callconv(.winapi) DWORD;
pub extern "kernel32" fn CloseHandle(handle: HANDLE) callconv(.winapi) BOOL;

pub extern "kernel32" fn InitializeCriticalSection(section: *CRITICAL_SECTION) callconv(.winapi) void;
pub extern "kernel32" fn EnterCriticalSection(section: *CRITICAL_SECTION) callconv(.winapi) void;
pub extern "kernel32" fn LeaveCriticalSection(section: *CRITICAL_SECTION) callconv(.winapi) void;
pub extern "kernel32" fn DeleteCriticalSection(section: *CRITICAL_SECTION) callconv(.winapi) void;

pub extern "kernel32" fn CreateToolhelp32Snapshot(flags: DWORD, process_id: DWORD) callconv(.winapi) HANDLE;
pub extern "kernel32" fn Module32First(snapshot: HANDLE, entry: *MODULEENTRY32) callconv(.winapi) BOOL;
pub extern "kernel32" fn Module32Next(snapshot: HANDLE, entry: *MODULEENTRY32) callconv(.winapi) BOOL;

pub extern "kernel32" fn GetProcessHeap() callconv(.winapi) HANDLE;
pub extern "kernel32" fn HeapAlloc(heap: HANDLE, flags: DWORD, bytes: SIZE_T) callconv(.winapi) LPVOID;
pub extern "kernel32" fn HeapFree(heap: HANDLE, flags: DWORD, memory: LPVOID) callconv(.winapi) BOOL;

pub extern "ws2_32" fn WSAStartup(version_requested: WORD, data: *WSADATA) callconv(.winapi) c_int;
pub extern "ws2_32" fn WSACleanup() callconv(.winapi) c_int;
pub extern "ws2_32" fn htons(host_short: u16) callconv(.winapi) u16;
pub extern "ws2_32" fn ntohs(net_short: u16) callconv(.winapi) u16;
pub extern "ws2_32" fn ntohl(net_long: u32) callconv(.winapi) u32;
pub extern "ws2_32" fn inet_addr(cp: LPCSTR) callconv(.winapi) u32;
pub extern "ws2_32" fn gethostbyname(name: LPCSTR) callconv(.winapi) ?*hostent;
pub extern "ws2_32" fn getsockopt(
    socket: SOCKET,
    level: c_int,
    option_name: c_int,
    option_value: [*c]u8,
    option_length: *c_int,
) callconv(.winapi) c_int;
pub extern "ws2_32" fn getpeername(
    socket: SOCKET,
    name: *sockaddr,
    name_length: *c_int,
) callconv(.winapi) c_int;
pub extern "ws2_32" fn getsockname(
    socket: SOCKET,
    name: *sockaddr,
    name_length: *c_int,
) callconv(.winapi) c_int;

pub const WINAPI: std.builtin.CallingConvention = .winapi;
pub const CDECL: std.builtin.CallingConvention = .c;
pub const FASTCALL: std.builtin.CallingConvention = .{ .x86_fastcall = .{} };
pub const THISCALL: std.builtin.CallingConvention = .{ .x86_thiscall = .{} };
pub const THISCALL_MINGW: std.builtin.CallingConvention = .{ .x86_thiscall = .{} };

pub inline fn ptrAdd(ptr: anytype, offset: usize) [*]u8 {
    return @ptrFromInt(@intFromPtr(ptr) + offset);
}

pub inline fn constPtrAdd(ptr: anytype, offset: usize) [*]const u8 {
    return @ptrFromInt(@intFromPtr(ptr) + offset);
}

pub inline fn moduleBase(module: HMODULE) ?[*]u8 {
    const handle = module orelse return null;
    return @ptrFromInt(@intFromPtr(handle));
}

pub fn proc(comptime T: type, module: HMODULE, name: [*:0]const u8) ?T {
    if (module == null) return null;
    const raw = GetProcAddress(module, @ptrCast(name)) orelse return null;
    return @ptrCast(raw);
}
