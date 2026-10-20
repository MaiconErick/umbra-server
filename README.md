# umbra-server

The server side of **Umbra**, my self-hosted personal agent. A FastAPI service, a Telegram
bot and a scheduler that run on a Rock Pi 4B and expose a single HTTP/WebSocket hub to the
Android client (`xUmbra`).

## What it does

- **Multi-provider LLM core** — one `providers.py` abstraction over OpenAI-compatible
  endpoints, with role-based model selection (`model_roles.py`) and token accounting.
- **Tool system** — a registry of tools (`tools.py`, `toolset.py`) the model can call:
  web search, image generation, transcription, notes, scheduling and more.
- **Agents** — long-running or scheduled background services. Each one reports state to the
  hub, so the client can show it as a room with a working character.
- **Scheduler** — cron-style jobs (`cron.py`) with persistent state.
- **Memory** — note-taker and summarizer workers that compress conversations into
  long-term memory, plus a trait extractor for the assistant persona.
- **Chat hub** — sessions, message history, streaming over WebSocket, attachments.
- **Telegram bridge** — the same agent reachable from a Telegram chat.
- **Extra APIs** — image tools, sprite processing, stories, a small lab, remote device
  control and network discovery.

## Tech

Python 3 · FastAPI · Uvicorn · WebSockets · APScheduler · python-telegram-bot ·
OpenAI-compatible APIs · Pillow · Selenium

## Layout

```
umbra-server/
├── umbra.py            entry point
├── cli.py              command-line interface
├── config.ini          server configuration
├── agent/              LLM core, tools, cron, memory workers, personas, providers
├── api/                FastAPI routers: hub, assistant, brain, sprites, story, lab...
├── agents/             individual agent scripts
├── handlers/           message handlers
├── personas/           persona definitions
├── scripts/            maintenance and helper scripts
├── tests/              unit tests
└── data/               runtime data (git-ignored)
```

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # add your provider keys, Telegram token, chat id
python umbra.py
```

The service listens on `:8765` by default and is normally managed by systemd.

## Secrets

Nothing sensitive is committed: `.env` is git-ignored and all tokens are read from the
environment. `.env.example` documents every variable.

## License

MIT — see [LICENSE](LICENSE).
