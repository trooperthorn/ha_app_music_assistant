# Music Assistant (Library Manager)

The upstream [Music Assistant server](https://github.com/music-assistant/server)
with the [trooperthorn library-manager frontend](https://github.com/trooperthorn/HA_int_MA-UI)
installed over the stock one. Everything else (options, ports, ingress, the
AppArmor profile, backups) is the upstream app definition, refreshed by this
repository's sync workflow.

Run this instead of the official Music Assistant app, not beside it: both use
the host network and port 8095, and both announce the same discovery service
to the Home Assistant integration.

The image is built on your Home Assistant host from the upstream server image
plus one wheel, so the first install and each update take a few minutes.
