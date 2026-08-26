const std = @import("std");
const win = @import("shared").win;
const config = @import("shared").config;
const logger = @import("shared").logger;
const process_guard = @import("shared").process_guard;
const RuntimeFactory = @import("shared").socket_runtime;
const state = @import("state.zig");
const profile = @import("profile.zig");
const patches = @import("patches.zig");
const lan = @import("lan.zig");
const c = win.c;
var started = false;
const Network = RuntimeFactory.Runtime(profile);

fn worker(_: c.LPVOID) callconv(.winapi) c.DWORD {
    var log_name_buffer: [64]u8 = undefined;
    const log_name = std.fmt.bufPrint(
        &log_name_buffer,
        "net_u2-trace-{d}.log",
        .{c.GetCurrentProcessId()},
    ) catch "net_u2-trace.log";
    logger.init(state.self_module, log_name);
    logger.line("net_u2 0.2.22 U2 single-port identity wrapper initialization thread entered", .{});
    var wsa: c.WSADATA = undefined;
    const wsa_result = c.WSAStartup(0x0202, &wsa);
    if (wsa_result != 0) {
        logger.line("WSAStartup failed error={d}", .{wsa_result});
        return 0;
    }
    _ = config.loadIni(state.self_module, "net_u2.ini", @as(void, {}), state.applyConfig);
    logger.setEnabled(state.log_enabled);
    state.refreshAddresses();
    logger.line("net_u2 network={} bootstrap={s}:{d} lobby={s}:{d} control={s}:{d} alias={s}:{d} race={s}:{d}", .{
        state.network_enabled,
        state.bootstrap.hostSlice(),
        state.bootstrap.port,
        state.lobby.hostSlice(),
        state.lobby.port,
        state.control.hostSlice(),
        state.control.port,
        state.control_alias.hostSlice(),
        state.control_alias.port,
        state.race.hostSlice(),
        state.race.port,
    });
    logger.line("net_u2 lan={} lobby={s}:{d} control={s}:{d} alias={s}:{d} inject={}", .{
        state.lan_enabled,
        state.lan.hostSlice(),
        state.lan.port,
        state.lan_control.hostSlice(),
        state.lan_control.port,
        state.lan_control_alias.hostSlice(),
        state.lan_control_alias.port,
        state.inject_server,
    });
    logger.line("U2 UDP TRACE enabled={}", .{state.udp_trace});
    _ = patches.install();
    _ = lan.install();
    if (!Network.init(state.self_module)) {
        logger.line("Winsock runtime initialization failed", .{});
        return 0;
    }
    var burst: u32 = 0;
    while (true) {
        Network.refresh();
        c.Sleep(if (burst < 200) 100 else 1000);
        burst += 1;
    }
}

pub export fn InitializeASI() callconv(.c) void {
    // Ultimate ASI Loader calls InitializeASI synchronously once after LoadLibrary.
    // A plain guard avoids Zig x86 lowering the imported atomic API into a
    // direct CALL to the IAT data slot, which crashes with execute error 998.
    if (started) return;
    started = true;
    if (!process_guard.acquire("u2")) return;
    const thread = c.CreateThread(null, 0, worker, null, 0, null);
    if (thread != null) {
        _ = c.CloseHandle(thread);
    }
}

pub export fn DllMain(module: c.HMODULE, reason: c.DWORD, _: c.LPVOID) callconv(.winapi) std.os.windows.BOOL {
    // Keep DllMain loader-lock safe. Ultimate ASI Loader calls InitializeASI
    // immediately after LoadLibrary succeeds, outside this entrypoint.
    if (reason == c.DLL_PROCESS_ATTACH) {
        state.self_module = module;
        _ = c.DisableThreadLibraryCalls(module);
    }
    return @enumFromInt(1);
}
