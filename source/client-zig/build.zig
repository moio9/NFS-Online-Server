const std = @import("std");

fn addAsi(
    b: *std.Build,
    name: []const u8,
    source: []const u8,
    shared_module: *std.Build.Module,
    target: std.Build.ResolvedTarget,
    optimize: std.builtin.OptimizeMode,
) void {
    const root_module = b.createModule(.{
        .root_source_file = b.path(source),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });
    root_module.addImport("shared", shared_module);

    const lib = b.addLibrary(.{
        .name = name,
        .linkage = .dynamic,
        .root_module = root_module,
    });

    lib.root_module.linkSystemLibrary("kernel32", .{});
    lib.root_module.linkSystemLibrary("ws2_32", .{});
    lib.root_module.strip = optimize != .Debug;

    const output_name = b.fmt("{s}.asi", .{name});
    const install = b.addInstallFileWithDir(lib.getEmittedBin(), .bin, output_name);
    b.getInstallStep().dependOn(&install.step);
}

pub fn build(b: *std.Build) void {
    const optimize = b.standardOptimizeOption(.{});
    const target = b.resolveTargetQuery(.{
        .cpu_arch = .x86,
        .os_tag = .windows,
        .abi = .gnu,
    });

    const shared_module = b.createModule(.{
        .root_source_file = b.path("src/shared/root.zig"),
        .target = target,
        .optimize = optimize,
        .link_libc = true,
    });

    addAsi(b, "net_u2", "src/u2/main.zig", shared_module, target, optimize);
    addAsi(b, "net_mw", "src/mw/main.zig", shared_module, target, optimize);
    addAsi(b, "net_carbon", "src/carbon/main.zig", shared_module, target, optimize);
}
