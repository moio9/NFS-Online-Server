// SPDX-License-Identifier: LGPL-3.0-only
// Virus vinyl visibility patch adapted from NFSCarbonDLCUnlocker by NicknineTheEagle.

const win = @import("shared").win;
const detour = @import("shared").detour;
const logger = @import("shared").logger;
const pe = @import("shared").pe;
const state = @import("state.zig");
const c = win.c;

var virus_detour = detour.Inline{};

const VirusFn = *const fn (?*anyopaque) callconv(.c) c_int;

fn original() VirusFn {
    return @ptrCast(virus_detour.trampoline.?);
}

fn hookVirus(part: ?*anyopaque) callconv(.c) c_int {
    if (state.virus_enabled and part != null) return 1;
    return original()(part);
}

pub fn install() bool {
    if (!state.virus_enabled) return true;
    if (virus_detour.trampoline != null) return true;

    const module = c.GetModuleHandleA(null) orelse return false;
    const text = pe.textSection(module) orelse return false;
    const target = pe.findSignature(
        text,
        "56 8B 74 24 08 85 F6 75 04 32 C0 5E C3 57 68 ? ? ? ? E8",
    ) orelse return false;

    if (!detour.install(&virus_detour, @ptrCast(target), @ptrCast(&hookVirus), 5)) {
        return false;
    }

    logger.line("Carbon Virus vinyl hook installed", .{});
    return true;
}
