# NFS Online client plugins

One Zig source tree builds three Windows x86 ASI plugins:

- `net_u2.asi` for Underground 2
- `net_mw.asi` for Most Wanted (2005)
- `net_carbon.asi` for Carbon

## Requirements

- Linux x86-64 or Termux
- Zig 0.16.0
- Python 3
- `file`

## Build

```bash
./build_linux.sh
```

The script validates the source and configured ports, formats the Zig files, builds all clients, audits the PE outputs, and copies the matching INI files to:

```text
zig-out/bin/
```

## Install

Copy the matching `.asi` and `.ini` into the game's ASI/plugin directory.

All Underground 2 participants in a shared-port room must use the current client version. Do not combine these plugins with older hooks that patch the same networking or Carbon Online code.

Carbon DLC ownership is controlled by the server. The client only applies the configured Virus visibility patch.

## License

Original client code is `AGPL-3.0-or-later`. `src/carbon/virus.zig` is `LGPL-3.0-only`; preserve its source header and the license texts under `LICENSES/` in source and binary distributions.
