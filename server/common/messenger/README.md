# Shared EA Messenger transport

`wire.py` implements the shared frame format and stream decoder. `hub.py` owns
socket handling, adapter selection and connection policy. The listener uses
adapters for U2, Most Wanted and Carbon; its host depends on the selected games.

Game-specific authentication, commands, profiles and responses remain in their
respective adapters and services. The social registry lives in `common.social`.
`classic.ea.messenger` preserves older import paths; it does not run another
listener. The pre-session DISC response remains part of the established shared
listener behavior.

For a Carbon-only selection, the launcher starts
`python -m common.messenger.standalone --config config/server.toml` and the
Carbon process. The standalone host receives signed session snapshots through
loopback IPC, shares the configured account database, handles website social
events and account-policy notifications, and provides the existing web/PREL
gateway. When U2 or MW is enabled, Classic hosts the shared listener instead;
switching between these modes restarts Carbon and interrupts active sessions.
