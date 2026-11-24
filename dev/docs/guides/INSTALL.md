# Manual installation of PartnerConsole from source

For a managed installation or upgrade on Ubuntu 24.04, download and verify the target version's installer and checksums by following [Install and update in the README](../../../README.md#install-and-update).

This guide explains how to run `install.sh` from a verified source tree and prepare the application settings, database, and services yourself.

Choose an installation directory that the service account can write to. The path must have no spaces or line breaks. The script changes to the directory that contains it and keeps configuration, data, logs, caches, temporary files, and backups under that directory. You can invoke the script by its path from a working directory that is read-only. If you move the installation, stop the services first and run `bash install.sh` again from the new location to rebuild the Python virtual environment if needed. If your database URL contains an absolute path to the old location, update it in `config/api.env` before starting the services. The installer sets the shell scripts to mode `755`, and `restart.sh` invokes the stop and start scripts through Bash.

The Next.js application lives in the root of the installation directory (`app/`, `components/`, `lib/`, `public/`, and `package.json`), with no separate `frontend/` directory.

## What the default installation does not configure

Make sure Python 3.12 or newer, Node.js 22 or newer, and npm are already on your `PATH`. Python must be able to create a virtual environment with `venv`. Starting the services also requires `ss` and `curl`. System packages, MySQL 8.4, Nginx, TLS, and systemd are managed by the operator. The source installer keeps its configuration and generated files inside the installation directory.

## Installation steps

```bash
bash install.sh
```

To use a SQLite database inside the installation directory, run this command instead.

```bash
bash install.sh --local-database
```

When `config/api.env` does not yet exist, this option sets its database URL to the absolute path of `data/partnerconsole.db` under the installation directory. Bootstrap initializes the database. The development database at `backend/data/partner.db` is not used.

Edit `config/api.env` and `config/web.env`. Replace all `REPLACE_WITH` placeholders and the example hostname. Set `NEXT_PUBLIC_API_BASE` to the public origin only, without `/api`. The client adds that prefix. Run `bash install.sh` again after setting or changing this origin to rebuild the web application.

With `ENVIRONMENT=production`, use exact HTTPS origins in `CORS_ORIGINS` for public browser access. For SSH tunnel access without a domain, the application accepts the explicitly configured loopback HTTP origins `http://127.0.0.1:3741`, `http://localhost:3741`, and `http://[::1]:3741`. Keep the administrator password out of the configuration files.

```bash
bash scripts/bootstrap.sh --confirm-database
bash start.sh
bash status.sh
```

Bootstrap prints the database target without its password before making database changes. Without `--confirm-database`, it exits without writing to the database. Production workers check the schema on startup. Run migrations through the maintenance bootstrap.

The API listens on `127.0.0.1:8741` with two workers, and the web application listens on `127.0.0.1:3741`. Open the administrator sign-in page at `/gate/k3p8n2` on the web application. The API health endpoint is `http://127.0.0.1:8741/api/health`. Its response confirms that the API is serving requests. It does not recheck the database connection after startup.

## Host-managed MySQL

If you need MySQL instead of SQLite, install MySQL 8.4 on the host and bind it to `127.0.0.1` only. The application uses `READ COMMITTED`, UTC, `utf8mb4`, and the `utf8mb4_0900_bin` collation for MySQL connections. Create the empty schema before running bootstrap. The source installer does not create it. See the SQL example below and the [operations guide](OPERATIONS.md).

## Render host templates

```bash
bash scripts/render-host-templates.sh
```

The script writes the templates to `deploy/generated/` inside the installation directory. The operator copies any required files into the service manager or web server configuration. See [portable installation](PORTABLE.md) and the [operations guide](OPERATIONS.md).

## Install on a production host

This procedure has been tested on Ubuntu 24.04 with Python 3.12, Node.js 22, a non-root service account, and MySQL 8.4 bound to `127.0.0.1`. For a fresh installation, create a dedicated, empty database.

Install MySQL on the host and keep ports `3306`, `8741`, and `3741` closed to the public network. Keep only SSH publicly reachable until you intentionally configure Nginx and TLS for public access.

