const win = @import("shared").win;
const strings = @import("shared").strings;
const c = win.c;

pub var self_module: c.HMODULE = null;
pub var game_base: ?[*]u8 = null;

pub var online_enabled = true;
pub var plasma_host: [256]u8 = blk: {
    var x = [_]u8{0} ** 256;
    strings.copyZ(x[0..], "127.1.1.0");
    break :blk x;
};
pub var messenger_host: [256]u8 = blk: {
    var x = [_]u8{0} ** 256;
    strings.copyZ(x[0..], "127.1.1.0");
    break :blk x;
};
pub var http_base: [64]u8 = blk: {
    var x = [_]u8{0} ** 64;
    strings.copyZ(x[0..], "80");
    break :blk x;
};
pub var platform: [32]u8 = blk: {
    var x = [_]u8{0} ** 32;
    strings.copyZ(x[0..], "PC");
    break :blk x;
};
pub var messenger_port: u16 = 13505;
pub var disable_gm_demangler = true;

pub var mad_enabled = true;
pub var mad_host: [256]u8 = blk: {
    var x = [_]u8{0} ** 256;
    strings.copyZ(x[0..], "127.1.1.0");
    break :blk x;
};
pub var mad_port: u16 = 9000;
pub var force_all_mad_ports = true;

pub var virus_enabled = true;
pub var log_enabled = true;

pub fn applyConfig(_: void, section: []const u8, key: []const u8, value: []const u8) void {
    if (strings.eqlIgnoreCase(section, "network")) {
        if (strings.eqlIgnoreCase(key, "enabled")) {
            if (strings.parseBool(value)) |v| {
                online_enabled = v;
            }
        } else if (strings.eqlIgnoreCase(key, "host")) {
            strings.copyZ(plasma_host[0..], value);
            strings.copyZ(messenger_host[0..], value);
        } else if (strings.eqlIgnoreCase(key, "plasma_host")) strings.copyZ(plasma_host[0..], value) else if (strings.eqlIgnoreCase(key, "messenger_host")) strings.copyZ(messenger_host[0..], value) else if (strings.eqlIgnoreCase(key, "messenger_port")) {
            if (strings.parseU16(value)) |v| {
                messenger_port = v;
            }
        } else if (strings.eqlIgnoreCase(key, "http_base")) strings.copyZ(http_base[0..], value) else if (strings.eqlIgnoreCase(key, "platform")) strings.copyZ(platform[0..], value) else if (strings.eqlIgnoreCase(key, "disable_gm_demangler")) {
            if (strings.parseBool(value)) |v| {
                disable_gm_demangler = v;
            }
        }
    } else if (strings.eqlIgnoreCase(section, "mad")) {
        if (strings.eqlIgnoreCase(key, "enabled")) {
            if (strings.parseBool(value)) |v| {
                mad_enabled = v;
            }
        } else if (strings.eqlIgnoreCase(key, "host")) strings.copyZ(mad_host[0..], value) else if (strings.eqlIgnoreCase(key, "port")) {
            if (strings.parseU16(value)) |v| {
                mad_port = v;
            }
        } else if (strings.eqlIgnoreCase(key, "force_all_ports")) {
            if (strings.parseBool(value)) |v| {
                force_all_mad_ports = v;
            }
        }
    } else if (strings.eqlIgnoreCase(section, "content")) {
        if (strings.eqlIgnoreCase(key, "virus")) {
            if (strings.parseBool(value)) |v| {
                virus_enabled = v;
            }
        }
    } else if (strings.eqlIgnoreCase(section, "logging")) {
        if (strings.eqlIgnoreCase(key, "enabled") or strings.eqlIgnoreCase(key, "network")) {
            if (strings.parseBool(value)) |v| {
                log_enabled = v;
            }
        }
    }
}
