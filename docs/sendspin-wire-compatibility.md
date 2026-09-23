# Sendspin output delay wire compatibility

The image pins Music Assistant server 2.10.4 and aiosendspin 9.1.1. That
aiosendspin release calls the player setting `static_delay_ms`, advertises
`set_static_delay`, and sends `static_delay_ms` with its server command. The
current Sendspin player v1 specification calls the equivalent field
`output_delay_ms` and command `set_output_delay`.

Before the compatibility patch, parsing a current-spec player state with
`supported_commands: ["set_output_delay"]` raises `InvalidFieldValue` because
the pinned `PlayerCommand` enum has no such value. The same spec-compliant
client also omits the old `client/hello` supported-commands field and may
advertise `volume` and `mute` in `client/state`; the pinned model previously
required the former and rejected the latter. The build-time patch accepts
both handshake layouts and either reported delay field. It reads reported
volume and mute even when those values are read-only, while sending control
commands only when the client advertises them. Internally the pinned
server continues to use its existing delay, timing and persistence path. When
the server sends a delay update, it chooses `set_output_delay` with
`output_delay_ms` for a current-spec client, or the legacy command and field
for a legacy client. It never sends a delay command to a client that advertises
neither. The existing `sendspin_static_delay` Music Assistant configuration key
is retained so saved player settings do not need migration.

The current spec also lets a client report a preferred audio `format` in
`client/state`. The pinned model ignored that field. The patch parses it and
routes changes through the pinned role's existing format-request validation and
transition path, which checks the client's supported formats and the server's
encoder support before changing streams. Legacy `stream/request-format` remains
available.

This patch does not make an unsupported client adjustable. It also does not
change AirPlay or Squeezelite's signed `sync_adjust`, browser-local timing, or
the Cast receiver's separate saved delay. A real client must acknowledge its
new value through a later state report; acoustic alignment remains a live
device test rather than a conclusion from wire compatibility alone.
