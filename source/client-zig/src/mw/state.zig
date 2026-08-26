const std = @import("std");
const win = @import("shared").win;
const net = @import("shared").net_util;
const strings = @import("shared").strings;
const c = win.c;

pub var self_module: c.HMODULE = null;
pub var bootstrap = net.Endpoint.init("127.0.0.1", 30921);
pub var lobby = net.Endpoint.init("127.0.0.1", 30920);
pub var control = net.Endpoint.init("127.0.0.1", 20923);
pub var control_alias = net.Endpoint.init("127.0.0.1", 13505);
pub var race = net.Endpoint.init("127.0.0.1", 20000);
pub var lan = net.Endpoint.init("127.0.0.1", 9900);
pub var lan_control = net.Endpoint.init("127.0.0.1", 20923);
pub var lan_control_alias = net.Endpoint.init("127.0.0.1", 13505);
pub var discovery = net.Endpoint.init("127.0.0.1", 3658);

pub var bootstrap_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var lobby_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var control_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var control_alias_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var race_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var lan_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var lan_control_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var lan_control_alias_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);
pub var discovery_addr: c.sockaddr_in = std.mem.zeroes(c.sockaddr_in);

pub var network_enabled = true;
pub var lan_enabled = true;
pub var patches_enabled = true;
pub var discovery_mirror = true;
pub var inject_server = true;
pub var refresh_state = false;
pub var selected_host_hook = true;
pub var log_enabled = true;
pub var udp_trace = true;
pub var server_name: [32]u8 = blk: {
    var x = [_]u8{0} ** 32;
    strings.copyZ(x[0..], "MWONLINE");
    break :blk x;
};
pub var server_racers: u32 = 1;

pub fn refreshAddresses() void {
    _ = net.resolve(&bootstrap, &bootstrap_addr);
    _ = net.resolve(&lobby, &lobby_addr);
    _ = net.resolve(&control, &control_addr);
    _ = net.resolve(&control_alias, &control_alias_addr);
    _ = net.resolve(&race, &race_addr);
    _ = net.resolve(&lan, &lan_addr);
    _ = net.resolve(&lan_control, &lan_control_addr);
    _ = net.resolve(&lan_control_alias, &lan_control_alias_addr);
    _ = net.resolve(&discovery, &discovery_addr);
}

fn setNetworkHosts(value: []const u8) void {
    bootstrap.setHost(value);
    lobby.setHost(value);
    control.setHost(value);
    control_alias.setHost(value);
    race.setHost(value);
}

fn setLanHosts(value: []const u8) void {
    lan.setHost(value);
    lan_control.setHost(value);
    lan_control_alias.setHost(value);
    discovery.setHost(value);
}

pub fn applyConfig(_: void, section: []const u8, key: []const u8, value: []const u8) void {
    if (strings.eqlIgnoreCase(section, "network")) {
        if (strings.eqlIgnoreCase(key, "enabled")) {
            if (strings.parseBool(value)) |v| network_enabled = v;
        } else if (strings.eqlIgnoreCase(key, "host")) {
            setNetworkHosts(value);
        } else if (strings.eqlIgnoreCase(key, "bootstrap_host")) {
            bootstrap.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "lobby_host")) {
            lobby.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "control_host")) {
            control.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "control_alias_host")) {
            control_alias.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "race_host")) {
            race.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "bootstrap_port")) {
            if (strings.parseU16(value)) |v| bootstrap.port = v;
        } else if (strings.eqlIgnoreCase(key, "lobby_port")) {
            if (strings.parseU16(value)) |v| lobby.port = v;
        } else if (strings.eqlIgnoreCase(key, "control_port")) {
            if (strings.parseU16(value)) |v| control.port = v;
        } else if (strings.eqlIgnoreCase(key, "control_alias_port")) {
            if (strings.parseU16(value)) |v| control_alias.port = v;
        } else if (strings.eqlIgnoreCase(key, "race_port")) {
            if (strings.parseU16(value)) |v| race.port = v;
        }
    } else if (strings.eqlIgnoreCase(section, "lan")) {
        if (strings.eqlIgnoreCase(key, "enabled")) {
            if (strings.parseBool(value)) |v| lan_enabled = v;
        } else if (strings.eqlIgnoreCase(key, "host")) {
            setLanHosts(value);
        } else if (strings.eqlIgnoreCase(key, "port")) {
            if (strings.parseU16(value)) |v| lan.port = v;
        } else if (strings.eqlIgnoreCase(key, "control_host")) {
            lan_control.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "control_port")) {
            if (strings.parseU16(value)) |v| lan_control.port = v;
        } else if (strings.eqlIgnoreCase(key, "control_alias_host")) {
            lan_control_alias.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "control_alias_port")) {
            if (strings.parseU16(value)) |v| lan_control_alias.port = v;
        } else if (strings.eqlIgnoreCase(key, "discovery_host")) {
            discovery.setHost(value);
        } else if (strings.eqlIgnoreCase(key, "discovery_port")) {
            if (strings.parseU16(value)) |v| discovery.port = v;
        } else if (strings.eqlIgnoreCase(key, "discovery_mirror")) {
            if (strings.parseBool(value)) |v| discovery_mirror = v;
        } else if (strings.eqlIgnoreCase(key, "inject_server")) {
            if (strings.parseBool(value)) |v| inject_server = v;
        } else if (strings.eqlIgnoreCase(key, "server_name")) {
            strings.copyZ(server_name[0..], value);
        } else if (strings.eqlIgnoreCase(key, "server_racers")) {
            if (strings.parseU32(value)) |v| server_racers = @min(v, 4);
        } else if (strings.eqlIgnoreCase(key, "selected_host_hook")) {
            if (strings.parseBool(value)) |v| selected_host_hook = v;
        } else if (strings.eqlIgnoreCase(key, "refresh_state")) {
            if (strings.parseBool(value)) |v| refresh_state = v;
        }
    } else if (strings.eqlIgnoreCase(section, "patches")) {
        if (strings.eqlIgnoreCase(key, "enabled")) {
            if (strings.parseBool(value)) |v| patches_enabled = v;
        }
    } else if (strings.eqlIgnoreCase(section, "logging")) {
        if (strings.eqlIgnoreCase(key, "enabled")) {
            if (strings.parseBool(value)) |v| log_enabled = v;
        } else if (strings.eqlIgnoreCase(key, "udp_trace")) {
            if (strings.parseBool(value)) |v| udp_trace = v;
        }
    }
}
