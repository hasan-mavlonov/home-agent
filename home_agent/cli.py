"""Command-line entry point: `uv run home-agent <command>`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import shutil
import socket
import sys
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, env_file_path, is_local_endpoint, load_settings


def cmd_init(args) -> int:
    env_path = env_file_path()
    example = REPO_ROOT / ".env.example"
    if not env_path.exists():
        if not example.exists():
            print(f"Missing {example}", file=sys.stderr)
            return 1
        shutil.copyfile(example, env_path)
        print(f"Created {env_path}")
    text = env_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed = False
    for index, line in enumerate(lines):
        if line.startswith("HOME_AGENT_SECRET_KEY=") and not line.split("=", 1)[1].strip():
            lines[index] = f"HOME_AGENT_SECRET_KEY={secrets.token_urlsafe(48)}"
            changed = True
    if not any(line.startswith("HOME_AGENT_SECRET_KEY=") for line in lines):
        lines.append(f"HOME_AGENT_SECRET_KEY={secrets.token_urlsafe(48)}")
        changed = True
    if changed:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Generated HOME_AGENT_SECRET_KEY")
    os.chmod(env_path, 0o600)
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.data_dir, 0o700)
    print(f"Data directory: {settings.data_dir}")
    print("Next: edit .env (API keys), then run: uv run home-agent set-password")
    return 0


def _open_db(settings):
    from .db import Database
    from .services.profile import ensure_profile

    db = Database(settings.db_path)
    db.migrate()
    ensure_profile(db)
    return db


def cmd_set_password(args) -> int:
    from .security.auth import MIN_PASSWORD_LENGTH, AuthService

    settings = load_settings()
    db = _open_db(settings)
    if args.stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass(f"New Home Agent password (min {MIN_PASSWORD_LENGTH} characters): ")
        if getpass.getpass("Repeat: ") != password:
            print("Passwords do not match", file=sys.stderr)
            return 1
    try:
        AuthService(db).set_password(password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Password set. All existing sessions were signed out.")
    return 0


def cmd_revoke_sessions(args) -> int:
    from .security.auth import AuthService

    count = AuthService(_open_db(load_settings())).revoke_all()
    print(f"Signed out {count} session(s).")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .logging_setup import configure_logging
    from .web.app import create_app

    settings = load_settings()
    if not settings.secret_key:
        print("HOME_AGENT_SECRET_KEY is not set. Run: uv run home-agent init", file=sys.stderr)
        return 1
    configure_logging(settings)
    caffeinate = _hold_wake_assertion(settings)
    app = create_app(settings)
    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level="warning",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1,::1",
            server_header=False,
        )
    finally:
        if caffeinate is not None:
            caffeinate.terminate()
    return 0


def _hold_wake_assertion(settings):
    """On macOS, keep the system (not the display) awake while the server runs.

    `caffeinate -i -s -w PID` prevents idle and system sleep (on AC power) until this
    process exits. The display can still sleep. Disable with HOME_AGENT_PREVENT_SLEEP=false.
    """
    import subprocess

    if sys.platform != "darwin" or not settings.prevent_sleep or not os.path.exists("/usr/bin/caffeinate"):
        return None
    try:
        return subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())])
    except OSError:
        return None


def _check(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "OK  ", False: "FAIL", None: "WARN"}[ok]
    print(f"[{mark}] {label}{': ' + detail if detail else ''}")


def cmd_doctor(args) -> int:
    from .llm import build_provider
    from .llm.base import ModelConfigError, ModelError

    settings = load_settings()
    print(f"Home Agent {__version__}\n.env: {env_file_path()} ({'found' if env_file_path().exists() else 'MISSING'})")
    _check(bool(settings.secret_key), "Secret key", "set" if settings.secret_key else "run: uv run home-agent init")
    env_path = env_file_path()
    if env_path.exists():
        mode = env_path.stat().st_mode & 0o777
        _check(mode & 0o077 == 0, ".env permissions", oct(mode) + ("" if mode & 0o077 == 0 else " - run: chmod 600 .env"))
    db = _open_db(settings)
    from .security.auth import AuthService

    _check(AuthService(db).password_is_set(), "Web password", "set" if AuthService(db).password_is_set() else "run: uv run home-agent set-password")
    _check(settings.host in ("127.0.0.1", "::1", "localhost"), "Listen address",
           f"{settings.host}:{settings.port}" + ("" if settings.host in ("127.0.0.1", "::1", "localhost")
                                                  else " (reachable from other machines; prefer 127.0.0.1 + Tailscale Serve)"))  # fmt: skip
    with socket.socket() as sock:
        in_use = sock.connect_ex(("127.0.0.1", settings.port)) == 0
    _check(None if in_use else True, f"Port {settings.port}", "in use (fine if Home Agent is already running)" if in_use else "free")

    provider_name = settings.model_provider
    try:
        provider = build_provider(settings, provider_name)
        _check(True, "Model provider", f"{provider_name} / {provider.model}" + (" (local)" if provider.is_local else " (cloud)"))
        if args.live:
            try:
                _check(True, "Model connectivity", asyncio.run(provider.check()))
            except (ModelError, ModelConfigError) as exc:
                _check(False, "Model connectivity", str(exc))
    except ModelConfigError as exc:
        _check(False, "Model provider", str(exc))
    if provider_name in ("ollama", "openai_compatible"):
        endpoint = settings.ollama_base_url if provider_name == "ollama" else (settings.openai_base_url or "")
        _check(is_local_endpoint(endpoint) or None, "Local model endpoint", endpoint or "not set")

    from .agent.sandbox import ScriptRunner
    from .services.workspace import Workspace

    scripts = ScriptRunner(settings, Workspace(settings.workspace_dir))
    if not scripts.available:
        _check(None, "Script sandbox", scripts.reason)
    else:
        _check(True, "Script sandbox", scripts.reason)
        for name, ok, detail in asyncio.run(scripts.self_test()):
            _check(ok, f"  {name}", detail)

    if settings.search_provider == "none":
        _check(None, "Web search", "disabled (SEARCH_PROVIDER=none)")
    else:
        key = {"brave": settings.brave_api_key, "tavily": settings.tavily_api_key, "searxng": settings.searxng_base_url}[settings.search_provider]
        _check(bool(key), "Web search", settings.search_provider + ("" if key else " - credentials/URL missing"))
        if key and args.live:
            from .search import build_search_provider

            async def probe():
                provider_obj, client = build_search_provider(settings)
                try:
                    return len(await provider_obj.search("Home Agent connectivity test", 2))
                finally:
                    if client:
                        await client.aclose()

            try:
                _check(True, "Search connectivity", f"{asyncio.run(probe())} results")
            except Exception as exc:
                _check(False, "Search connectivity", str(exc))

    if settings.browser_enabled:
        browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ("Library/Caches/ms-playwright" if sys.platform == "darwin" else ".cache/ms-playwright")))
        has_browser = settings.browser_executable_path or settings.browser_channel or any(browsers.glob("chromium*"))
        _check(bool(has_browser) or None, "Browser for JavaScript pages",
               ("installed" + ("" if args.live else " (--live test-launches it)")) if has_browser
               else "not installed - run: uv run playwright install chromium")  # fmt: skip
        if args.live and has_browser:
            from .net.browser import BrowserRenderer
            from .net.guard import NetGuard

            async def render():
                renderer = BrowserRenderer(NetGuard(settings.extra_allowed_networks), upstream_proxy=settings.outbound_proxy,
                                           executable_path=settings.browser_executable_path, channel=settings.browser_channel)  # fmt: skip
                try:
                    return await renderer.render("https://example.com/")
                finally:
                    await renderer.aclose()

            try:
                result = asyncio.run(render())
                _check(True, "Browser launch", f"rendered example.com ({result.title})")
            except Exception as exc:
                _check(False, "Browser launch", str(exc).splitlines()[0][:200])
    else:
        _check(None, "Browser for JavaScript pages", "disabled")
    if settings.gmail_client_id:
        _check(bool(settings.gmail_client_secret), "Gmail OAuth client", "configured; connect it in Settings")
        _check(None if settings.gmail_allow_send else True, "Gmail sending", "enabled (approval required per email)" if settings.gmail_allow_send else "disabled (drafts only)")
    else:
        _check(None, "Gmail", "not configured (optional)")
    if settings.outbound_proxy:
        _check(None, "Outbound proxy", "set - DNS-rebinding protection is limited to pre-checks when a proxy is used")
    if sys.platform == "darwin":
        import subprocess

        try:
            out = subprocess.run(["/usr/bin/pmset", "-g"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        sleep_line = next((line.strip() for line in out.splitlines() if line.strip().startswith("sleep")), "")
        never_sleeps = sleep_line.split()[1:2] == ["0"]
        _check(True if never_sleeps else None, "System sleep", (sleep_line or "unknown") + " (see README: Keep the Mac awake)")
    return 0


def cmd_recommend_model(args) -> int:
    from .hardware import report

    print(report())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="home-agent", description="Self-hosted personal research agent")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create .env and a secret key").set_defaults(func=cmd_init)
    p = sub.add_parser("set-password", help="set the web login password")
    p.add_argument("--stdin", action="store_true", help="read the password from standard input")
    p.set_defaults(func=cmd_set_password)
    sub.add_parser("serve", help="run the web app and background worker").set_defaults(func=cmd_serve)
    p = sub.add_parser("doctor", help="check configuration")
    p.add_argument("--live", action="store_true", help="also contact the model and search provider")
    p.set_defaults(func=cmd_doctor)
    sub.add_parser("recommend-model", help="suggest a local model for this machine").set_defaults(func=cmd_recommend_model)
    sub.add_parser("revoke-sessions", help="sign out every browser").set_defaults(func=cmd_revoke_sessions)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
