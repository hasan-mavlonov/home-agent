# Home Agent

A self-hosted personal research agent for an Apple Silicon Mac. It runs as a background service on
`http://127.0.0.1:18789`, keeps working after you close the browser, and is reachable privately from
your phone or laptop through Tailscale.

It does real web research (search, then reading the actual pages, with JavaScript rendering when needed),
keeps a sortable tracker of opportunities with per-fact verification, drafts emails without sending
them, and runs recurring research only on schedules you set up or confirm.

## What is implemented

| Area | What you get |
|---|---|
| Agent | Tool-calling loop (Claude API, Ollama, or any OpenAI-compatible server) with validated tool inputs, retries, cancellation, and limits on steps, tool calls, searches, page reads, time, tokens and dollars (per task and per day). Progress shows real tool activity only; model reasoning is never displayed. |
| Durability | SQLite job queue. Every step is checkpointed and every tool call is recorded in a ledger, so after a crash or restart a task resumes where it was without repeating side effects. Closing the browser has no effect on running tasks. |
| Research | Search through Brave, Tavily or your own SearXNG. Pages and PDFs are fetched and read; JavaScript-heavy pages are rendered in headless Chromium. Blocked pages, logins and CAPTCHAs are reported honestly, never bypassed. |
| Honesty checks | A fact can only be marked *verified* if its quote is found on a page fetched in that task. Sources must be pages the agent actually consulted. Links in an answer that were never opened are flagged. Expired opportunities are marked. |
| Opportunities | Sortable table (cards on mobile): name, category, relevance, eligibility, deadline and time zone, location and format, cost, prize or funding, application link, sources with check dates, verification status. De-duplicated. You can bookmark, dismiss, add notes, and export to CSV. New finds are highlighted. |
| Email | Drafts live in the app (copy, or open in your mail app). Optional Gmail integration creates Gmail drafts (`gmail.compose` scope only). Optional sending requires you to approve the exact recipients, subject, body and attachments; the backend enforces it, any edit cancels the approval, and a send can't happen twice. The agent has no send tool. |
| Memory and schedules | Conversations, tasks, sources, opportunities, drafts and schedules are stored. Editable profile (starts with Shanghai, Uzbek citizen, AI undergraduate, MindForm, Defex Robotics) plus editable and deletable saved notes. Daily, weekly or monthly schedules in Asia/Shanghai by default; schedules the agent proposes stay inactive until you confirm them. In-app notifications. |
| Security | Password login, server-side sessions, CSRF tokens, strict CSP, host-header checks, login rate limiting. Credentials stay in `.env` (never in prompts, logs, or API responses); Gmail tokens are encrypted at rest. Agent web access cannot reach localhost, private networks, Tailscale addresses or cloud metadata endpoints, including through redirects and DNS tricks. File access is confined to a workspace folder. No shell access for the agent. |

## Install (macOS, Apple Silicon)

