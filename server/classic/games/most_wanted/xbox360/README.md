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
