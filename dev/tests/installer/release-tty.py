"""Answer the official installer's prompts on a disposable runner."""
import argparse
import errno
import os
import pty
import select
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=("install", "interrupt", "resume", "repeat", "menu"), default="install")
parser.add_argument("password", type=Path)
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()

password = args.password.read_bytes().strip()
origin = b"HTTPS origin: "
admin = b"Initial admin password (at least 12 characters, not saved): "
confirm = b"Repeat password: "
choice = b"Choice: "
pause = b"Press Enter to return to the menu... "
answers = [(origin, b"\n"), (admin, password + b"\n"), (confirm, password + b"\n")]
required = {
    "install": {origin, admin, confirm},
    "interrupt": {origin, admin},
    "resume": {admin, confirm},
    "repeat": set(),
    "menu": {choice, pause},
}[args.mode]
if args.mode == "interrupt":
    answers = [(origin, b"\n"), (admin, b"\x03")]
elif args.mode == "menu":
    answers += [(choice, b"1\n"), (pause, b"\n"), (choice, b"0\n")]

pid, fd = pty.fork()
if pid == 0:
    os.execvp(args.command[0], args.command)
pending = b""
seen = set()
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
    while True:
        found = [(pending.index(prompt), prompt) for prompt in (origin, admin, confirm, choice, pause) if prompt in pending]
        if not found:
            break
        index, prompt = min(found)
        pending = pending[index + len(prompt):]
        seen.add(prompt)
        for i, (expected, reply) in enumerate(answers):
            if expected == prompt and (args.mode != "repeat" and not (args.mode == "resume" and prompt == origin)):
                answers.pop(i)
                os.write(fd, reply)
                break
        else:
            os.write(fd, b"\x03")
            _, _ = os.waitpid(pid, 0)
            raise SystemExit("installer requested an unexpected input")
    pending = pending[-4096:]
else:
    os.kill(pid, 15)
    raise SystemExit("installer did not answer within ten minutes")
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
if required - seen:
    raise SystemExit(code if code > 0 else 1)
if args.mode == "interrupt":
    if code == 0:
        raise SystemExit("installer did not stop when interrupted at the administrator prompt")
    print("Stopped at the administrator password prompt.")
    raise SystemExit(0)
raise SystemExit(code)
