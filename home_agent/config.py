"""Application configuration.

Everything is read from environment variables, normally via the `.env` file in the
repository root (override the path with HOME_AGENT_ENV_FILE). Secrets stay in that
file and in process memory; they are never written to the database, logs, or prompts.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

Effort = Literal["low", "medium", "high", "xhigh", "max"]
ProviderName = Literal["anthropic", "ollama", "openai_compatible"]
SearchProviderName = Literal["brave", "tavily", "searxng", "none"]


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HomeAgent"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "home-agent"


def env_file_path() -> Path:
    return Path(os.environ.get("HOME_AGENT_ENV_FILE", REPO_ROOT / ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True, case_sensitive=False)

    # --- server -----------------------------------------------------------------
    host: str = Field("127.0.0.1", validation_alias=AliasChoices("HOME_AGENT_HOST", "host"))
    port: int = Field(18789, validation_alias=AliasChoices("HOME_AGENT_PORT", "port"))
    data_dir: Path = Field(
        default_factory=default_data_dir, validation_alias=AliasChoices("HOME_AGENT_DATA_DIR", "data_dir")
    )
    secret_key: SecretStr | None = Field(None, validation_alias=AliasChoices("HOME_AGENT_SECRET_KEY", "secret_key"))
    timezone: str = Field("Asia/Shanghai", validation_alias=AliasChoices("HOME_AGENT_TIMEZONE", "timezone"))
    public_url: str | None = Field(None, validation_alias=AliasChoices("HOME_AGENT_PUBLIC_URL", "public_url"))
    cookie_secure: Literal["auto", "always", "never"] = Field(
        "auto", validation_alias=AliasChoices("HOME_AGENT_COOKIE_SECURE", "cookie_secure")
    )
    session_days: int = Field(30, validation_alias=AliasChoices("HOME_AGENT_SESSION_DAYS", "session_days"))
    allowed_hosts: str = Field("", validation_alias=AliasChoices("HOME_AGENT_ALLOWED_HOSTS", "allowed_hosts"))
    log_level: str = Field("INFO", validation_alias=AliasChoices("HOME_AGENT_LOG_LEVEL", "log_level"))
    prevent_sleep: bool = Field(True, validation_alias=AliasChoices("HOME_AGENT_PREVENT_SLEEP", "prevent_sleep"))

    # --- model providers ----------------------------------------------------------
    model_provider: ProviderName = Field("anthropic", validation_alias=AliasChoices("MODEL_PROVIDER", "model_provider"))
    local_only: bool = Field(False, validation_alias=AliasChoices("LOCAL_ONLY", "local_only"))
    model_request_timeout: float = Field(
        600.0, validation_alias=AliasChoices("MODEL_REQUEST_TIMEOUT_SECONDS", "model_request_timeout")
    )
    model_input_price: float | None = Field(
        None, validation_alias=AliasChoices("MODEL_INPUT_PRICE_PER_MTOK", "model_input_price")
    )
    model_output_price: float | None = Field(
        None, validation_alias=AliasChoices("MODEL_OUTPUT_PRICE_PER_MTOK", "model_output_price")
    )

    anthropic_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key")
    )
    anthropic_model: str = Field("claude-opus-5-5", validation_alias=AliasChoices("ANTHROPIC_MODEL", "anthropic_model"))
    anthropic_effort: Effort = Field("medium", validation_alias=AliasChoices("ANTHROPIC_EFFORT", "anthropic_effort"))
    anthropic_max_tokens: int = Field(
        32000, validation_alias=AliasChoices("ANTHROPIC_MAX_TOKENS", "anthropic_max_tokens")
    )
    anthropic_refusal_fallback: bool = Field(
        True, validation_alias=AliasChoices("ANTHROPIC_REFUSAL_FALLBACK", "anthropic_refusal_fallback")
    )

    ollama_base_url: str = Field(
        "http://127.0.0.1:11434", validation_alias=AliasChoices("OLLAMA_BASE_URL", "ollama_base_url")
    )
    ollama_model: str = Field("qwen3:8b", validation_alias=AliasChoices("OLLAMA_MODEL", "ollama_model"))
    ollama_num_ctx: int = Field(32768, validation_alias=AliasChoices("OLLAMA_NUM_CTX", "ollama_num_ctx"))
    ollama_think: Literal["default", "on", "off"] = Field(
        "default", validation_alias=AliasChoices("OLLAMA_THINK", "ollama_think")
    )
    ollama_keep_alive: str = Field("30m", validation_alias=AliasChoices("OLLAMA_KEEP_ALIVE", "ollama_keep_alive"))

    openai_base_url: str | None = Field(
        None, validation_alias=AliasChoices("OPENAI_COMPAT_BASE_URL", "openai_base_url")
    )
    openai_api_key: SecretStr | None = Field(
        None, validation_alias=AliasChoices("OPENAI_COMPAT_API_KEY", "openai_api_key")
    )
    openai_model: str | None = Field(None, validation_alias=AliasChoices("OPENAI_COMPAT_MODEL", "openai_model"))
    openai_max_tokens: int = Field(
        8192, validation_alias=AliasChoices("OPENAI_COMPAT_MAX_TOKENS", "openai_max_tokens")
    )

    # --- web research ---------------------------------------------------------------
    search_provider: SearchProviderName = Field(
        "brave", validation_alias=AliasChoices("SEARCH_PROVIDER", "search_provider")
    )
    brave_api_key: SecretStr | None = Field(None, validation_alias=AliasChoices("BRAVE_API_KEY", "brave_api_key"))
    tavily_api_key: SecretStr | None = Field(None, validation_alias=AliasChoices("TAVILY_API_KEY", "tavily_api_key"))
    searxng_base_url: str | None = Field(
        None, validation_alias=AliasChoices("SEARXNG_BASE_URL", "searxng_base_url")
    )
    outbound_proxy: str | None = Field(None, validation_alias=AliasChoices("OUTBOUND_PROXY", "outbound_proxy"))
    fetch_allowed_extra_cidrs: str = Field(
        "", validation_alias=AliasChoices("FETCH_ALLOWED_EXTRA_CIDRS", "fetch_allowed_extra_cidrs")
    )
    fetch_timeout: float = Field(20.0, validation_alias=AliasChoices("FETCH_TIMEOUT_SECONDS", "fetch_timeout"))
    fetch_max_bytes: int = Field(8_000_000, validation_alias=AliasChoices("FETCH_MAX_BYTES", "fetch_max_bytes"))
    browser_enabled: bool = Field(True, validation_alias=AliasChoices("BROWSER_ENABLED", "browser_enabled"))
    browser_timeout: float = Field(
        30.0, validation_alias=AliasChoices("BROWSER_TIMEOUT_SECONDS", "browser_timeout")
    )
    browser_executable_path: str | None = Field(
        None, validation_alias=AliasChoices("BROWSER_EXECUTABLE_PATH", "browser_executable_path")
    )
    browser_channel: str | None = Field(None, validation_alias=AliasChoices("BROWSER_CHANNEL", "browser_channel"))

    # --- limits -----------------------------------------------------------------------
    max_concurrent_tasks: int = Field(2, validation_alias=AliasChoices("MAX_CONCURRENT_TASKS", "max_concurrent_tasks"))
    task_max_steps: int = Field(30, validation_alias=AliasChoices("TASK_MAX_STEPS", "task_max_steps"))
    task_max_tool_calls: int = Field(60, validation_alias=AliasChoices("TASK_MAX_TOOL_CALLS", "task_max_tool_calls"))
    task_max_searches: int = Field(15, validation_alias=AliasChoices("TASK_MAX_SEARCHES", "task_max_searches"))
    task_max_fetches: int = Field(30, validation_alias=AliasChoices("TASK_MAX_FETCHES", "task_max_fetches"))
    task_max_seconds: int = Field(1500, validation_alias=AliasChoices("TASK_MAX_SECONDS", "task_max_seconds"))
    task_max_tokens: int = Field(3_000_000, validation_alias=AliasChoices("TASK_MAX_TOKENS", "task_max_tokens"))
    task_max_cost_usd: float = Field(1.5, validation_alias=AliasChoices("TASK_MAX_COST_USD", "task_max_cost_usd"))
    daily_max_cost_usd: float = Field(6.0, validation_alias=AliasChoices("DAILY_MAX_COST_USD", "daily_max_cost_usd"))

    # --- Gmail (optional) -----------------------------------------------------------------
    gmail_client_id: str | None = Field(None, validation_alias=AliasChoices("GMAIL_CLIENT_ID", "gmail_client_id"))
    gmail_client_secret: SecretStr | None = Field(
        None, validation_alias=AliasChoices("GMAIL_CLIENT_SECRET", "gmail_client_secret")
    )
    gmail_redirect_uri: str | None = Field(
        None, validation_alias=AliasChoices("GMAIL_REDIRECT_URI", "gmail_redirect_uri")
    )
    gmail_allow_send: bool = Field(False, validation_alias=AliasChoices("GMAIL_ALLOW_SEND", "gmail_allow_send"))

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return default_data_dir()
        if isinstance(v, str):
            return Path(os.path.expandvars(v.strip())).expanduser()
        return v

    @field_validator(
        "public_url",
        "openai_base_url",
        "searxng_base_url",
        "outbound_proxy",
        "gmail_client_id",
        "gmail_redirect_uri",
        "openai_model",
        "browser_executable_path",
        "browser_channel",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "secret_key",
        "anthropic_api_key",
        "openai_api_key",
        "brave_api_key",
        "tavily_api_key",
        "gmail_client_secret",
        "model_input_price",
        "model_output_price",
        mode="before",
    )
    @classmethod
    def _blank_secret_to_none(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # --- derived paths -------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "home_agent.sqlite3"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspace"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "server.lock"

    @property
    def effective_gmail_redirect_uri(self) -> str:
        return self.gmail_redirect_uri or f"http://127.0.0.1:{self.port}/api/integrations/gmail/callback"

    @property
    def extra_allowed_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        nets = []
        for part in self.fetch_allowed_extra_cidrs.replace(";", ",").split(","):
            part = part.strip()
            if part:
                nets.append(ipaddress.ip_network(part, strict=False))
        return nets

    def secret_values(self) -> list[str]:
        """All configured secret strings, used to scrub logs and error messages."""
        out = []
        for value in (
            self.secret_key,
            self.anthropic_api_key,
            self.openai_api_key,
            self.brave_api_key,
            self.tavily_api_key,
            self.gmail_client_secret,
        ):
            if value is not None:
                raw = value.get_secret_value()
                if len(raw) >= 6:
                    out.append(raw)
        return out


def is_local_endpoint(url: str) -> bool:
    """True when a model endpoint is on this machine, the LAN, or the tailnet.

    Used to make sure "local" model mode never talks to a hosted service.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    if host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")  # Tailscale / CGNAT


def load_settings(env_file: Path | None = None, **overrides) -> Settings:
    path = env_file if env_file is not None else env_file_path()
    kwargs = {}
    if path and Path(path).exists():
        kwargs["_env_file"] = str(path)
        kwargs["_env_file_encoding"] = "utf-8"
    return Settings(**kwargs, **overrides)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
