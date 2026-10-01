# Local PublikClip MCP server

The server exposes the clipping pipeline to MCP clients. It shares the desktop
app's job database and ChatGPT account store, and runs video processing in a
separate background worker. It does not require the desktop window to be open.

## Start on Windows

Clone this fork's `chatgpt-plus` branch and install
[uv](https://docs.astral.sh/uv/getting-started/installation/).
In PowerShell from the repository folder:

```powershell
git clone --branch chatgpt-plus https://github.com/Liam-1992/publikclip.git
cd publikclip
powershell -ExecutionPolicy Bypass -File .\scripts\Start-MCP.ps1
```

The launcher starts Streamable HTTP at `http://127.0.0.1:8917/mcp`.
Keep the terminal open. Ctrl+C stops the server and any worker it owns, preserving
checkpoints. `GET http://127.0.0.1:8917/health` returns a basic health check.

On macOS/Linux, or without the launcher:

```sh
cd pipeline
uv run --frozen --group mcp publikclip-mcp --transport streamable-http
```

Both methods install only the small MCP/authentication groups at server startup.
The first video job installs the larger pipeline dependencies and local model
weights. Internet access, sufficient disk space, and ffmpeg or the app's managed
ffmpeg download are needed for video processing.

## Connect a local MCP client

Use the HTTP endpoint above in clients that support local Streamable HTTP.
For clients that start a stdio server, add this entry to their MCP configuration,
replacing the example path with your checkout's absolute path:

```json
{
  "mcpServers": {
    "publikclip": {
      "command": "uv",
      "args": [
        "--directory", "D:\\Portfolio\\publikclip\\pipeline",
        "run", "--frozen", "--group", "mcp", "publikclip-mcp"
      ]
    }
  }
}
```

A stdio client starts and stops its own server: do not run the HTTP launcher at
the same time against the same data directory. Use `--home` or `PUBLIKCLIP_HOME`
to select a different data directory if needed. The default is `~/.publikclip`,
matching the app. A lock prevents two MCP servers from managing the same store.

Loopback addresses refer to the machine running the server. A cloud-hosted MCP
client cannot reach a server on your laptop through `127.0.0.1`. This server is
for local clients; it does not publish a tunnel or add itself to ChatGPT.

## ChatGPT setup

In the desktop app choose **Brain & keys → Continue with ChatGPT**, then pick a
model. Alternatively, from `pipeline/`:

```sh
uv run --frozen --group mcp publikclip chatgpt login
uv run --frozen --group mcp publikclip chatgpt models
uv run --frozen --group mcp publikclip chatgpt model <model-slug>
```

Sign in on the same machine/user and with the same `PUBLIKCLIP_HOME` as the MCP
server. Credentials remain in the existing backend store. MCP tools do not
return credentials, accept passwords, or perform sign-in. Availability and
usage limits depend on your account; live ChatGPT inference requires an eligible
signed-in account. Other scoring providers are explicitly selected per job.

## Tools

| Tool | Action |
| --- | --- |
| `server_status` | Read readiness, provider options, caption presets and ChatGPT connection status. |
| `list_jobs` | List desktop, CLI and MCP jobs; limit 1–100. |
| `start_job` | Start a local video or HTTPS YouTube URL; returns immediately with a job ID. |
| `job_status` | Read state, stage progress, saved options and errors. |
| `cancel_job` | Stop a worker owned by this server and its process tree; keep checkpoints. |
| `resume_job` | Continue a stopped/failed job with its saved provider and options. |
| `job_results` | Read scored candidates and rendered MP4 file paths, sizes and caption status. |
| `get_transcript` | Read paginated transcript segments and word timings. |

Results are also available as `publikclip://jobs/{job_id}/results` MCP resources.
Videos are returned as local file paths, not large base64 payloads.

Example tool sequence:

```text
server_status {}
start_job {"source":"D:\\Videos\\podcast.mp4","provider":"chatgpt","captions":"beast","camera":"cut"}
job_status {"job_id":"<returned-id>"}
job_results {"job_id":"<returned-id>"}
```

One MCP video worker runs at a time. Avoid running a desktop/CLI worker on the
same job concurrently. Resume refuses jobs still marked running; cancellation
affects only processes owned by the current MCP server. Server shutdown preserves
checkpoints. After a hard crash, check whether a worker or desktop job is still
running before attempting recovery.

Starting/resuming a job can download models and send transcripts/frames to the
selected AI provider. ChatGPT is the default and consumes eligible plan usage;
explicitly choosing Publik or Gemini can incur their charges. Image generation
is not included in the ChatGPT provider.

The HTTP server binds only to `127.0.0.1` and validates MCP Host/Origin headers.
It has no remote authentication and is not intended to be exposed publicly.
There is no arbitrary shell, arbitrary-file reader, or credential-management
tool. Remote source URLs are restricted to HTTPS YouTube hosts.

## Checks

```sh
uv run --frozen --group dev --group mcp pytest -q tests/test_mcp.py
```

Tests exercise real stdio/HTTP MCP initialization and tool/resource calls,
background worker output, cancellation/resume/shutdown, persisted checkpoints,
invalid paths, DNS rebinding protection, and a small real MP4 artifact when
ffmpeg is installed. Model-heavy end-to-end clipping and live ChatGPT inference
require a local setup and are not simulated as passed by these tests.

Protocol implementation: [official MCP Python SDK v1 documentation](https://py.sdk.modelcontextprotocol.io/v1/).
The SDK is bounded to its supported v1 maintenance line (`mcp>=1.30,<2`).
