#!/usr/bin/env python3
"""Build the three release assets from an explicit Git commit."""
import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])

def replace_field(data, field, value):
    result, count = re.subn(rb"^" + field + rb'="[^"\r\n]*"$', field + b'="' + value + b'"', data, flags=re.M)
    if count != 1:
        raise ValueError(f"Missing or duplicate field {field!r}")
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit", help="full Git commit SHA")
    parser.add_argument("tag", help="stable semantic version, such as v1.0.9")
    parser.add_argument("output", type=Path, help="new output directory")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit) or git("cat-file", "-t", args.commit).strip() != b"commit":
        parser.error("commit must identify an exact Git commit")
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}", args.tag):
        parser.error("tag must be a stable version")

    def blob(path):
        return git("show", f"{args.commit}:{path}")

    paths = blob("dev/release/runtime-files.txt").decode().splitlines()
    tree = {}
    for entry in git("ls-tree", "-rz", args.commit).split(b"\x00"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            tree[name.decode()] = metadata.split()[0]
    if paths != sorted(set(paths)) or not paths:
        parser.error("runtime file list must be sorted, unique and nonempty")
    versions = args.tag[1:]
    payload = {}
    for path in paths:
        segments = path.split("/")
        if (not all(re.fullmatch(r"[A-Za-z0-9_\[][A-Za-z0-9_.\[\]-]*", item) for item in segments)
            or set(segments) & {"dev", "tests", "data", "logs", "runtime", "backups", "node_modules", "partnerconsole.sh"}
            or path in {"PACKAGE.json", "config/api.env", "config/web.env"}
            or tree.get(path) not in {b"100644", b"100755"}):
            parser.error(f"invalid runtime file {path}")
        data = blob(path)
        if path in {"package.json", "package-lock.json"}:
            fields = json.loads(data)
            fields["version"] = versions
            if path == "package-lock.json":
                fields["packages"][""]["version"] = versions
            data = (json.dumps(fields, indent=2, ensure_ascii=False) + "\n").encode()
        elif path == "backend/pyproject.toml":
            count = 0
            data, count = re.subn(rb'^version = "[^"]+"$', f'version = "{versions}"'.encode(), data, flags=re.M)
            if count != 1:
                parser.error("backend version field not found")
        payload[path] = (data, 0o755 if tree[path] == b"100755" else 0o644)
    payload["PACKAGE.json"] = (json.dumps({"tag": args.tag, "commit": args.commit}, sort_keys=True).encode(), 0o644)
    args.output.mkdir(parents=True, exist_ok=False)
    archive = args.output / f"partnerconsole-{args.tag}.tar.gz"
    with archive.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
            for path, (data, permissions) in sorted(payload.items()):
                member = tarfile.TarInfo(path)
                member.mode = permissions
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
    installer = args.output / "partnerconsole.sh"
    template = blob("partnerconsole.sh")
    for field, value in (
        (b"RELEASE_TAG", args.tag.encode()),
        (b"RELEASE_SOURCE_COMMIT", args.commit.encode()),
        (b"RELEASE_ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest().encode()),
    ):
        template = replace_field(template, field, value)
    installer.write_bytes(template)
    installer.chmod(0o755)
    sums = "".join(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n" for file in (installer, archive))
    (args.output / "SHA256SUMS").write_bytes(sums.encode())
    print(sums, end="")

if __name__ == "__main__":
    main()
