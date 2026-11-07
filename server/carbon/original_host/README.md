# Optional original Carbon rebroadcaster

The default Carbon server continues to use its Python rebroadcaster. The
`original_host` package provides an opt-in alternative for rooms managed by
an external `rebroadcaster.exe`. Python still provides authentication, Theater,
the room directory and the local stream bridge. The executable runs as a
separate process under a suitable Wine installation; it is not bundled here.

Use a separate staged configuration with `[carbon]` settings
`ORIGINAL_HOST_ACCOUNT`, `ORIGINAL_SEED_ACCOUNT`,
`ORIGINAL_HOST_UDP_PORT`, `ORIGINAL_STREAM_PORT`, and
`ORIGINAL_RESTART_MARKER`. The optional `ORIGINAL_PLAY_NOW_EARLY_START`
enables the existing Xbox timing workaround. `python -m
carbon.original_host.config_stage --help` describes creating a staged copy;
the initializer must be a distinct disabled service account. Password
authentication and a restart marker are required. With no host account set,
the feature is disabled.

`python -m carbon.original_host.stack --help` describes the separate stack
supervisor. It expects a prepared release containing the original executable
and required DLLs in a non-writable `reference` directory, a private host
config, Wine and its prefix, and the optional Starlette, Uvicorn and
websockets dependencies. The loopback ASGI app is in
`carbon.transport.web_app`. The stack's
public-route defaults use host UDP port 19140 and stream port 19141. The
ASGI transport uses the configured FESL, Theater and shared Messenger
listener ports and the original stream port. It is loopback-only and requires
a separately configured TLS reverse proxy for remote clients. Enabling the runtime in Carbon alone starts
the internal bridge; the stack starts the original executable and services.

The original-host mode retains the observed single-host and platform
limitations. Passing a local synthetic-client test does not establish real
PC/Xbox cross-play or production readiness.
