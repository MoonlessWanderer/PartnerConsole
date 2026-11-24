"""Answer the official installer's prompts on a disposable runner."""
import errno
import os
import pty
import select
import sys
from pathlib import Path

password = Path(sys.argv[1]).read_bytes().strip()
answers = [
    (b"HTTPS origin: ", b"\n"),
    (b"Initial admin password (at least 12 characters, not saved): ", password + b"\n"),
    (b"Repeat password: ", password + b"\n"),
]
pid, fd = pty.fork()
if pid == 0:
    os.execvp(sys.argv[2], sys.argv[2:])
pending = b""
while select.select([fd], [], [], 600)[0]:
    try:
        chunk = os.read(fd, 4096)
    except OSError as exc:
        if exc.errno == errno.EIO:
            break
        raise
    if not chunk:
        break
    sys.stdout.buffer.write(chunk.replace(password, b"[redacted]"))
    sys.stdout.buffer.flush()
    pending += chunk
    if answers and answers[0][0] in pending:
        prompt, answer = answers.pop(0)
        pending = pending.split(prompt, 1)[1]
        os.write(fd, answer)
    pending = pending[-4096:]
else:
    os.kill(pid, 15)
    raise SystemExit("installer did not answer within ten minutes")
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
raise SystemExit(code or bool(answers))
