# Installer regression fixtures

These archives supply isolated inputs for current update, service identity, browser origin, certificate activation, and administrator credential regressions. CI verifies the archives before extracting them. The update suite assigns a local tag to the fixture tree and recalculates its installer tree pin for that reduced tree.

They contain the runtime portions needed by the exercises. The RC4 and RC5 archives contain only the exact scripts used by their comparisons.

| Fixture | Source commit at capture | Files |
| --- | --- | --- |
| `v1.0.3` | `bd6d44e22530e2fddde80ae24078ce8aa96a5e94` | 116 |
| `v1.0.3-rc.4` | `2862db51f5ee364d77c21aeaf2887e4f855f3f8f` | 1 |
| `v1.0.3-rc.5` | `05b2d3be90e4b8d7c90e868870603aeac0dd2344` | 2 |
| `v1.0.3-rc.6` | `f3835e384992c2007d839eb12f538fd2274023f8` | 116 |
| `v1.0.3-rc.7` | `71cff9738ef25bab89c380da1576e562783da777` | 116 |