```bash
useradd --system --create-home --home-dir /srv/ppv1 --shell /bin/bash ppv1
install -d -o ppv1 -g ppv1 -m 755 /srv/partnerconsole
```

Place the verified source tree for this version in `/srv/partnerconsole` and set its ownership to `ppv1`. Keep the account's home directory separate from the application directory so stray files in the home directory are easy to spot.

As a MySQL administrator connected through the local socket, create an empty schema and a user whose privileges are limited to that schema.

```sql
CREATE DATABASE partnerconsole CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
CREATE USER 'partnerconsole'@'127.0.0.1' IDENTIFIED BY 'REPLACE_WITH_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES
  ON partnerconsole.* TO 'partnerconsole'@'127.0.0.1';
```

Before the first run of `install.sh`, create both files with mode `600` and ownership set to `ppv1`. Keep the administrator password out of both files. Replace `REPLACE_WITH` placeholders and `panel.example.com` before continuing. The installer skips the web build if `config/web.env` still contains either placeholder. Bootstrap and startup reject placeholders in either file.

`config/api.env`

```text
ENVIRONMENT=production
DATABASE_URL=mysql+pymysql://partnerconsole:REPLACE_WITH_PASSWORD@127.0.0.1:3306/partnerconsole?charset=utf8mb4
SECRET_KEY=REPLACE_WITH_LONG_RANDOM
ADMIN_JWT_SECRET=REPLACE_WITH_DIFFERENT_LONG_RANDOM
PARTNER_JWT_SECRET=REPLACE_WITH_THIRD_LONG_RANDOM
PANEL_ENCRYPTION_KEY=
ADMIN_USERNAME=admin
ACCESS_TOKEN_EXPIRE_MINUTES=720
CORS_ORIGINS=https://panel.example.com
API_RATE_LIMIT_PER_MINUTE=120
PANEL_PRIVATE_ORIGINS=
```

Percent-encode reserved characters in the database password before inserting it into `DATABASE_URL`. Use three distinct, long, randomly generated signing keys. Replace `https://panel.example.com` in `CORS_ORIGINS` with the exact HTTPS origin used by the browser, without a path.

`config/web.env`

```text
NEXT_PUBLIC_API_BASE=https://panel.example.com
```

Set `NEXT_PUBLIC_API_BASE` to the actual HTTPS origin. The client adds `/api` automatically. The origin is embedded in the web build, so run `bash install.sh` again if it changes.

```bash
runuser -u ppv1 -- bash /srv/partnerconsole/install.sh
runuser -u ppv1 -- bash /srv/partnerconsole/scripts/bootstrap.sh
```

The second command omits `--confirm-database` and should exit with a nonzero status without creating any tables. Review the database name that it prints. Continue only if it is the dedicated, empty schema you intend to initialize.

```bash
runuser -u ppv1 -- env ADMIN_PASSWORD='REPLACE_WITH_INITIAL_ADMIN_PASSWORD' \
  bash /srv/partnerconsole/scripts/bootstrap.sh --confirm-database
unset ADMIN_PASSWORD
runuser -u ppv1 -- bash /srv/partnerconsole/start.sh
runuser -u ppv1 -- bash /srv/partnerconsole/status.sh
```

Use an initial password of at least 12 characters and do not use `admin12345`. The environment assignment supplies the password to that bootstrap process. Production workers do not run migrations on startup. The expected Alembic head is `0005_security_state`.

Verify the services from the server itself.

```bash
curl -fsS http://127.0.0.1:8741/api/health
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3741/
```

Expect `{"ok": true}` from the API and HTTP `200` from the web application. These checks do not recheck the database connection after startup. Use `bash stop.sh`, `bash restart.sh`, and `bash status.sh` to manage the processes. Both installations would need ports `8741` and `3741`; stop the first installation before starting the second.

The `bash scripts/render-host-templates.sh` command generates files only. To start the application at boot, install and enable the generated systemd units. To expose it over HTTPS, configure Nginx with the actual hostname and certificate paths. Keep the application ports on loopback.

On a normally restricted host, a non-root service account cannot access an installation under `/root/partnerconsole`. Use an accessible directory such as `/srv/partnerconsole` and leave the permissions on `/root` intact.
