const win = @import("shared").win;
const memory = @import("shared").memory;
const logger = @import("shared").logger;
const strings = @import("shared").strings;
const state = @import("state.zig");
const c = win.c;

const rva_conn_manager: usize = 0x007CB344;
const rva_callsite: usize = 0x0053E219;
const rva_scheme: usize = 0x00622758;
const rva_non_ssl: usize = 0x0054D874;
const rva_skip_ssl: usize = 0x00538F9C;

fn address(rva: usize) usize {
    return @intFromPtr(state.game_base.?) + rva;
}

fn hookStuffOverrides() callconv(.winapi) c_int {
    const root_address = address(rva_conn_manager);
    const root = @as(*align(1) ?[*]u8, @ptrFromInt(root_address)).* orelse return 0;
    const values = root + 0x4c;
    @as(*align(1) u32, @ptrCast(values + 0x08)).* = @as(u32, state.messenger_port);
    @as(*align(1) u32, @ptrCast(values + 0x10)).* |= 1;
    strings.copyZ((values + 0x58)[0..64], strings.sliceZ(state.plasma_host[0..]));
    strings.copyZ((values + 0x98)[0..64], strings.sliceZ(state.messenger_host[0..]));
    strings.copyZ((values + 0x118)[0..64], strings.sliceZ(state.http_base[0..]));
    strings.copyZ((values + 0x398)[0..32], strings.sliceZ(state.platform[0..]));
    if (state.disable_gm_demangler) {
        @as(*align(1) u32, @ptrCast(values + 0x42c)).* = 0;
    }
    return @intCast(@intFromPtr(values));
}

pub fn install() bool {
    if (!state.online_enabled) return true;
    const callsite: [*]u8 = @ptrFromInt(address(rva_callsite));
    if (callsite[0] != 0xe8) return false;
    if (!memory.patchRelCall(@ptrCast(callsite), @ptrCast(&hookStuffOverrides))) return false;
    if (!memory.protectWrite(@ptrFromInt(address(rva_skip_ssl)), &[_]u8{0xeb})) return false;
    if (!memory.protectWrite(@ptrFromInt(address(rva_non_ssl)), &[_]u8{ 0x33, 0xff, 0x90, 0x90, 0x90 })) return false;
    if (!memory.protectWrite(@ptrFromInt(address(rva_scheme)), "xttps\x00")) return false;
    logger.line("Carbon online overrides installed", .{});
    return true;
}
