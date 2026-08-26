# NFS Online Server

Unofficial community server and Windows x86 client plugins for:

- Need for Speed: Underground 2
- Need for Speed: Most Wanted (2005)
- Need for Speed: Carbon

The server is written in Python and uses one TOML configuration. The three ASI clients are built from Zig source.

This public tree contains no production accounts, password hashes, player data, logs, secrets, backups, or compiled ASI files.

## Requirements

Server:

- Linux
- Python 3.10 or newer with `sqlite3`

Client build:

- Linux x86-64 or Termux
- Zig 0.16.0
- Python 3 and `file`

No original game files are included.

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

The Carbon Virus visibility patch is separately licensed under `LGPL-3.0-only`. Its attribution is preserved in the source header and [LICENSE-NOTICE.md](LICENSE-NOTICE.md).

Need for Speed and related names are trademarks of Electronic Arts. This project is unofficial and is not affiliated with or endorsed by Electronic Arts.
