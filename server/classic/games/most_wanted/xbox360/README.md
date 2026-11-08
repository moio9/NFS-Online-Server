# Experimental Most Wanted Xbox 360 adapter

This is a separate Xbox lobby using the Classic server's existing room engine
and password-backed shared account database. The client bridge authenticates
an account on behalf of one local game process; the gamertag and XUID are not
credentials. It does not implement Xbox Live authentication.

Both the service and the bridge accept loopback bindings only. The Classic
password encoding is reversible: use an authenticated encrypted tunnel for
every remote hop, and only use the bridge in a trusted local environment. No
public listener or tunnel is automatically installed by this experiment.

The Xbox lobby is independent from PC rooms. Xbox stats are kept in memory,
not merged with the PC leaderboard, until the stat and result formats have
been verified. Two native instances on the same phone joined a local instance
using synthetic accounts and entered the same race. The host saw the guest's
car accelerate and move away. This test used a laboratory-only loopback bind
and destination rewrite, so each game could use UDP port 3658 on one device;
game traffic went directly between the instances, not through a race tunnel.
The host also displayed the guest in the game room after the Xbox adapter was
changed to send an updated userset and member record after game join, following
the notification pattern observed with the original LAN DLL.

The Xbox lobby heartbeat uses UTC, matching the original LAN DLL. Sending the
server's local time caused a two-hour countdown error in a UTC+2 test. The
change is confined to the Xbox lobby profile; other game profiles are unchanged.
Race completion and results, Internet NAT traversal between separate devices,
shared-network XUID collisions, and PC/Xbox interoperability remain unverified.
Do not advertise this as production-ready.

The isolated service can be started with the normal server configuration:

```sh
PYTHONPATH=server python -m classic.games.most_wanted.xbox360 --config config/server.toml
```

The client-side bridge is started separately in a terminal, using an existing
account; it prompts for the password without putting it in command arguments:

```sh
PYTHONPATH=server python -m classic.games.most_wanted.xbox360.client_bridge --account ACCOUNT
```

For Terms of Service and News, start the Classic web service and pass the port
at which it is reachable on loopback. For example, with a web service (or the
local end of its encrypted tunnel) on `127.0.0.1:20923`:

```sh
PYTHONPATH=server python -m classic.games.most_wanted.xbox360.client_bridge --account ACCOUNT --web-upstream-port 20923
```

The bridge rewrites the three web URLs in the signed MW news response to its
own loopback address and port 18080, then forwards those requests to the
configured local web service. `--web-upstream-ip` selects another loopback
address, and `--web-listen-port` changes the local listener. This option does
not start a web service or create an encrypted tunnel; set both up separately.
The local lab verified that the Xbox game fetched and displayed both the
server's terms and its News through the bridge. Whether acceptance persists
across later logins remains unverified.

By default it listens at `127.0.0.4` and connects to a service (or the local end
of an encrypted tunnel) at `127.0.0.1`, on TCP 30960 and 10901. Configure the
game's `red_dns` to point to the bridge. Each simultaneous game needs its own
bridge and secured connection. The launcher and the public server do not start
either component automatically.

## Optional original LAN server backend

The default remains `--backend python`. The opt-in `--backend original`
uses a separately supplied original PC `server.dll` for lobby and room
commands, while the Python Xbox runtime handles directory, password-backed
authentication, News and the existing Xbox `user` statistics response. Login
credentials are removed from the message sent to the original DLL, and the
DLL receives the persona verified by Python. This is not Xbox Live
authentication. The DLL's room state is separate from Python's room state.

Build `original_host_loader.c` as a 32-bit Windows executable and put that
executable in a private working directory alongside your own `server.dll` and
a LAN `server.cfg`. The DLL and generated executable are not included. The
DLL must be isolated from external network access with an appropriate
OS-level sandbox; configuring a loopback address alone does not restrict all
outbound connections. Start the loader in that directory under Wine; the DLL
is expected on `127.0.0.1:9900` by default. For example, on a machine that
already has a suitable loopback-only process guard and Wine configured:

```sh
i686-w64-mingw32-gcc -O2 server/classic/games/most_wanted/xbox360/original_host_loader.c -o /path/to/private/host/loader.exe
cd /path/to/private/host
LOOPBACK_GUARD /path/to/wine loader.exe
```

From the server checkout, start the optional backend on separate loopback
addresses. Use the same configured shared account database as usual:

```sh
PYTHONPATH=server python -m classic.games.most_wanted.xbox360 \
  --config config/server.toml --bind-ip 127.0.0.8 \
  --backend original --original-listen-ip 127.0.0.9
```

Start the existing client bridge with `--upstream-ip 127.0.0.9` and, when
News/ToS are available, the web upstream options described above. Remote
connections still need their own authenticated encrypted tunnel. The loader,
Python runtime and client bridge are separate processes; switching modes
requires restarting the runtime and connected clients.

The integrated backend was tested locally with two synthetic authenticated
accounts and two Xbox instances using the same-phone-only UDP bind/routing
workaround: both entered the same room and started a race. It has not been
validated for separate devices, Internet/NAT traversal, full race results,
leaderboard persistence or PC/Xbox rooms. Python does not yet synchronize
race results and room lifecycle from the DLL; do not treat this mode as a
production replacement for the Python backend.
