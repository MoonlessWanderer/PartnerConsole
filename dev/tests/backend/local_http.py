"""Own a loopback-only Uvicorn child and let the OS allocate its port."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4


TESTS = Path(__file__).resolve().parent
DEV = TESTS.parents[1]
BACKEND = DEV.parent / "backend"
LISTENING = re.compile(r"Uvicorn running on (http://127\.0\.0\.1:\d+)")


@contextmanager
def local_server(workdir, target, *, env=None):
    # Keep inherited database settings and .env out of the child. Working
    # directories are owned by pytest and this helper never reuses a server.
    child_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
                 if key in os.environ}
    child_env.update({
        "PYTHONPATH": os.pathsep.join(map(str, (BACKEND, DEV, TESTS))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": "sqlite://",
    })
    child_env.update(env or {})
    log = workdir / f"{target.replace(':', '-')}-{uuid4().hex}.log"
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "uvicorn", target, "--host", "127.0.0.1",
             "--port", "0", "--no-access-log", "--no-proxy-headers"],
            cwd=workdir, env=child_env, stdout=sink, stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{target} exited before becoming ready (exit {process.returncode})")
            match = LISTENING.search(log.read_text(encoding="utf-8", errors="replace"))
            if match:
                yield match.group(1), process
                return
            time.sleep(0.05)
        raise RuntimeError(f"{target} did not become ready")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
