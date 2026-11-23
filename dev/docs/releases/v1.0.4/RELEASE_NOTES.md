# Partner Console v1.0.4

The application and the Ubuntu 24.04 installer are both version 1.0.4. The release contains the current Windows development setup, frontend and API, and the installer for Ubuntu.

## Payments and subscriptions

- Paid actions show a short-lived signed quote. The backend verifies the reviewed amount and payment mix before accepting the operation. A material change requires a fresh quote and confirmation.
- Quotes are bound to the partner account and sign-in session. Retrying an accepted operation uses durable recovery and duplicate-request protection.

## Install or update

Download `partnerconsole.sh` and `SHA256SUMS` from this Release and verify the installer with `sha256sum --check --strict SHA256SUMS`. Run the verified installer on Ubuntu 24.04. For an existing installation, use its `update` command after recording a database backup and preserving the local configuration. Refresh open browser tabs after the update.

## Known limitations

- V2Board usage can change between the last check and a traffic reset. The charge remains tied to the amount the partner approved.
- Use separate browser profiles for simultaneous administrator and partner sessions on the same origin.
