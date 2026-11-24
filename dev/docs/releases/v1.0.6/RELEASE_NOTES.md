# PartnerConsole v1.0.6

The Ubuntu installer supports SSH listening on a custom TCP port. It checks
the live SSH listeners before and after applying the MySQL firewall rules,
including Ubuntu 24.04 socket activation. MySQL stays restricted to loopback.
Update reports SSH detection and MySQL firewall failures separately.

Use the target installer and verify its SHA256 before running
`sudo bash ./partnerconsole.sh update`. See the
[upgrade instructions](../../../../README.md#upgrade-an-existing-v104-installation).

No database migration or financial backfill is included. CI now seals private
upgrade fixtures with their own tree hashes, and the browser check allows
additional time for Chrome startup.
