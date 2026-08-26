const std = @import("std");
const win = @import("win.zig");
const pe = @import("pe.zig");
const net = @import("net_util.zig");
const strings = @import("strings.zig");
const c = win.c;

pub fn Runtime(comptime Profile: type) type {
    return struct {
        const Self = @This();

        pub const ConnectFn = *const fn (c.SOCKET, *const c.sockaddr, c_int) callconv(.winapi) c_int;
        pub const BindFn = *const fn (c.SOCKET, *const c.sockaddr, c_int) callconv(.winapi) c_int;
        pub const WSAConnectFn = *const fn (c.SOCKET, *const c.sockaddr, c_int, c.LPWSABUF, c.LPWSABUF, c.LPQOS, c.LPQOS) callconv(.winapi) c_int;
        pub const SendFn = *const fn (c.SOCKET, [*]const u8, c_int, c_int) callconv(.winapi) c_int;
        pub const RecvFn = *const fn (c.SOCKET, [*]u8, c_int, c_int) callconv(.winapi) c_int;
        pub const SendToFn = *const fn (c.SOCKET, [*]const u8, c_int, c_int, *const c.sockaddr, c_int) callconv(.winapi) c_int;
        pub const RecvFromFn = *const fn (c.SOCKET, [*]u8, c_int, c_int, *c.sockaddr, *c_int) callconv(.winapi) c_int;
        pub const CloseSocketFn = *const fn (c.SOCKET) callconv(.winapi) c_int;
        pub const WSASendFn = *const fn (c.SOCKET, c.LPWSABUF, c.DWORD, c.LPDWORD, c.DWORD, c.LPWSAOVERLAPPED, c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int;
        pub const WSARecvFn = *const fn (c.SOCKET, c.LPWSABUF, c.DWORD, c.LPDWORD, c.LPDWORD, c.LPWSAOVERLAPPED, c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int;
        pub const WSASendToFn = *const fn (c.SOCKET, c.LPWSABUF, c.DWORD, c.LPDWORD, c.DWORD, *const c.sockaddr, c_int, c.LPWSAOVERLAPPED, c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int;
        pub const WSARecvFromFn = *const fn (c.SOCKET, c.LPWSABUF, c.DWORD, c.LPDWORD, c.LPDWORD, *c.sockaddr, *c_int, c.LPWSAOVERLAPPED, c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int;
        pub const WSAGetOverlappedResultFn = *const fn (c.SOCKET, c.LPWSAOVERLAPPED, c.LPDWORD, c.BOOL, c.LPDWORD) callconv(.winapi) c.BOOL;
        pub const WSAGetLastErrorFn = *const fn () callconv(.winapi) c_int;
        pub const GetProcAddressFn = *const fn (c.HMODULE, c.LPCSTR) callconv(.winapi) c.FARPROC;

        const Peer = struct { socket: c.SOCKET = c.INVALID_SOCKET, address: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in) };
        const PendingRecv = struct {
            overlapped: c.LPWSAOVERLAPPED = null,
            socket: c.SOCKET = c.INVALID_SOCKET,
            buffer: [*c]u8 = null,
            address: ?*c.sockaddr = null,
            address_length: ?*c_int = null,
            connected: bool = false,
        };
        var peers: [64]Peer = [_]Peer{.{}} ** 64;
        var pending_recvs: [64]PendingRecv = [_]PendingRecv{.{}} ** 64;
        var peer_lock: c.CRITICAL_SECTION = undefined;
        var peer_lock_ready = false;
        var self_module: c.HMODULE = null;

        var real_connect: ?ConnectFn = null;
        var real_bind: ?BindFn = null;
        var real_wsa_connect: ?WSAConnectFn = null;
        var real_send: ?SendFn = null;
        var real_recv: ?RecvFn = null;
        var real_sendto: ?SendToFn = null;
        var real_recvfrom: ?RecvFromFn = null;
        var real_close: ?CloseSocketFn = null;
        var real_wsa_send: ?WSASendFn = null;
        var real_wsa_recv: ?WSARecvFn = null;
        var real_wsa_sendto: ?WSASendToFn = null;
        var real_wsa_recvfrom: ?WSARecvFromFn = null;
        var real_wsa_get_overlapped_result: ?WSAGetOverlappedResultFn = null;
        var real_wsa_get_last_error: ?WSAGetLastErrorFn = null;
        var real_get_proc: ?GetProcAddressFn = null;

        fn peerSet(socket: c.SOCKET, address: *const c.sockaddr_in) void {
            if (!peer_lock_ready or socket == c.INVALID_SOCKET or address.sin_family != c.AF_INET) return;
            c.EnterCriticalSection(&peer_lock);
            defer c.LeaveCriticalSection(&peer_lock);
            var free_slot: ?usize = null;
            for (&peers, 0..) |*entry, i| {
                if (entry.socket == socket) {
                    entry.address = address.*;
                    return;
                }
                if (free_slot == null and (entry.socket == c.INVALID_SOCKET or entry.socket == 0)) {
                    free_slot = i;
                }
            }
            if (free_slot) |i| {
                peers[i] = .{ .socket = socket, .address = address.* };
            }
        }

        fn peerGet(socket: c.SOCKET, output: *c.sockaddr_in) bool {
            if (!peer_lock_ready or socket == c.INVALID_SOCKET) return false;
            c.EnterCriticalSection(&peer_lock);
            defer c.LeaveCriticalSection(&peer_lock);
            for (&peers) |*entry| if (entry.socket == socket) {
                output.* = entry.address;
                return true;
            };
            return false;
        }

        fn peerClear(socket: c.SOCKET) void {
            if (!peer_lock_ready) return;
            c.EnterCriticalSection(&peer_lock);
            defer c.LeaveCriticalSection(&peer_lock);
            for (&peers) |*entry| if (entry.socket == socket) {
                entry.* = .{};
                break;
            };
            for (&pending_recvs) |*entry| {
                if (entry.socket == socket) entry.* = .{};
            }
        }

        fn pendingRecvStore(entry: PendingRecv) void {
            if (!peer_lock_ready or entry.overlapped == null) return;
            c.EnterCriticalSection(&peer_lock);
            defer c.LeaveCriticalSection(&peer_lock);
            var free_slot: ?usize = null;
            for (&pending_recvs, 0..) |*current, i| {
                if (current.overlapped == entry.overlapped) {
                    current.* = entry;
                    return;
                }
                if (free_slot == null and current.overlapped == null) free_slot = i;
            }
            if (free_slot) |i| pending_recvs[i] = entry;
        }

        fn pendingRecvTake(overlapped: c.LPWSAOVERLAPPED, output: *PendingRecv) bool {
            if (!peer_lock_ready or overlapped == null) return false;
            c.EnterCriticalSection(&peer_lock);
            defer c.LeaveCriticalSection(&peer_lock);
            for (&pending_recvs) |*entry| {
                if (entry.overlapped == overlapped) {
                    output.* = entry.*;
                    entry.* = .{};
                    return true;
                }
            }
            return false;
        }

        fn decodePendingRecv(entry: *const PendingRecv, transferred: c.LPDWORD) void {
            if (transferred == null or transferred.* == 0 or entry.buffer == null) return;
            var decoded: c_int = @intCast(transferred.*);
            if (entry.connected) {
                const relay = Profile.relayAddress();
                var source = relay;
                var source_length: c_int = @sizeOf(c.sockaddr_in);
                decoded = Profile.recvUdp(entry.socket, @ptrCast(entry.buffer), decoded, @ptrCast(&source), &source_length, peerGet);
                if (!net.addressEquals(&source, &relay)) peerSet(entry.socket, &source);
            } else if (entry.address) |address| {
                if (entry.address_length) |address_length| {
                    decoded = Profile.recvUdp(entry.socket, @ptrCast(entry.buffer), decoded, address, address_length, peerGet);
                    if (address_length.* >= @sizeOf(c.sockaddr_in) and address.sa_family == c.AF_INET) {
                        const source_unaligned: *align(1) const c.sockaddr_in = @ptrCast(address);
                        const source = source_unaligned.*;
                        const relay = Profile.relayAddress();
                        if (!net.addressEquals(&source, &relay)) peerSet(entry.socket, &source);
                    }
                }
            }
            transferred.* = @intCast(decoded);
        }

        pub fn getPeer(socket: c.SOCKET, output: *c.sockaddr_in) bool {
            return peerGet(socket, output);
        }
        pub fn rememberPeer(socket: c.SOCKET, address: *const c.sockaddr_in) void {
            peerSet(socket, address);
        }

        pub fn rawSendTo(socket: c.SOCKET, data: [*]const u8, length: c_int, flags: c_int, target: *const c.sockaddr_in) c_int {
            const function = real_sendto orelse return c.SOCKET_ERROR;
            return function(socket, data, length, flags, @ptrCast(target), @sizeOf(c.sockaddr_in));
        }

        fn resolveFunctions() bool {
            const kernel = c.GetModuleHandleA("kernel32.dll") orelse return false;
            real_get_proc = win.proc(GetProcAddressFn, kernel, "GetProcAddress");
            var ws = c.GetModuleHandleA("ws2_32.dll");
            if (ws == null) {
                ws = c.LoadLibraryA("ws2_32.dll");
            }
            if (ws == null) return false;
            real_connect = win.proc(ConnectFn, ws, "connect");
            real_bind = win.proc(BindFn, ws, "bind");
            real_wsa_connect = win.proc(WSAConnectFn, ws, "WSAConnect");
            real_send = win.proc(SendFn, ws, "send");
            real_recv = win.proc(RecvFn, ws, "recv");
            real_sendto = win.proc(SendToFn, ws, "sendto");
            real_recvfrom = win.proc(RecvFromFn, ws, "recvfrom");
            real_close = win.proc(CloseSocketFn, ws, "closesocket");
            real_wsa_send = win.proc(WSASendFn, ws, "WSASend");
            real_wsa_recv = win.proc(WSARecvFn, ws, "WSARecv");
            real_wsa_sendto = win.proc(WSASendToFn, ws, "WSASendTo");
            real_wsa_recvfrom = win.proc(WSARecvFromFn, ws, "WSARecvFrom");
            real_wsa_get_overlapped_result = win.proc(WSAGetOverlappedResultFn, ws, "WSAGetOverlappedResult");
            real_wsa_get_last_error = win.proc(WSAGetLastErrorFn, ws, "WSAGetLastError");
            return real_connect != null and real_bind != null and real_sendto != null and real_recvfrom != null;
        }

        fn myBind(socket: c.SOCKET, address: *const c.sockaddr, length: c_int) callconv(.winapi) c_int {
            const function = real_bind orelse return c.SOCKET_ERROR;
            if (net.isSocketType(socket, c.SOCK_DGRAM) and length >= @sizeOf(c.sockaddr_in) and address.sa_family == c.AF_INET and @hasDecl(Profile, "udpBindRedirect")) {
                const original = @as(*align(1) const c.sockaddr_in, @ptrCast(address)).*;
                var redirect: c.sockaddr_in = undefined;
                if (Profile.udpBindRedirect(&original, &redirect)) {
                    return function(socket, @ptrCast(&redirect), @sizeOf(c.sockaddr_in));
                }
            }
            return function(socket, address, length);
        }

        fn myConnect(socket: c.SOCKET, address: *const c.sockaddr, length: c_int) callconv(.winapi) c_int {
            const function = real_connect orelse return c.SOCKET_ERROR;
            if (length >= @sizeOf(c.sockaddr_in) and address.sa_family == c.AF_INET) {
                const original = @as(*align(1) const c.sockaddr_in, @ptrCast(address)).*;
                if (net.isSocketType(socket, c.SOCK_DGRAM)) {
                    peerSet(socket, &original);
                    var redirect: c.sockaddr_in = undefined;
                    if (Profile.udpConnectRedirect(socket, &original, &redirect))
                        return function(socket, @ptrCast(&redirect), @sizeOf(c.sockaddr_in));
                } else if (net.isSocketType(socket, c.SOCK_STREAM)) {
                    var redirect: c.sockaddr_in = undefined;
                    if (Profile.tcpRedirect(&original, &redirect))
                        return function(socket, @ptrCast(&redirect), @sizeOf(c.sockaddr_in));
                }
            }
            return function(socket, address, length);
        }

        fn myWSAConnect(socket: c.SOCKET, address: *const c.sockaddr, length: c_int, caller: c.LPWSABUF, callee: c.LPWSABUF, sqos: c.LPQOS, gqos: c.LPQOS) callconv(.winapi) c_int {
            const function = real_wsa_connect orelse return c.SOCKET_ERROR;
            if (length >= @sizeOf(c.sockaddr_in) and address.sa_family == c.AF_INET) {
                const original = @as(*align(1) const c.sockaddr_in, @ptrCast(address)).*;
                if (net.isSocketType(socket, c.SOCK_DGRAM)) {
                    peerSet(socket, &original);
                    var redirect: c.sockaddr_in = undefined;
                    if (Profile.udpConnectRedirect(socket, &original, &redirect))
                        return function(socket, @ptrCast(&redirect), @sizeOf(c.sockaddr_in), caller, callee, sqos, gqos);
                } else if (net.isSocketType(socket, c.SOCK_STREAM)) {
                    var redirect: c.sockaddr_in = undefined;
                    if (Profile.tcpRedirect(&original, &redirect))
                        return function(socket, @ptrCast(&redirect), @sizeOf(c.sockaddr_in), caller, callee, sqos, gqos);
                }
            }
            return function(socket, address, length, caller, callee, sqos, gqos);
        }

        fn mySendTo(socket: c.SOCKET, data: [*]const u8, length: c_int, flags: c_int, address: *const c.sockaddr, address_length: c_int) callconv(.winapi) c_int {
            const function = real_sendto orelse return c.SOCKET_ERROR;
            if (!net.isSocketType(socket, c.SOCK_DGRAM) or length <= 0)
                return function(socket, data, length, flags, address, address_length);
            var peer: c.sockaddr_in = undefined;
            if (address_length >= @sizeOf(c.sockaddr_in) and address.sa_family == c.AF_INET) {
                peer = @as(*align(1) const c.sockaddr_in, @ptrCast(address)).*;
                const relay = Profile.relayAddress();
                if (!net.addressEquals(&peer, &relay)) peerSet(socket, &peer);
            } else if (!peerGet(socket, &peer)) {
                return function(socket, data, length, flags, address, address_length);
            }
            if (@hasDecl(Profile, "normalizeUdpPeer")) Profile.normalizeUdpPeer(socket, &peer, peerGet);
            if (@hasDecl(Profile, "beforeUdpSend")) {
                if (Profile.beforeUdpSend(socket, data, length, flags, &peer, function)) |handled| return handled;
            }
            return Profile.sendUdp(socket, data, length, flags, &peer, function);
        }

        fn mySend(socket: c.SOCKET, data: [*]const u8, length: c_int, flags: c_int) callconv(.winapi) c_int {
            const function = real_send orelse return c.SOCKET_ERROR;
            if (!net.isSocketType(socket, c.SOCK_DGRAM) or length <= 0) return function(socket, data, length, flags);
            var peer: c.sockaddr_in = undefined;
            if (!peerGet(socket, &peer)) return function(socket, data, length, flags);
            const sendto_function = real_sendto orelse return function(socket, data, length, flags);
            if (@hasDecl(Profile, "normalizeUdpPeer")) Profile.normalizeUdpPeer(socket, &peer, peerGet);
            return Profile.sendUdp(socket, data, length, flags, &peer, sendto_function);
        }

        fn myRecvFrom(socket: c.SOCKET, data: [*]u8, capacity: c_int, flags: c_int, address: *c.sockaddr, address_length: *c_int) callconv(.winapi) c_int {
            const function = real_recvfrom orelse return c.SOCKET_ERROR;
            var result = function(socket, data, capacity, flags, address, address_length);
            if (result > 0) {
                if (net.isSocketType(socket, c.SOCK_DGRAM)) {
                    result = Profile.recvUdp(socket, data, result, address, address_length, peerGet);
                    if (address_length.* >= @sizeOf(c.sockaddr_in) and address.sa_family == c.AF_INET) {
                        const source_unaligned: *align(1) const c.sockaddr_in = @ptrCast(address);
                        const source = source_unaligned.*;
                        const relay = Profile.relayAddress();
                        if (!net.addressEquals(&source, &relay)) peerSet(socket, &source);
                    }
                } else Profile.tcpData(socket, data[0..@intCast(result)]);
            }
            return result;
        }

        fn myRecv(socket: c.SOCKET, data: [*]u8, capacity: c_int, flags: c_int) callconv(.winapi) c_int {
            const function = real_recv orelse return c.SOCKET_ERROR;
            var result = function(socket, data, capacity, flags);
            if (result > 0) {
                if (net.isSocketType(socket, c.SOCK_DGRAM)) {
                    var source = Profile.relayAddress();
                    const relay = source;
                    var source_length: c_int = @sizeOf(c.sockaddr_in);
                    result = Profile.recvUdp(socket, data, result, @ptrCast(&source), &source_length, peerGet);
                    if (!net.addressEquals(&source, &relay)) peerSet(socket, &source);
                } else Profile.tcpData(socket, data[0..@intCast(result)]);
            }
            return result;
        }

        fn myClose(socket: c.SOCKET) callconv(.winapi) c_int {
            peerClear(socket);
            if (@hasDecl(Profile, "socketClosed")) Profile.socketClosed(socket);
            const function = real_close orelse return c.SOCKET_ERROR;
            return function(socket);
        }

        fn myWSASendTo(socket: c.SOCKET, buffers: c.LPWSABUF, count: c.DWORD, sent: c.LPDWORD, flags: c.DWORD, address: *const c.sockaddr, address_length: c_int, overlapped: c.LPWSAOVERLAPPED, completion: c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int {
            const original = real_wsa_sendto orelse return c.SOCKET_ERROR;
            if (overlapped != null or completion != null or count != 1 or buffers == null or buffers[0].buf == null)
                return original(socket, buffers, count, sent, flags, address, address_length, overlapped, completion);
            const result = mySendTo(socket, @ptrCast(buffers[0].buf), @intCast(buffers[0].len), @intCast(flags), address, address_length);
            if (result == c.SOCKET_ERROR) return c.SOCKET_ERROR;
            if (sent != null) {
                sent.* = @intCast(result);
            }
            return 0;
        }

        fn myWSASend(socket: c.SOCKET, buffers: c.LPWSABUF, count: c.DWORD, sent: c.LPDWORD, flags: c.DWORD, overlapped: c.LPWSAOVERLAPPED, completion: c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int {
            const original = real_wsa_send orelse return c.SOCKET_ERROR;
            if (overlapped != null or completion != null or count != 1 or buffers == null or buffers[0].buf == null)
                return original(socket, buffers, count, sent, flags, overlapped, completion);
            const result = mySend(socket, @ptrCast(buffers[0].buf), @intCast(buffers[0].len), @intCast(flags));
            if (result == c.SOCKET_ERROR) return c.SOCKET_ERROR;
            if (sent != null) {
                sent.* = @intCast(result);
            }
            return 0;
        }

        fn myWSARecvFrom(socket: c.SOCKET, buffers: c.LPWSABUF, count: c.DWORD, received: c.LPDWORD, flags: c.LPDWORD, address: *c.sockaddr, address_length: *c_int, overlapped: c.LPWSAOVERLAPPED, completion: c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int {
            const original = real_wsa_recvfrom orelse return c.SOCKET_ERROR;
            const track = net.isSocketType(socket, c.SOCK_DGRAM) and completion == null and overlapped != null and count == 1 and buffers != null and buffers[0].buf != null;
            if (track) pendingRecvStore(.{
                .overlapped = overlapped,
                .socket = socket,
                .buffer = buffers[0].buf,
                .address = address,
                .address_length = address_length,
                .connected = false,
            });
            const result = original(socket, buffers, count, received, flags, address, address_length, overlapped, completion);
            if (result == 0 and received != null and received.* > 0 and count == 1 and buffers != null and buffers[0].buf != null) {
                if (net.isSocketType(socket, c.SOCK_DGRAM)) {
                    var pending: PendingRecv = .{};
                    if (track and pendingRecvTake(overlapped, &pending)) {
                        decodePendingRecv(&pending, received);
                    } else {
                        received.* = @intCast(Profile.recvUdp(socket, @ptrCast(buffers[0].buf), @intCast(received.*), address, address_length, peerGet));
                        if (address_length.* >= @sizeOf(c.sockaddr_in) and address.sa_family == c.AF_INET) {
                            const source_unaligned: *align(1) const c.sockaddr_in = @ptrCast(address);
                            const source = source_unaligned.*;
                            const relay = Profile.relayAddress();
                            if (!net.addressEquals(&source, &relay)) peerSet(socket, &source);
                        }
                    }
                } else Profile.tcpData(socket, @as([*]u8, @ptrCast(buffers[0].buf))[0..@as(usize, @intCast(received.*))]);
            } else if (result != 0 and track) {
                const get_error = real_wsa_get_last_error;
                if (get_error == null or get_error.?() != 997) {
                    var failed: PendingRecv = .{};
                    _ = pendingRecvTake(overlapped, &failed);
                }
            }
            return result;
        }

        fn myWSARecv(socket: c.SOCKET, buffers: c.LPWSABUF, count: c.DWORD, received: c.LPDWORD, flags: c.LPDWORD, overlapped: c.LPWSAOVERLAPPED, completion: c.LPWSAOVERLAPPED_COMPLETION_ROUTINE) callconv(.winapi) c_int {
            const original = real_wsa_recv orelse return c.SOCKET_ERROR;
            const track = net.isSocketType(socket, c.SOCK_DGRAM) and completion == null and overlapped != null and count == 1 and buffers != null and buffers[0].buf != null;
            if (track) pendingRecvStore(.{
                .overlapped = overlapped,
                .socket = socket,
                .buffer = buffers[0].buf,
                .connected = true,
            });
            const result = original(socket, buffers, count, received, flags, overlapped, completion);
            if (result == 0 and received != null and received.* > 0 and count == 1 and buffers != null and buffers[0].buf != null) {
                if (net.isSocketType(socket, c.SOCK_DGRAM)) {
                    var pending: PendingRecv = .{};
                    if (track and pendingRecvTake(overlapped, &pending)) {
                        decodePendingRecv(&pending, received);
                    } else {
                        var source = Profile.relayAddress();
                        const relay = source;
                        var source_length: c_int = @sizeOf(c.sockaddr_in);
                        received.* = @intCast(Profile.recvUdp(socket, @ptrCast(buffers[0].buf), @intCast(received.*), @ptrCast(&source), &source_length, peerGet));
                        if (!net.addressEquals(&source, &relay)) peerSet(socket, &source);
                    }
                } else Profile.tcpData(socket, @as([*]u8, @ptrCast(buffers[0].buf))[0..@as(usize, @intCast(received.*))]);
            } else if (result != 0 and track) {
                const get_error = real_wsa_get_last_error;
                if (get_error == null or get_error.?() != 997) {
                    var failed: PendingRecv = .{};
                    _ = pendingRecvTake(overlapped, &failed);
                }
            }
            return result;
        }

        fn myWSAGetOverlappedResult(socket: c.SOCKET, overlapped: c.LPWSAOVERLAPPED, transferred: c.LPDWORD, wait: c.BOOL, flags: c.LPDWORD) callconv(.winapi) c.BOOL {
            const original = real_wsa_get_overlapped_result orelse return c.FALSE;
            const result = original(socket, overlapped, transferred, wait, flags);
            if (result == c.FALSE) return result;
            var pending: PendingRecv = .{};
            if (pendingRecvTake(overlapped, &pending)) decodePendingRecv(&pending, transferred);
            return result;
        }

        fn farProc(pointer: anytype) c.FARPROC {
            return @ptrCast(pointer);
        }

        fn myGetProcAddress(module: c.HMODULE, raw_name: c.LPCSTR) callconv(.winapi) c.FARPROC {
            const original = real_get_proc orelse return null;
            const fallback = original(module, raw_name);
            if (raw_name == null) return fallback;
            const name_pointer: [*c]const u8 = raw_name;
            const address = @intFromPtr(name_pointer);
            if ((address >> 16) == 0) {
                return switch (@as(u16, @truncate(address))) {
                    2 => farProc(&myBind),
                    3 => farProc(&myClose),
                    4 => farProc(&myConnect),
                    16 => farProc(&myRecv),
                    17 => farProc(&myRecvFrom),
                    19 => farProc(&mySend),
                    20 => farProc(&mySendTo),
                    else => fallback,
                };
            }
            const name = std.mem.span(@as([*:0]const u8, @ptrCast(name_pointer)));
            if (strings.eqlIgnoreCase(name, "closesocket")) return farProc(&myClose);
            if (strings.eqlIgnoreCase(name, "bind")) return farProc(&myBind);
            if (strings.eqlIgnoreCase(name, "connect")) return farProc(&myConnect);
            if (strings.eqlIgnoreCase(name, "WSAConnect")) return farProc(&myWSAConnect);
            if (strings.eqlIgnoreCase(name, "send")) return farProc(&mySend);
            if (strings.eqlIgnoreCase(name, "recv")) return farProc(&myRecv);
            if (strings.eqlIgnoreCase(name, "sendto")) return farProc(&mySendTo);
            if (strings.eqlIgnoreCase(name, "recvfrom")) return farProc(&myRecvFrom);
            if (strings.eqlIgnoreCase(name, "WSASend")) return farProc(&myWSASend);
            if (strings.eqlIgnoreCase(name, "WSARecv")) return farProc(&myWSARecv);
            if (strings.eqlIgnoreCase(name, "WSASendTo")) return farProc(&myWSASendTo);
            if (strings.eqlIgnoreCase(name, "WSARecvFrom")) return farProc(&myWSARecvFrom);
            if (strings.eqlIgnoreCase(name, "WSAGetOverlappedResult")) return farProc(&myWSAGetOverlappedResult);
            return fallback;
        }

        fn hookModule(module: c.HMODULE) void {
            _ = pe.hookIat(module, "ws2_32.dll", "bind", 2, @ptrCast(&myBind), null);
            _ = pe.hookIat(module, "wsock32.dll", "bind", 2, @ptrCast(&myBind), null);
            _ = pe.hookIat(module, "ws2_32.dll", "closesocket", 3, @ptrCast(&myClose), null);
            _ = pe.hookIat(module, "wsock32.dll", "closesocket", 3, @ptrCast(&myClose), null);
            _ = pe.hookIat(module, "ws2_32.dll", "connect", 4, @ptrCast(&myConnect), null);
            _ = pe.hookIat(module, "wsock32.dll", "connect", 4, @ptrCast(&myConnect), null);
            _ = pe.hookIat(module, "ws2_32.dll", "recv", 16, @ptrCast(&myRecv), null);
            _ = pe.hookIat(module, "wsock32.dll", "recv", 16, @ptrCast(&myRecv), null);
            _ = pe.hookIat(module, "ws2_32.dll", "recvfrom", 17, @ptrCast(&myRecvFrom), null);
            _ = pe.hookIat(module, "wsock32.dll", "recvfrom", 17, @ptrCast(&myRecvFrom), null);
            _ = pe.hookIat(module, "ws2_32.dll", "send", 19, @ptrCast(&mySend), null);
            _ = pe.hookIat(module, "wsock32.dll", "send", 19, @ptrCast(&mySend), null);
            _ = pe.hookIat(module, "ws2_32.dll", "sendto", 20, @ptrCast(&mySendTo), null);
            _ = pe.hookIat(module, "wsock32.dll", "sendto", 20, @ptrCast(&mySendTo), null);
            _ = pe.hookIat(module, "ws2_32.dll", "WSAConnect", 0, @ptrCast(&myWSAConnect), null);
            _ = pe.hookIat(module, "ws2_32.dll", "WSASend", 0, @ptrCast(&myWSASend), null);
            _ = pe.hookIat(module, "ws2_32.dll", "WSARecv", 0, @ptrCast(&myWSARecv), null);
            _ = pe.hookIat(module, "ws2_32.dll", "WSASendTo", 0, @ptrCast(&myWSASendTo), null);
            _ = pe.hookIat(module, "ws2_32.dll", "WSARecvFrom", 0, @ptrCast(&myWSARecvFrom), null);
            _ = pe.hookIat(module, "ws2_32.dll", "WSAGetOverlappedResult", 0, @ptrCast(&myWSAGetOverlappedResult), null);
            _ = pe.hookIat(module, "kernel32.dll", "GetProcAddress", 0, @ptrCast(&myGetProcAddress), null);
            _ = pe.hookIat(module, "kernelbase.dll", "GetProcAddress", 0, @ptrCast(&myGetProcAddress), null);
        }

        pub fn init(module: c.HMODULE) bool {
            self_module = module;
            if (!peer_lock_ready) {
                c.InitializeCriticalSection(&peer_lock);
                peer_lock_ready = true;
                for (&peers) |*entry| entry.socket = c.INVALID_SOCKET;
                for (&pending_recvs) |*entry| entry.* = .{};
            }
            if (!resolveFunctions()) return false;
            pe.hookLoadedModules(self_module, hookModule);
            return true;
        }

        pub fn refresh() void {
            pe.hookLoadedModules(self_module, hookModule);
        }

        pub fn shutdown() void {
            if (peer_lock_ready) {
                c.DeleteCriticalSection(&peer_lock);
                peer_lock_ready = false;
            }
        }
    };
}
