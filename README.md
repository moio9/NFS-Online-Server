# NFS Online Server

Unofficial community server and Windows x86 client plugins for:

- Need for Speed: Underground 2
- Need for Speed: Most Wanted (2005)
- Need for Speed: Carbon

The server is written in Python and uses one TOML configuration. The three ASI clients are built from Zig source.

## Requirements

Server:

- Linux
- Python 3.10 or newer with `sqlite3`

Client build:

- Linux x86-64 or Termux
- Zig 0.16.0
- Python 3 and `file`

No original game files are included.

## Files kept local

The repository deliberately ignores generated, private, and machine-specific
files. This includes:

- `runtime/`, `logs/`, PID files, caches, and virtual environments;
- account databases, password/user data, backups, server state, and Carbon
  progression/social/blob files under `data/`;
- built client plugins (`*.asi`) and other Zig/client build output;
- credentials, private keys, and locally generated release archives.

Do not force-add these files to a commit. Keep a separate backup of production
data when upgrading the server.

The original Need for Speed executables, game assets, and other proprietary
files must also remain on the operator's machine. They are required for
compatibility testing but are never distributed by this repository.

## Quick start

```bash
git clone <repository-url>
cd NFS-Online-Server
python nfs_online.py check
python nfs_online.py configure --public-host server.example.com
python nfs_online.py create-account
./start.sh
```

Useful commands:

```bash
python nfs_online.py status
python nfs_online.py stop
python nfs_online.py start --games u2,mw --daemon
python nfs_online.py start --games carbon
python nfs_online.py kick ACCOUNT_NAME
python nfs_online.py dlc show ACCOUNT_NAME
python nfs_online.py stats --help
```

Run `python nfs_online.py --help` for the complete command list.

## Connect to a public server

Players who want to join the my server can use this address:

```text
brake.go.io
```

## Configuration

Server settings are stored in:

```text
config/server.toml
```

Client settings remain game-specific:

```text
clients/underground2/net_u2.ini
clients/most-wanted/net_mw.ini
clients/carbon/net_carbon.ini
```

Change `PUBLIC_HOST` before an Internet deployment. Keep the Messenger IPC and standalone DLC listener bound to loopback unless they are placed behind a secured reverse proxy.

## Default ports

| Service | Protocol | Port |
|---|---|---:|
| Shared Messenger | TCP | 13505 |
| Messenger IPC | TCP, loopback | 13506 |
| U2 bootstrap | TCP | 20921 |
| U2 lobby | TCP | 20922 |
| U2/MW web and PREL | TCP | 20923 |
| Most Wanted lobby | TCP | 30920 |
| Most Wanted bootstrap | TCP | 30921 |
| U2/MW race relay | UDP | 20000 |
| Carbon FESL | TCP | 18210 |
| Carbon Theater | TCP | 18215 |
| Carbon race transport | UDP | 19119 |
| Carbon Massive Ads | TCP | 9000 |
| Standalone DLC page | TCP, loopback | 8081 |

Open only the ports required by the games you run.

## Client plugins

Build all three clients:

```bash
cd source/client-zig
./build_linux.sh
```

Outputs are written to `source/client-zig/zig-out/bin/`:

```text
net_u2.asi
net_mw.asi
net_carbon.asi
```

Copy each ASI beside its matching INI in the game's ASI/plugin directory. Do not load it together with an older plugin that patches the same game code.

## Roadmap

Planned bug fixes, protocol work, ranking support, configuration options,
documentation, and general polish are tracked in [TODO.md](TODO.md).

## Tests

```bash
python -m unittest discover -s tests -q

cd server/common
PYTHONPATH=.. python -m unittest discover -s tests -q

cd ../classic
PYTHONPATH=.. python -m unittest discover -s tests -q

cd ../carbon
PYTHONPATH=.. python -m unittest discover -s tests -q
```

Before publishing, also run:

```bash
python tools/check_public_tree.py
python nfs_online.py check
```

## Data safety

The first run creates `runtime/`, `logs/`, the account database, and other persistent state. These paths are ignored by Git. Never publish a production server directory or force-add ignored files.

## License

Original project code is licensed under `AGPL-3.0-or-later`. See [LICENSE](LICENSE) and [LICENSE-NOTICE.md](LICENSE-NOTICE.md).

Need for Speed and related names are trademarks of Electronic Arts. This project is unofficial and is not affiliated with or endorsed by Electronic Arts.
