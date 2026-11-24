#!/usr/bin/env bash
# Used only when preparing historical Git-based test baselines.
tree_sha256() {
  python3 - "$1" <<'PY'
import hashlib, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
paths = sorted(item.decode("utf-8") for item in raw.split(b"\0") if item)
digest = hashlib.sha256()
for rel in paths:
    if rel.startswith("../") or "/../" in rel or rel.startswith("/"):
        raise SystemExit(2)
    path = root / rel
    if path.is_symlink():
        data = b"symlink\0" + str(path.readlink()).encode("utf-8")
    else:
        data = path.read_bytes()
    if rel == "partnerconsole.sh":
        lines = []
        for line in data.splitlines(keepends=True):
            if line.startswith(b"RELEASE_TREE_SHA256="):
                ending = b"\r\n" if line.endswith(b"\r\n") else (b"\n" if line.endswith(b"\n") else b"")
                line = b'RELEASE_TREE_SHA256=""' + ending
            lines.append(line)
        data = b"".join(lines)
    digest.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
print(digest.hexdigest())
PY
}
