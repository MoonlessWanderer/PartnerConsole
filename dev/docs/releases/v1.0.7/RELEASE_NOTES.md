# PartnerConsole v1.0.7

The installed command `sudo partnerconsole update` and option **5) Update**
now look up the latest published stable release in the official GitHub
repository. For a newer release, Update verifies the installer against the
SHA256 digest in GitHub release metadata and checks the pinned tag before
running the installer. The installer still verifies the release tree and
checks for modified managed files. Update refuses downgrades and keeps a
verified current installation running without a reinstall or restart.

From v1.0.6 and earlier, download the v1.0.7 installer and SHA256SUMS for a
one-time upgrade. Follow the [upgrade instructions](../../../../README.md#upgrade-an-existing-installation).
This release adds no database migration or financial backfill.
