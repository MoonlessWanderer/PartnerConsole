# Partner Console

Partner Console is a self-hosted B2B application for administrators and partners. Partners can provision subscriptions on a connected V2Board panel using wallet funds or product-specific credit. Administrators manage partners, products, panels, and transaction records. The application interface is in Persian.

## Features

- Partner and administrator dashboards with separate sign-in pages
- V2Board subscription provisioning, link and traffic resets, and subscription management
- Wallet balances in toman, product-specific data and day credits, and an auditable transaction ledger
- Ubuntu installer with service control, updates, database backup and restore, Domain & SSL, and administrator credential maintenance

## Technology stack and requirements

The application uses Next.js 15, React 19, TypeScript, FastAPI, Python, SQLAlchemy, Alembic, and V2Board. The Ubuntu installer targets Ubuntu 24.04 and provisions Node.js 22, Python 3.12 or newer, and MySQL 8.4 LTS. Domain setup uses Nginx and Certbot and requires a DNS name that resolves to the server and inbound HTTP and HTTPS access.

## Installation

The application and installer are both **v1.0.5**. Download the installer and checksum file from the [v1.0.5 Release](https://github.com/MoonlessWanderer/PartnerConsole/releases/tag/v1.0.5).

```bash
work_dir="$(mktemp -d)"
cd "$work_dir"
release_url=https://github.com/MoonlessWanderer/PartnerConsole/releases/download/v1.0.5
curl -fsSLO "$release_url/partnerconsole.sh"
curl -fsSLO "$release_url/SHA256SUMS"
sha256sum --check --strict SHA256SUMS
```

After the checksum check succeeds, open the installer menu.

```bash
sudo bash ./partnerconsole.sh
```

The default installation directory is `/srv/partnerconsole`. To choose a different directory, pass `PARTNERCONSOLE_ROOT` to the initial installer invocation. Once installed, open the menu with `sudo partnerconsole`.

| Service | Local address |
| --- | --- |
| API | `http://127.0.0.1:8741` |
| Partner sign-in | `http://127.0.0.1:3741/` |
| Administrator sign-in | `http://127.0.0.1:3741/gate/k3p8n2` |
| MySQL | `127.0.0.1:3306` |

Use an SSH tunnel to reach the local web and API ports before configuring a public HTTPS origin. The partner page omits the administrator sign-in link. Both roles still require authentication.

## Installer commands

These commands describe Installer `v1.0.5`.

| Command | Action |
| --- | --- |
| `sudo partnerconsole` | Open the interactive menu |
| `sudo partnerconsole start` | Start the application |
| `sudo partnerconsole stop` | Stop the application |
| `sudo partnerconsole restart` | Restart the application |
| `sudo partnerconsole status` | Check processes and API health |
| `sudo partnerconsole logs` | Show recent application logs |
| `sudo partnerconsole update` | Apply the release pinned by the installer being run |
| `sudo partnerconsole domain` | Open Domain & SSL in an interactive terminal |
| `sudo partnerconsole credentials` | Maintain the administrator credentials through an interactive prompt |

### Upgrade an existing v1.0.4 installation

On the Ubuntu host, stop Partner Console, take a database backup, save protected copies of the application environment files, and start the current installation again.

```bash
sudo partnerconsole stop
sudo partnerconsole backup
sudo partnerconsole start
```

Download the installer and checksum for the **target** release into a new directory. Verify the checksum before running Update.

```bash
work_dir="$(mktemp -d)"
cd "$work_dir"
release_url=https://github.com/MoonlessWanderer/PartnerConsole/releases/download/v1.0.5
curl -fsSLO "$release_url/partnerconsole.sh"
curl -fsSLO "$release_url/SHA256SUMS"
sha256sum --check --strict SHA256SUMS
sudo bash ./partnerconsole.sh update
sudo partnerconsole status
```

Update checks the target tag and release tree, detects local changes to managed files, and rebuilds the web client with the existing `config/web.env`. It keeps the configured database and application settings. After Update, sign in using the existing account, refresh any open tabs, and review a fresh quote before confirming a paid action.

For manual installation and recovery, see the [manual installation guide](dev/docs/guides/INSTALL.md) and [production maintenance procedure](dev/docs/guides/production-bootstrap.md).

## Domain & SSL

Run `sudo partnerconsole domain` in an interactive terminal. Check that the domain's A record matches the server. Any AAAA records must match the server's usable public IPv6 address. The installer can request permission to install the required Ubuntu packages and uses Nginx to serve a local HTTP-01 challenge check before invoking Certbot's Webroot authenticator.

Once the certificate exists and the HTTPS checks pass, Nginx redirects ordinary HTTP requests to HTTPS and proxies the application over loopback. The challenge route stays available on HTTP for renewals. The installer records the HTTPS origin in the API and web configuration and offers an optional systemd boot-start unit. The app itself runs as `ppv1`.

Next.js embeds `NEXT_PUBLIC_API_BASE` in the browser JavaScript at build time. When Domain & SSL configures a new domain, it loads `config/web.env` before it rebuilds the web app and stops if the value is not the new HTTPS origin. After the build and again over HTTPS, it checks that the sign-in JavaScript calls the API at that origin. Any failure restores the previous configuration, build, and Nginx site. For a domain that is already active, Domain & SSL does not rebuild the web app. It checks the served sign-in JavaScript and reports a mismatch.

## Backup & Restore

Stop the application before creating a database backup with `sudo partnerconsole backup`. The Backup & Restore menu also lists backups and their checksum status. Store the database dump and protected copies of the configuration and encryption keys. Restore requires an intact backup and a planned outage. A MySQL import can leave the target partially imported if it fails.

See the [production recovery procedure](dev/docs/guides/production-bootstrap.md) for the database and recovery checks.

## Admin credentials

Run `sudo partnerconsole credentials` in a terminal or select **Admin Credentials** in the installer menu. The command uses the installer-managed local MySQL database from the root-owned `config/api.env`. Select an existing administrator by ID. If you rename the administrator named by `ADMIN_USERNAME`, the command also updates that setting. Password prompts hide the input. Already issued tokens remain valid until expiry or revocation.

For manual installations, use `bash scripts/reset-admin-password.sh --confirm-database USERNAME` after reviewing the database displayed by the same script without arguments.

## Browser sessions

Administrator and partner sessions on the same web origin share browser storage. The most recent sign-in replaces the saved session across tabs in the same profile. Use two separate browser profiles to stay signed in to both roles.

## Development

Run the backend in one terminal. This example uses PowerShell:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip --only-binary=:all:
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set an independent random `SECRET_KEY` and a non-default initial administrator password in `.env`, then run `uvicorn app.main:app --port 8741 --reload`. On development startup, the API applies migrations and creates the configured administrator if the administrator table is empty.

From the repository root, install the frontend dependencies with `npm ci` and run `npm run dev`. The development API base defaults to `http://127.0.0.1:8741`. To override it, set `NEXT_PUBLIC_API_BASE` in `.env.local`.

Development defaults to SQLite at `backend/data/partner.db`. MySQL tests require an isolated schema configured as described in the [MySQL validation guide](dev/docs/guides/production-mysql-validation.md). An example isolated connection string is `mysql+pymysql://TEST_USER:ENCODED_PASSWORD@127.0.0.1:3307/partnerpanel_test_wp1?charset=utf8mb4`. Production uses a dedicated `python -m app.bootstrap_database` maintenance command to prepare the schema. Application and Alembic MySQL connections use UTC, READ COMMITTED, and `utf8mb4_0900_bin`.

### Billing rules

- The toman wallet is shared across products. Data and day credits belong to individual products.
- Provisioning and upgrades consume matching product credit first, then charge any shortfall at that product's rate. Reducing or deleting a subscription returns product credit.
- Link resets are free. Resetting traffic costs `floor(usage × price_per_gb)`.
- V2Board remains the source for subscription URLs, usage, and subscriber email. The application tracks the stable `v2_user_id`.
- Dates use the Persian calendar and the `Asia/Tehran` time zone.

See the [application contract](dev/docs/guides/START.md) for the complete rules.

## Testing

Run the backend suite from `backend` with the development environment active. Use an in-memory SQLite database for the default run.

```powershell
$env:DATABASE_URL = 'sqlite://'
python -m pytest
```

From the repository root:

```bash
npm run test:frontend
npm run typecheck
bash dev/tests/installer/release-check.sh
bash dev/tests/installer/features-check.sh
bash dev/tests/installer/domain-launcher-check.sh
bash dev/tests/installer/domain-rollback-check.sh
bash dev/tests/installer/domain-acme-check.sh
bash dev/tests/installer/domain-build-env-check.sh
bash dev/tests/installer/domain-build-next-check.sh
```

Backend and frontend tests live under `dev/tests/`. The simulated V2Board panel lives in `dev/v2lab/`.

`domain-build-next-check.sh` copies the repository to a temporary custom root, runs `npm ci` and real Next.js builds, and uses headless Chrome or Edge to record the sign-in request URLs. Set `CHROME` to the browser path if it is not detected. The `Installer Linux validation` workflow runs additional suites on GitHub-hosted Ubuntu 24.04. Its Credentials job uses a controlling terminal, the installed launcher, and disposable MySQL 8.4. The install, update, and restore job tests official release baselines against the candidate in a disposable runner-local repository, then runs Backup and Full Restore on fixture data. Backend tests use temporary SQLite databases and simulated V2Board responses for billing and rollback. The security suite and guarded MySQL tests document their own prerequisites.

## Security considerations

- Use a random `SECRET_KEY` of at least 32 characters and an initial administrator password between 12 and 200 characters. Use independent, random values for optional JWT and panel encryption keys. Preserve the key that protects existing panel credentials when planning a rotation.
- Use `ENVIRONMENT=production` and exact HTTPS origins in `CORS_ORIGINS`. The application uses Bearer tokens and currently stores them in browser local storage. Trust forwarded client IPs only from the designated reverse proxy.
- Login throttling and session revocation persist in the database. Login allows up to eight attempts per account and role and 40 per IP and role within ten minutes. The default API limit is 120 requests per minute per account and role. Logout revokes the current session. The `0005_security_state` migration adds the throttle and revocation tables.
- Private V2Board destinations require an exact allowlisted origin in `PANEL_PRIVATE_ORIGINS`. Public production origins use HTTPS. The client checks the IP used for each connection, rejects redirects and unsafe addresses, and requires an uncompressed response of at most 2 MiB. Invalid upstream data does not establish that a subscription was deleted.
- Keep backups and environment files outside version control. Verify the published installer hash before running it as root.

## Release notes

See the [Installer v1.0.5 release notes](dev/docs/releases/v1.0.5/RELEASE_NOTES.md) for financial fixes, upgrade instructions, and known limitations.