You need [Homebrew](https://brew.sh) and Git. Docker is not used.

```bash
git clone https://github.com/hasan-mavlonov/home-agent.git ~/home-agent
cd ~/home-agent
scripts/install-macos.sh
```

The installer:

- installs `uv` with Homebrew if it is missing;
- installs Python and the pinned dependencies from `uv.lock`;
- installs Chromium for Playwright (about 150 MB);
- creates `.env` with a random secret key (mode 600);
- asks you for the web password;
- prints a local-model recommendation for this Mac's memory;
- installs and starts the launchd service.

It does not touch Caddy, your other services, or system power settings.

Then add your keys to `.env` (see the next section) and run:

```bash
scripts/service.sh restart
open http://127.0.0.1:18789
```

## Credentials and setup you still need

| What | Needed for | Where |
|---|---|---|
| `ANTHROPIC_API_KEY` | The default cloud model (`claude-opus-5-5`) | [console.anthropic.com](https://console.anthropic.com). Usage-billed; **a Claude.ai or Claude Code subscription does not include API access.** |
| `BRAVE_API_KEY` **or** `TAVILY_API_KEY` **or** `SEARXNG_BASE_URL` | Web search | Brave: [brave.com/search/api](https://brave.com/search/api/) (monthly free credit, then about $5 per 1,000 searches). Tavily: [tavily.com](https://tavily.com) (free monthly credits). Or your own SearXNG instance with `formats: [html, json]`. |
| Ollama (optional) | Local model | `brew install ollama && brew services start ollama`, then `uv run home-agent recommend-model` |
| Gmail OAuth client (optional) | Gmail drafts and approved sending | See [Gmail](#gmail-optional) |
| Tailscale (optional) | Remote access | [tailscale.com/download/mac](https://tailscale.com/download/mac) |

Without a model key or a local model, tasks fail with a message saying exactly what is missing. Without a
search provider, the agent says search is unavailable. Neither case produces made-up results.

If this Mac is in mainland China: `api.anthropic.com`, Brave and Google may not be reachable without a VPN.
Anthropic offers its API only in [supported countries](https://www.anthropic.com/supported-countries), so
check that your use complies. Alternatives are a local model, or an OpenAI-compatible provider that serves
your region (set `MODEL_PROVIDER=openai_compatible` with its URL, key, model, and prices). If you use a VPN
client with a local HTTP proxy, set `OUTBOUND_PROXY=http://127.0.0.1:7890`. If the client uses fake-IP DNS
(for example Clash), also set `FETCH_ALLOWED_EXTRA_CIDRS=198.18.0.0/15`. Mirrors can speed up installation: `UV_INDEX_URL` for PyPI, `UV_PYTHON_INSTALL_MIRROR` for the Python
download, and `PLAYWRIGHT_DOWNLOAD_HOST` for Chromium.

## Everyday commands

```bash
scripts/service.sh status      # is it running?
scripts/service.sh restart     # after editing .env
scripts/service.sh logs        # follow the logs
scripts/service.sh stop
scripts/update.sh              # git pull + pinned deps + restart (migrations run automatically)
uv run home-agent doctor       # check configuration; add --live to contact the model and search
```

## Starting at login vs at boot

`scripts/service.sh install` creates a **LaunchAgent**
(`~/Library/LaunchAgents/com.homeagent.server.plist`):

- It starts when you log in and restarts automatically if it stops.
- It keeps running after you close Terminal or the browser.
- It is not running while nobody is logged in.

For a headless Mac mini, use `scripts/service.sh install --boot` instead. It installs a **LaunchDaemon**
(`/Library/LaunchDaemons/…`, needs sudo) that starts at boot before anyone logs in and runs as your user.

**FileVault caveat:** with FileVault on, after a reboot or power cut the disk stays locked until someone
enters the password at the Mac. Nothing starts until then, not even a LaunchDaemon. For a planned restart,
`sudo fdesetup authrestart` reboots without that prompt. If FileVault is off, the LaunchDaemon starts on
its own after a power cut.

## Keep the Mac awake (display may sleep)

While the service runs, it holds a `caffeinate -i -s` assertion. That prevents idle sleep and system sleep
on AC power but still lets the display sleep. To turn it off, set `HOME_AGENT_PREVENT_SLEEP=false`. For
belt and braces, also set the system power settings once:

```bash
sudo pmset -a sleep 0          # never put the system to sleep
sudo pmset -a displaysleep 10  # display sleeps after 10 minutes
sudo pmset -a disksleep 0
sudo pmset -a womp 1           # wake for network access
sudo pmset -a autorestart 1    # start up after a power failure
pmset -g                       # verify; `pmset -g assertions` shows Home Agent's caffeinate
```

The same settings are in System Settings → Energy: turn on "Prevent automatic sleeping when the display is
off", "Wake for network access" and "Start up automatically after a power failure". Choosing Apple menu →
Sleep still sleeps the Mac.

## Remote access with Tailscale

Home Agent listens only on `127.0.0.1`. Tailscale Serve publishes it with HTTPS to your own devices, not to
the internet:

```bash
scripts/tailscale-serve.sh        # serves https://<mac-name>.<tailnet>.ts.net:8443 -> 127.0.0.1:18789
```

- The script uses its own port (8443 by default) and refuses to replace an existing Serve handler, so your
  other Serve mappings and Caddy are left alone.
- The mapping persists across reboots.
- HTTPS certificates must be enabled for your tailnet; the CLI prompts you the first time.
- Add the printed URL to `.env` as `HOME_AGENT_PUBLIC_URL`, then restart the service.
- Open the URL on your phone (with Tailscale connected) and sign in with your Home Agent password. You can
  add it to the home screen.
- To remove the mapping: `scripts/tailscale-serve.sh --off`.

## Local models

```bash
uv run home-agent recommend-model
```

This reads the Mac's unified memory and suggests a tool-capable Ollama model and context size. For
example: 16 GB → `qwen3:8b`, 24 GB → `qwen3:14b`, 32 GB → `gpt-oss:20b`, 36 GB or more → `qwen3.8:27b`.
Ollama uses Metal on Apple Silicon; there is no CUDA. Then set the following in `.env` (or switch the
provider in Settings):

```
MODEL_PROVIDER=ollama
OLLAMA_MODEL=qwen3:8b
OLLAMA_NUM_CTX=32768
LOCAL_ONLY=true          # optional: refuse any non-local model endpoint
```

Local mode guarantees:

- Tasks are pinned to the provider they started with.
- A failure fails the task; it is never retried on a cloud model.
- With `LOCAL_ONLY=true`, cloud providers, non-private endpoints and Ollama cloud models (`*-cloud`,
  remote hosts) are refused before any prompt is sent.

LM Studio and `llama-server --jinja` work through `MODEL_PROVIDER=openai_compatible`.

### What leaves the machine

| Service | When | What is sent |
|---|---|---|
| Claude API (`api.anthropic.com`) | Only when the provider is `anthropic` | Prompts, profile, tool results (page text) |
| OpenAI-compatible endpoint | Only when configured | Same as above (stays local if the URL is local) |
| Brave / Tavily / SearXNG | Each web search | The search query only |
| Websites | Page reads | Normal HTTP requests from your Mac's IP |
| Google OAuth and Gmail API | Only if you connect Gmail | Draft contents, and sends you approved |
| Tailscale | Remote access | Your encrypted traffic to your own devices |

## Gmail (optional)

1. In [Google Cloud Console](https://console.cloud.google.com), create a project and enable the
   **Gmail API**.
2. Under Google Auth Platform:
   - **Branding**: set the app name and your email.
   - **Audience**: choose External and add yourself as a test user.
   - **Data access**: add the scope `https://www.googleapis.com/auth/gmail.compose`.
3. Under **Clients**, create a client of type **Web application**. Add the authorized redirect URI
   `http://127.0.0.1:18789/api/integrations/gmail/callback`. If you will connect from your phone, also add
   `https://<mac>.<tailnet>.ts.net:8443/api/integrations/gmail/callback` and set `GMAIL_REDIRECT_URI` to it.
4. Put `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` in `.env`, then restart. In Settings → Gmail, click
   Connect. If the browser ends on a `127.0.0.1` page that does not load (because you are not on the Mac),
   copy that whole address back into Settings.
5. Sending stays off unless you set `GMAIL_ALLOW_SEND=true`. Even then, each email needs your explicit
   approval of its exact content.

While the app is in "Testing", Google expires refresh tokens after 7 days and you would reconnect weekly. To
avoid this, publish the app ("In production"). Google then shows an "unverified app" warning for your own
app, which is expected for personal use.

## Using it

Try the chat examples:

- "Find AI and robotics hackathons I can enter in the next three months."
- "Research this company and summarize what it does: …"
- "Draft an email asking whether international students can participate."
- "Check these opportunities every week and show me newly discovered ones." The agent proposes a schedule;
  confirm it under Schedules.

Fill in the descriptions of MindForm and Defex Robotics under Profile. The agent uses them to judge fit.

## Tests

```bash
uv sync --frozen
uv run pytest                       # everything that needs no credentials
HOME_AGENT_LIVE=1 uv run pytest tests/live   # live tests; each skips itself if its credentials are missing
```

What the default tests use (labelled in each test file):

- **Mocked:** the model (scripted), search results, web pages and Gmail.
- **Real:** the HTTP server, auth and CSRF, the SQLite queue, worker, scheduler, approval logic, network
  guard, filtering proxy, and the Anthropic SDK request and response handling (against a mocked HTTP
  transport).

`tests/test_ui_browser.py` drives the actual web UI in headless Chromium.

The tests cover:

- authentication, CSRF and the host check;
- the email approval rules and duplicate-send prevention, including concurrent clicks, lost responses and a
  restart mid-send;
- cancellation, step and budget limits, and restart recovery without duplicated side effects;
- exactly-once scheduled runs and coalescing of missed runs;
- SSRF and redirect blocking, workspace confinement, and provider adapters.

The live tests read real pages, render with Chromium, and check redirect blocking against the internet. If
`ANTHROPIC_API_KEY` or a search key is set, they also run a real model call and real searches.

## Data, backups, removal

- Everything lives in `~/Library/Application Support/HomeAgent`: the SQLite database, logs, and the
  `workspace/` folder for agent and uploaded files. Back up that folder and `.env`.
- To stop the service and keep your data: `scripts/service.sh uninstall`.
- `uv run home-agent revoke-sessions` signs out every browser.
- `uv run home-agent set-password` changes the password and also signs everyone out.

## Known limitations

- Research quality depends on the model. Small local models are much weaker than Claude at multi-step
  research and may ignore instructions. Check anything you act on: deadlines and eligibility rules change.
- The quote check proves a quote appears on a fetched page. It does not prove the page is current or that
  the quote means what the model says.
- Pages behind logins, CAPTCHAs or heavy bot protection are reported as inaccessible. Some JavaScript sites
  still render incompletely.
- With `OUTBOUND_PROXY` set, the proxy resolves host names. The private-address check is then done before
  the request, not at connect time.
- Search APIs and Google may be unreachable from some networks without a VPN.
- One user per installation. Gmail is the only email integration. Sending is off by default.
- The launchd, Tailscale and pmset parts were written for macOS and checked with shellcheck, and the
  generated launchd plists parse as valid property lists. They could not be executed on a Mac from the
  build environment. The systemd unit is provided for Linux hosts.
