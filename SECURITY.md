# Security Policy

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, private
addresses, or logs. Use GitHub's private vulnerability-reporting feature for
this repository. If private reporting is unavailable, open a minimal issue
asking the maintainer to establish a private channel; omit technical details.

Include the affected version/commit, prerequisites, impact, a minimal
reproduction, and suggested remediation. Remove tokens, API keys, cookies,
usernames, and private network details.

## Response targets

These are project targets, not an SLA: acknowledge critical/high reports in
three business days, establish severity and containment in seven, and publish
a coordinated fix/advisory as soon as safely validated. Lower-severity issues
are prioritized by exploitability and impact.

## Supported version

Only the latest published release and the default branch receive security
fixes. Operators should update Home Assistant, the app itself, and retain a
tested rollback/backup.

## Security boundaries

This app is the upstream Music Assistant server image plus one Python wheel.
It inherits the upstream app's privileges (`host_network`, `SYS_ADMIN`,
`DAC_READ_SEARCH`, audio, `media:rw`) and the upstream AppArmor profile; see
[docs/security.md](docs/security.md). Vulnerabilities in the server, its
Debian base image or its Python dependencies are upstream's to fix; this
repository follows the next stable release automatically. The fork frontend
wheel is verified against a pinned SHA-256 before installation.
