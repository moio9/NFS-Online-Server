const win = @import("shared").win;
const pe = @import("shared").pe;
const memory = @import("shared").memory;
const logger = @import("shared").logger;
const state = @import("state.zig");
const c = win.c;

pub fn install() bool {
    if (!state.patches_enabled) return true;
    const module = c.GetModuleHandleA(null);
    const image = pe.image(module) orelse return false;
    const match = pe.findSignature(image, "7D ? C7 86 ? ? ? ? ? ? ? ? EB ? 03 7C 24") orelse
        pe.findSignature(image, "7E ? C7 86 ? ? ? ? ? ? ? ? EB ? 03 7C 24") orelse return false;
    if (match[0] == 0x7e) return true;
    const ok = memory.protectWrite(@ptrCast(match), &[_]u8{0x7e});
    if (ok) logger.line("MW certificate branch patched", .{});
    return ok;
}
