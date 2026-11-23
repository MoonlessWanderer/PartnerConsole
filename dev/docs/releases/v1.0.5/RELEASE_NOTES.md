# Partner Console v1.0.5

Version 1.0.5 includes the application and Ubuntu 24.04 installer.

## Fixes

- Completed purchases record the local subscription ID on the operation. This applies to both the normal purchase path and a purchase completed during recovery after a lost panel response.
- A decrease compares the requested traffic cap with raw usage in bytes. Usage equal to the cap is allowed; a single byte above it is rejected, including when usage changes before confirmation.
- Traffic reset charges are calculated from raw bytes and rounded down once, to whole tomans. A changed charge requires a new quote and confirmation.
- When another tab signs in with a different partner account, the first tab follows the new session and discards its previous purchase draft.
- After a stale quote is rejected, the new quote also refreshes the product credit balance shown in the purchase form.

This release adds no database migration or backfill. Update keeps existing wallet, ledger and subscription records. The fixes apply when the request or recovery paths run.

## Upgrade and restore

Use the target installer and verify its SHA256 before running `sudo bash ./partnerconsole.sh update`. The [README](../../../../README.md#upgrade-an-existing-v104-installation) gives the commands for upgrading v1.0.4.

The Restore test imported a dump made from a custom-named source schema into a separate MySQL instance with the default `partnerconsole` schema and account. Direct Restore into a custom-named target remains unverified and the current Restore command requires the default schema and account. Keep protected copies of the application configuration and signing and panel-encryption keys separately from the SQL dump. Keep the reverse-proxy and TLS configuration separately as well.
