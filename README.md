# sdp-mcp

<a href="https://m8ven.ai/mcp/teejer/sdp-mcp" rel="noopener"><img src="https://m8ven.ai/badge/mcp/teejer/sdp-mcp" alt="M8ven Score" height="20"></a>

A **local** MCP (Model Context Protocol) server for ManageEngine ServiceDesk
Plus **Cloud (On-Demand)** — e.g. `https://help.example.com` — using
the v3 API with **Zoho OAuth 2.0** (a Self Client). Also supports a plain
`authtoken` for on-prem/legacy sites.

Runs over stdio on your machine, either directly with `python` or in a Docker
container. Full CRUD on tickets ("requests"): list, search, get, create,
update, assign, pickup, close, note, trash/restore — plus lookup tools for
technicians, groups, priorities, statuses, templates, request types and
requesters.

> **New here?** Follow the step-by-step [HOWTO guide](HOWTO.md) — including how
> to create the API credentials at <https://api-console.zoho.com/>.

## 1. Get credentials (Zoho OAuth Self Client)

Your site is SDP Cloud On-Demand, so the v3 API authenticates with **Zoho
OAuth 2.0**, not an SDP-page API key.

1. In the **Zoho API Console** (`https://console.zoho.com`), create a client of
   type **Self Client**.
2. Set scopes (comma-separated):
   ```
   SDPOnDemand.requests.ALL,SDPOnDemand.setup.READ,SDPOnDemand.users.READ
   ```
3. Copy the **Client ID** and **Client Secret** (from the *Client Secret* tab).
4. In the **Generate Code** tab, generate a one-time **authorization code**
   with those scopes (select your SDP portal if prompted). It expires in
   minutes.

## 2. Configure

```bash
cp .env.example .env   # then fill in CLIENT_ID / CLIENT_SECRET / ACCOUNTS_SERVER
```

Set `SDP_ACCOUNTS_SERVER` to your data center: US `https://accounts.zoho.com`,
EU `https://accounts.zoho.eu`, IN `https://accounts.zoho.in`,
AU `https://accounts.zoho.au`, CN `https://accounts.zoho.cn`.

## 3. Bootstrap the refresh token (once)

Exchange the one-time authorization code for a permanent refresh token:

```bash
python sdp_mcp.py exchange <authorization-code>
```

Paste the printed `SDP_REFRESH_TOKEN=...` line into `.env`. The server refreshes
the access token automatically from then on — you never handle the code again.

## 4. Run

### Option A — directly with python (no container)

```bash
pip install -e .
sdp-mcp        # stdio server
```

### Option B — container

```bash
docker build -t sdp-mcp .
docker run -i --env-file .env sdp-mcp   # env-file keeps secrets out of the command line
```

> Note: `-i` (interactive) is required — the MCP stdio transport needs stdin.
> Keep your secrets in a separate file (e.g. `.env`) that is not world
> readable; nothing is baked into the image.

## 5. Register with OpenCode

Add to `~/.config/opencode/opencode.json` (or the project's
`opencode.json`). With the container, the `.env` already carries the
credentials, so the command is all you need:

```json
{
  "mcp": {
    "servicedesk-plus": {
      "type": "local",
      "command": [
        "docker", "run", "-i", "--rm",
        "--env-file", "/path/to/sdp-mcp/.env",
        "sdp-mcp"
      ],
      "enabled": true
    }
  }
}
```

Or, running directly with python and passing env inline:

```json
{
  "mcp": {
    "servicedesk-plus": {
      "type": "local",
      "command": ["sdp-mcp"],
      "environment": {
        "SDP_BASE_URL": "https://help.example.com",
        "SDP_CLIENT_ID": "1000.XXX",
        "SDP_CLIENT_SECRET": "xxx",
        "SDP_REFRESH_TOKEN": "xxx",
        "SDP_ACCOUNTS_SERVER": "https://accounts.zoho.com"
      },
      "enabled": true
    }
  }
}
```

## Tools provided

| Tool | Purpose |
| --- | --- |
| `list_requests` | List tickets via an SDP filter (see `list_request_filters`) |
| `search_requests` | Search by field/condition (subject contains, status.name eq, …) |
| `get_request` | Full ticket detail, optionally with notes |
| `get_request_conversation` | Comment thread of a ticket |
| `list_request_filters` | Available saved list filters |
| `create_request` | Create a ticket (subject required; rest optional) |
| `update_request` | Patch arbitrary request fields via JSON |
| `assign_request` / `pickup_request` | Assign to tech/group / take ownership |
| `resolve_request` | Set a resolution (required before close); `close=true` closes in one step |
| `close_request` | Close with closure code + comments |
| `add_note` / `delete_note` | Add / remove a note (private by default) |
| `add_worklog` / `list_worklogs` / `delete_worklog` | Log, list, delete work time |
| `delete_request` / `restore_request` | Trash / permanently delete / restore |
| `list_technicians`, `list_groups`, `list_priorities`, `list_statuses`, `list_closure_codes`, `list_request_templates`, `list_cii_types`, `list_requesters` | Name/ID lookups |

Every tool declares the four MCP behavior annotations
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so MCP
hosts can warn before invoking destructive operations.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests replace the SDP HTTP layer with an in-memory fake, so they run
offline with no credentials and exercise every declared tool plus its argument
handling.

## License

Free for **personal and non-commercial** use (nonprofits, education, research,
government) under the [PolyForm Noncommercial License 1.0.0](LICENSE).
Commercial use requires a separate license from the author — open an issue to
ask.

> Required Notice: Copyright (c) 2026 Teejer

## Notes & caveats

- Built and tested against **MCP Python SDK v2** (`MCPServer`, stdio,
  `httpx2` for outbound calls). If you ever pin `mcp<1.x`-style v1 versions,
  the import path changes back to `mcp.server.fastmcp`.
- The API acts with the **scopes** granted to your OAuth client *and* the
  permissions of the Zoho user who authorized the code — both apply.
- Some SDP business rules (mandatory template fields, status-transition
  rules) can reject writes even when syntax is correct — the raw SDP error
  is returned so the model can correct itself.
- The image never contains credentials; they enter via environment at
  runtime. The refresh token is stored only in `.env` (or your MCP config).
