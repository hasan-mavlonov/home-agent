"""Run Python scripts from the agent workspace inside a sandbox.

The agent never gets a shell. It can only run a `.py` file that is already in its
workspace, with the standard library, under these limits:

* Operating-system sandbox (the real boundary):
  - macOS: Seatbelt via /usr/bin/sandbox-exec. No network, no new processes, writes only
    to the workspace and the run's own folder, no reads inside your home folder except
    the workspace and Python itself.
  - Linux: bubblewrap (`bwrap`) with every namespace unshared (so no network), the file
    system read-only, the home folder hidden, and only the workspace writable.
* Inside Python (a second layer, and the only one in SCRIPT_SANDBOX=basic): an audit hook
  that refuses sockets, subprocesses, native-code loading and file access outside those
  folders; CPU, memory, file-size and process limits; a wall-clock alarm.
* A clean environment: no API keys or other variables from the server are passed on.

Every run has a folder under <data dir>/runs/<run id>/ holding its metadata, the
workspace state before the run, stdout, stderr, and the exit status. After a crash or
restart, `recover()` reports the outcome from that folder instead of running the script
a second time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import REPO_ROOT, Settings
from ..services.workspace import Workspace, WorkspaceError

MODES = ("auto", "seatbelt", "bwrap", "basic", "off")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
BOOT_SOURCE = (Path(__file__).with_name("script_boot.py")).read_text(encoding="utf-8")
OUTPUT_LIMIT = 100_000  # bytes of stdout / stderr kept per run
MAX_FILE_BYTES = 50_000_000
MAX_MEMORY_BYTES = 2_000_000_000
GRACE_SECONDS = 5
SNAPSHOT_LIMIT = 5000
REPORTED_FILES = 50

SEATBELT_PROFILE = """(version 1)
(deny default)
(allow process-exec)
(allow signal (target self))
(allow sysctl-read)
(allow file-read-metadata)
(allow file-read* (require-all (subpath "/") (require-not (subpath (param "HOME_DIR")))))
(allow file-read* (subpath (param "WS")) (subpath (param "RUN")) (subpath (param "PY_PREFIX")) (subpath (param "PY_BASE")))
(allow file-write* (subpath (param "WS")) (subpath (param "RUN")))
(allow file-write-data (literal "/dev/null"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow ipc-posix-sem)
"""


@dataclass
class ScriptResult:
    run_id: str
    script: str
    status: str  # exited | timed_out | killed | interrupted | unknown
    exit_code: int | None
    duration: float
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    files: list[dict] = field(default_factory=list)
    recovered: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "exited" and self.exit_code == 0

    def to_dict(self) -> dict:
        return asdict(self)


class ScriptError(Exception):
    pass


def resolve_mode(setting: str) -> tuple[str, str]:
    """(mode, explanation). mode is one of seatbelt, bwrap, basic, off."""
    setting = (setting or "auto").strip().lower()
    if setting == "off":
        return "off", "turned off (SCRIPT_SANDBOX=off)"
    if setting == "basic":
        return "basic", "in-process limits only (SCRIPT_SANDBOX=basic); weaker than an operating-system sandbox"
    has_seatbelt = sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)
    has_bwrap = sys.platform.startswith("linux") and shutil.which("bwrap") is not None
    if setting == "seatbelt":
        return ("seatbelt", "macOS Seatbelt sandbox") if has_seatbelt else ("off", f"{SANDBOX_EXEC} is not available")
    if setting == "bwrap":
        return ("bwrap", "bubblewrap sandbox") if has_bwrap else ("off", "bwrap is not installed")
    if has_seatbelt:
        return "seatbelt", "macOS Seatbelt sandbox"
    if has_bwrap:
        return "bwrap", "bubblewrap sandbox"
    return "off", ("no operating-system sandbox found (on Linux install bubblewrap); SCRIPT_SANDBOX=basic accepts a "
                   "weaker in-process sandbox")  # fmt: skip


def run_id_for(task_id: int, call_id: str) -> str:
    return f"t{task_id}-{hashlib.sha256(call_id.encode()).hexdigest()[:16]}"


def _real(path: Path | str) -> str:
    return os.path.realpath(path)


def _snapshot(root: Path) -> dict[str, list[int]]:
    files: dict[str, list[int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            try:
                stat = path.lstat()
            except OSError:
                continue
            if not path.is_file() or path.is_symlink():
                continue
            files[path.relative_to(root).as_posix()] = [stat.st_size, stat.st_mtime_ns]
            if len(files) >= SNAPSHOT_LIMIT:
                return files
    return files


def _sha256(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _read_capped(path: Path) -> tuple[str, bool]:
    try:
        with path.open("rb") as handle:
            data = handle.read(OUTPUT_LIMIT + 1)
    except OSError:
        return "", False
    return data[:OUTPUT_LIMIT].decode("utf-8", errors="replace"), len(data) > OUTPUT_LIMIT


class ScriptRunner:
    def __init__(self, settings: Settings, workspace: Workspace):
        self.workspace = workspace
        self.mode, self.reason = resolve_mode(settings.script_sandbox)
        self.max_seconds = settings.script_max_seconds
        self.runs_dir = settings.data_dir / "runs"
        self.python = sys.executable
        self.home = _real(Path.home())
        self.protected = sorted({self.home, _real(REPO_ROOT), _real(settings.data_dir)})
        self.read_roots = sorted({_real(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)})

    @property
    def available(self) -> bool:
        return self.mode != "off"

    def describe(self) -> dict:
        return {"enabled": self.available, "mode": self.mode, "detail": self.reason, "max_seconds": self.max_seconds}

    # -- running ---------------------------------------------------------------------------
    def _command(self, script: Path, args: list[str], run_dir: Path, timeout: int, audit_hook: bool = True) -> list[str]:
        config = {
            "audit_hook": audit_hook,
            "write_roots": [_real(self.workspace.root), _real(run_dir)],
            "read_roots": self.read_roots,
            "protected": self.protected,
            "run_dir": _real(run_dir),
            "cpu_seconds": timeout + 1,
            "wall_seconds": timeout + 2,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_memory_bytes": MAX_MEMORY_BYTES,
        }
        python = [self.python, "-I", "-S", "-B", "-X", "utf8", "-c", BOOT_SOURCE, json.dumps(config), str(script), *args]
        if self.mode == "seatbelt":
            params = {"WS": _real(self.workspace.root), "RUN": _real(run_dir), "HOME_DIR": self.home,
                      "PY_PREFIX": _real(sys.prefix), "PY_BASE": _real(sys.base_prefix)}  # fmt: skip
            return [SANDBOX_EXEC, "-p", SEATBELT_PROFILE, *[x for k, v in params.items() for x in ("-D", f"{k}={v}")], *python]
        if self.mode == "bwrap":
            cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all", "--ro-bind", "/", "/", "--dev", "/dev",
                   "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", self.home]  # fmt: skip
            for root in self.read_roots:
                cmd += ["--ro-bind", root, root]
            for root in (_real(self.workspace.root), _real(run_dir)):
                cmd += ["--bind", root, root]
            return [*cmd, "--chdir", _real(self.workspace.root), *python]
        return python

    def _environment(self, run_dir: Path) -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "HOME": str(run_dir / "home"), "TMPDIR": str(run_dir / "tmp"),
                "LANG": "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"}  # fmt: skip

    def resolve_script(self, relative: str) -> Path:
        path = self.workspace.resolve(relative)
        if path.suffix.lower() != ".py":
            raise ScriptError("Only .py files in the workspace can be run")
        if not path.is_file():
            raise ScriptError(f"No such file in the workspace: {relative}. Write it with write_file first.")
        return path

    async def run(self, relative: str, args: list[str], timeout: int, run_id: str, *, audit_hook: bool = True) -> ScriptResult:
        if not self.available:
            raise ScriptError(f"Running scripts is unavailable: {self.reason}")
        try:
            script = self.resolve_script(relative)
        except WorkspaceError as exc:
            raise ScriptError(str(exc)) from None
        timeout = max(1, min(int(timeout), self.max_seconds))
        run_dir = self.runs_dir / run_id
        if run_dir.exists():
            shutil.rmtree(run_dir)  # a previous attempt that never started the script
        for sub in ("home", "tmp"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)
        os.chmod(self.runs_dir, 0o700)
        started = time.time()
        (run_dir / "before.json").write_text(json.dumps(_snapshot(self.workspace.root)))
        (run_dir / "meta.json").write_text(json.dumps({"script": relative, "args": args, "timeout": timeout,
                                                       "started_at": started, "mode": self.mode}))  # fmt: skip
        status = "exited"
        with (run_dir / "stdout").open("wb") as out, (run_dir / "stderr").open("wb") as err:
            proc = await asyncio.create_subprocess_exec(
                *self._command(script, args, run_dir, timeout, audit_hook), cwd=self.workspace.root, env=self._environment(run_dir),
                stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True,
            )  # fmt: skip
            (run_dir / "pid").write_text(str(proc.pid))
            try:
                await asyncio.wait_for(proc.wait(), timeout + GRACE_SECONDS)
            except asyncio.TimeoutError:
                status = "timed_out"
            except asyncio.CancelledError:
                # The task was cancelled, or the server is shutting down: stop the script and record
                # that it was interrupted, so a later recovery reports this instead of running it again.
                _kill_group(proc.pid)
                try:
                    await asyncio.shield(proc.wait())
                except asyncio.CancelledError:
                    pass
                interrupted = self._result(run_id, run_dir, status="interrupted", returncode=proc.returncode,
                                           duration=time.time() - started)  # fmt: skip
                (run_dir / "result.json").write_text(json.dumps(interrupted.to_dict()))
                raise
            finally:
                if proc.returncode is None:
                    _kill_group(proc.pid)
                    try:
                        await asyncio.shield(proc.wait())
                    except asyncio.CancelledError:
                        pass
        result = self._result(run_id, run_dir, status=status, returncode=proc.returncode, duration=time.time() - started)
        (run_dir / "result.json").write_text(json.dumps(result.to_dict()))
        return result

    def _result(self, run_id: str, run_dir: Path, *, status: str, returncode: int | None, duration: float,
                recovered: bool = False) -> ScriptResult:  # fmt: skip
        meta = json.loads((run_dir / "meta.json").read_text())
        exit_code = None
        exit_file = run_dir / "exit.json"
        if exit_file.exists():
            try:
                exit_code = int(json.loads(exit_file.read_text())["exit_code"])
            except (ValueError, KeyError, TypeError):
                exit_code = None
        if status == "exited":
            if exit_code is None and returncode is not None and returncode >= 0:
                exit_code = returncode
            if exit_code is None:
                status = "killed"  # by a signal: the CPU, memory or wall-clock limit, or the sandbox
        stdout, out_cut = _read_capped(run_dir / "stdout")
        stderr, err_cut = _read_capped(run_dir / "stderr")
        if status == "killed" and returncode is not None and returncode < 0:
            stderr += f"\n[Home Agent] The script was stopped by signal {-returncode}."
        return ScriptResult(run_id=run_id, script=meta["script"], status=status, exit_code=exit_code,
                            duration=round(duration, 3), stdout=stdout, stderr=stderr, stdout_truncated=out_cut,
                            stderr_truncated=err_cut, files=self._changes(run_dir), recovered=recovered)  # fmt: skip

    def _changes(self, run_dir: Path) -> list[dict]:
        try:
            before = json.loads((run_dir / "before.json").read_text())
        except (OSError, ValueError):
            before = {}
        after = _snapshot(self.workspace.root)
        changes = []
        for path in sorted(set(before) | set(after)):
            if path not in after:
                changes.append({"path": path, "change": "deleted"})
            elif path not in before or before[path] != after[path]:
                full = self.workspace.root / path
                changes.append({"path": path, "change": "created" if path not in before else "modified",
                                "size": after[path][0], "sha256": _sha256(full)})  # fmt: skip
        return changes[:REPORTED_FILES]

    # -- recovery --------------------------------------------------------------------------
    async def recover(self, run_id: str) -> ScriptResult | None:
        """Outcome of a run whose server was interrupted. None if the script never started
        (it is then safe to run). Never starts the script again."""
        run_dir = self.runs_dir / run_id
        if (run_dir / "result.json").exists():
            data = json.loads((run_dir / "result.json").read_text())
            return ScriptResult(**{**data, "recovered": True})
        if not (run_dir / "pid").exists():
            return None
        meta = json.loads((run_dir / "meta.json").read_text())
        deadline = meta["started_at"] + meta["timeout"] + GRACE_SECONDS
        while not (run_dir / "exit.json").exists() and time.time() < deadline:
            await asyncio.sleep(0.25)
        status = "exited" if (run_dir / "exit.json").exists() else "unknown"
        if status == "exited":
            await asyncio.sleep(0.2)  # let the process finish flushing after writing its exit record
        result = self._result(run_id, run_dir, status=status, returncode=None,
                              duration=time.time() - meta["started_at"], recovered=True)  # fmt: skip
        (run_dir / "result.json").write_text(json.dumps(result.to_dict()))
        return result

    # -- self test -------------------------------------------------------------------------
    async def self_test(self) -> list[tuple[str, bool, str]]:
        """Run a probe script and check that the sandbox allows and refuses what it should. With an
        operating-system sandbox the probe also runs once without the in-process guard, so a gap in
        the OS layer cannot hide behind the guard."""
        if not self.available:
            return [("Script sandbox", False, self.reason)]
        layers = [("", True)] if self.mode == "basic" else [(f" ({self.mode} alone)", False), ("", True)]
        checks: list[tuple[str, bool, str]] = []
        for suffix, hook in layers:
            checks += await self._probe(suffix, hook)
        return checks

    async def _probe(self, suffix: str, audit_hook: bool) -> list[tuple[str, bool, str]]:
        probe_dir = "home-agent-sandbox-check"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        secret = self.runs_dir / "probe-secret.txt"
        secret.write_text("secret")
        outside = Path(self.home) / ".home-agent-sandbox-probe"
        code = PROBE_SOURCE.replace("@SECRET@", repr(str(secret))).replace("@OUTSIDE@", repr(str(outside)))
        self.workspace.write_text(f"{probe_dir}/probe.py", code)
        try:
            result = await self.run(f"{probe_dir}/probe.py", [], 30, f"selftest-{time.time_ns()}", audit_hook=audit_hook)
        finally:
            shutil.rmtree(self.workspace.root / probe_dir, ignore_errors=True)
            secret.unlink(missing_ok=True)
        checks = [("Script runs" + suffix, result.ok and "ran:ok" in result.stdout,
                   f"exit {result.exit_code}, status {result.status}" + (f": {result.stderr.strip()[-300:]}" if not result.ok else ""))]  # fmt: skip
        for key, label in (("network", "Network blocked"), ("subprocess", "New processes blocked"),
                           ("write_outside", "Writes outside the workspace blocked"),
                           ("read_private", "Reading Home Agent's private files blocked"),
                           ("write_inside", "Writes inside the workspace allowed")):  # fmt: skip
            match = re.search(rf"^{key}:(\w+)", result.stdout, re.M)
            ok = bool(match) and match.group(1) == ("ok" if key == "write_inside" else "blocked")
            checks.append((label + suffix, ok, match.group(1) if match else "no result"))
        if outside.exists():
            outside.unlink()
            checks.append(("Writes outside the workspace blocked" + suffix, False, "the probe wrote to your home folder"))
        return checks


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


PROBE_SOURCE = """
import os, sys
print("ran:ok")
def attempt(key, fn):
    try:
        fn()
        print(key + ":allowed")
    except Exception:
        print(key + ":blocked")
def net():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(("1.1.1.1", 443))
def proc():
    import subprocess
    subprocess.run(["/bin/echo", "hi"], check=True)
def write_outside():
    with open(@OUTSIDE@, "w") as f:
        f.write("x")
def read_private():
    with open(@SECRET@) as f:
        f.read()
attempt("network", net)
attempt("subprocess", proc)
attempt("write_outside", write_outside)
attempt("read_private", read_private)
try:
    os.makedirs("home-agent-sandbox-check/out", exist_ok=True)
    with open("home-agent-sandbox-check/out/ok.txt", "w") as f:
        f.write("ok")
    print("write_inside:ok")
except Exception as exc:
    print("write_inside:failed", exc)
"""
