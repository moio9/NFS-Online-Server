# Carbon: common services and platform adaptations

The existing FESL, Theater, GameManager and rebroadcaster modules remain
shared. Their existing game and room rules are not redefined here. This
package groups the conventions and adapters that currently differ by client.
The Python candidate remains a single application; this is a code boundary,
not a new network service or deployment.

The optional, separate original-executable implementation is described in
`../original_host/README.md`. It is disabled unless explicitly configured;
the Python rebroadcaster remains available.

* `contracts.py` defines the session-policy interface. `pc.py` supplies the
  existing PC-compatible defaults, also used for an unknown platform.
* `xbox360/session.py` supplies the existing Xbox session-ID, self-join and
  peer-publication conventions. Some are compatibility experiments and are
  not proven to reproduce an original Xbox service.
* `xbox360/live.py` holds the authenticated account's optional Xbox wire-format
  preference. `xbox360/live_gateway.py` is the existing experimental
  loopback password bridge for observed Xbox Live-related messages. Neither
  one validates Xbox Live identity or proves XUID ownership. The bridge can
  advertise local Theater/Messenger tunnels and attach an authenticated race
  adapter for the lifetime of its session.
* `xbox360/prototunnel.py` and `xbox360/clear_ingress.py` implement the clear
  framing and local ingress selection observed in testing.
  This mode provides no authentication or confidentiality; the existing loopback
  and authenticated-ticket guards must remain in force. This is not a claim
  about all security layers on a retail Xbox.

The old clear-codec functions in `transport/prototunnel.py`
delegate to the Xbox clear codec, preserving their previous API. Shared packet
types and the existing encrypted ProtoTunnel implementation remain in the
transport package; an encrypted codec should not be declared PC-exclusive
without evidence about the original Xbox networking layers.

`carbon/race_client.py` is the shared local UDP-to-WSS race adapter used by
the bridge. It requires the optional `websockets` package (tested with 16.1.1)
when this transport is used; the normal server and basic bridge do not import
that package. The WSS endpoint and other required tunnel services must also be
set up separately. Adding the adapter does not enable them in the server.

Client adaptations should preserve original game behavior where possible;
compatibility behavior should be documented as such.
