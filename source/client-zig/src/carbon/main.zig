const std = @import("std");
const win = @import("shared").win;
const config = @import("shared").config;
const logger = @import("shared").logger;
const strings = @import("shared").strings;
const process_guard = @import("shared").process_guard;
const state = @import("state.zig");
const online = @import("online.zig");
const mad = @import("mad.zig");
const virus = @import("virus.zig");
const c = win.c;
var started = false;

fn mainThread(_: c.LPVOID) callconv(.winapi) c.DWORD {
    logger.init(state.self_module, "net_carbon.log");
    logger.line("net_carbon 0.2.21 initialization thread entered", .{});
    _ = config.loadIni(state.self_module, "net_carbon.ini", @as(void, {}), state.applyConfig);
    logger.setEnabled(state.log_enabled);
    logger.line("net_carbon network={} plasma={s} messenger={s}:{d} mad={} mad_server={s}:{d} virus={}", .{
        state.online_enabled,
        strings.sliceZ(state.plasma_host[0..]),
        strings.sliceZ(state.messenger_host[0..]),
        state.messenger_port,
        state.mad_enabled,
        strings.sliceZ(state.mad_host[0..]),
        state.mad_port,
        state.virus_enabled,
    });
    state.game_base = if (c.GetModuleHandleA("nfsc.exe")) |module| @ptrCast(module) else blk: {
        const module = c.GetModuleHandleA(null) orelse return 0;
        break :blk @ptrCast(module);
    };
    var online_ready = !state.online_enabled;
    var mad_ready = !state.mad_enabled;
    var virus_ready = !state.virus_enabled;
    var attempt: usize = 0;
    while (attempt < 600 and !(online_ready and mad_ready and virus_ready)) : (attempt += 1) {
        if (!online_ready) {
            online_ready = online.install();
        }
        if (!mad_ready and mad.signaturesReady()) {
            mad_ready = mad.install();
        }
        if (!virus_ready) {
            virus_ready = virus.install();
        }
        if (!(online_ready and mad_ready and virus_ready)) c.Sleep(50);
    }
    if (online_ready and mad_ready and virus_ready) logger.line("net_carbon ready", .{}) else logger.line("net_carbon incomplete online={} mad={} virus={}", .{ online_ready, mad_ready, virus_ready });
    return 0;
}

pub export fn InitializeASI() callconv(.c) void {
    // Ultimate ASI Loader calls InitializeASI synchronously once after LoadLibrary.
    // A plain guard avoids Zig x86 lowering the imported atomic API into a
    // direct CALL to the IAT data slot, which crashes with execute error 998.
    if (started) return;
    started = true;
    if (!process_guard.acquire("carbon")) return;
    const thread = c.CreateThread(null, 0, mainThread, null, 0, null);
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
