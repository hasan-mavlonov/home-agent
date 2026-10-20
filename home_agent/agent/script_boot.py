"""Bootstrap for sandboxed workspace scripts. Not imported by the app: its source is passed
to `python -I -S -c`, so the sandbox never needs to read the app's own files.

argv: [config_json, script_path, *script_args]

It sets resource limits and a wall-clock alarm, installs an audit hook that refuses
network, process creation, native-code loading and file access outside the allowed
folders, runs the script as __main__, and records the exit status in the run folder.
The audit hook is a second layer: the operating-system sandbox (Seatbelt on macOS,
bubblewrap on Linux) is the boundary that holds even against deliberately hostile code.
"""

import json
import os
import runpy
import sys
import traceback

CFG = json.loads(sys.argv[1])
SCRIPT = sys.argv[2]
ARGS = sys.argv[3:]


def _real(path):
    return os.path.realpath(os.fsdecode(path))


WRITE_ROOTS = tuple(_real(p) for p in CFG["write_roots"])
READ_ROOTS = WRITE_ROOTS + tuple(_real(p) for p in CFG["read_roots"])
PROTECTED = tuple(_real(p) for p in CFG["protected"])  # no reads here except READ_ROOTS
BLOCKED_IMPORTS = {"ctypes", "_ctypes", "_posixsubprocess", "multiprocessing", "_multiprocessing", "cffi", "_cffi_backend"}
BLOCKED_PREFIXES = ("socket.", "subprocess.", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "pty.",
                    "ctypes.", "webbrowser.", "os.startfile")  # fmt: skip
BLOCKED_EVENTS = {"os.system", "os.kill", "os.killpg", "os.symlink", "os.link", "code.__new__", "sys.remote_exec"}
WRITE_EVENTS = {"os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.chmod", "os.chown", "os.truncate", "os.utime",
                "os.chflags", "os.lchflags", "os.lchmod", "os.mkfifo", "os.mknod", "os.setxattr", "os.removexattr",
                "shutil.rmtree", "shutil.chown"}  # fmt: skip
LIST_EVENTS = {"os.listdir", "os.scandir", "os.chdir", "glob.glob", "glob.glob/2", "os.listxattr", "os.getxattr"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _inside(path, roots):
    real = _real(path)
    return any(real == root or real.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def _deny(what):
    raise PermissionError(f"Sandbox: {what} is not allowed for workspace scripts")


def _check_read(path):
    if _inside(path, PROTECTED) and not _inside(path, READ_ROOTS):
        _deny(f"reading {os.fsdecode(path)}")


def _check_write(path):
    if not _inside(path, WRITE_ROOTS):
        _deny(f"writing {os.fsdecode(path)} (only the workspace is writable)")


def _hook(event, args):
    if event == "import":
        if args[0] and args[0].split(".")[0] in BLOCKED_IMPORTS:
            _deny(f"importing {args[0]}")
        return
    if event in BLOCKED_EVENTS or event.startswith(BLOCKED_PREFIXES):
        _deny(event.replace("socket.", "network access: socket."))
    if event == "open":
        path, mode, flags = args
        if path is None or isinstance(path, int):
            return
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool((flags or 0) & WRITE_FLAGS)
        (_check_write if writing else _check_read)(path)
    elif event in WRITE_EVENTS:
        for value in args:
            if isinstance(value, (str, bytes, os.PathLike)):
                _check_write(value)
    elif event in LIST_EVENTS:
        if args and isinstance(args[0], (str, bytes, os.PathLike)):
            _check_read(args[0])


def _limit(name, value):
    try:
        import resource
    except ImportError:
        return
    which = getattr(resource, name, None)
    if which is None:
        return
    try:
        _, hard = resource.getrlimit(which)
        value = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError):
        pass


_limit("RLIMIT_CPU", CFG["cpu_seconds"])
_limit("RLIMIT_FSIZE", CFG["max_file_bytes"])
_limit("RLIMIT_NOFILE", 256)
_limit("RLIMIT_CORE", 0)
if sys.platform.startswith("linux"):
    _limit("RLIMIT_AS", CFG["max_memory_bytes"])
_limit("RLIMIT_NPROC", 0)  # no new processes (ignored for root)
try:
    import signal

    signal.alarm(CFG["wall_seconds"])  # ends the run even if the server that started it is gone
except (ImportError, AttributeError, ValueError):
    pass

exit_record = os.path.join(CFG["run_dir"], "exit.json")
script_dir = os.path.dirname(os.path.abspath(SCRIPT))
if CFG.get("audit_hook", True):
    sys.addaudithook(_hook)

code = 0
sys.argv = [SCRIPT, *ARGS]
sys.path.insert(0, script_dir)
try:
    runpy.run_path(SCRIPT, run_name="__main__")
except SystemExit as exc:
    if exc.code is None:
        code = 0
    elif isinstance(exc.code, int):
        code = exc.code
    else:
        print(exc.code, file=sys.stderr)
        code = 1
except BaseException:  # noqa: BLE001 - report every failure as the script's own error
    traceback.print_exc()
    code = 1
finally:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    with open(exit_record, "w", encoding="utf-8") as handle:
        json.dump({"exit_code": code}, handle)
os._exit(code & 0xFF if code >= 0 else 1)
