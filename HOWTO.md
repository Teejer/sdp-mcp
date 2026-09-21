# How-To Guide

Step-by-step setup for connecting this MCP server to your ServiceDesk Plus
(Cloud / On-Demand) instance. No prior SDP or Zoho-API experience needed.

## Background: how authentication works

ServiceDesk Plus Cloud does **not** hand out API keys from inside the help
desk UI. Its REST API (v3) authenticates with **Zoho OAuth 2.0**:

1. You register an OAuth **Self Client** in the **Zoho API Console**
   (<https://api-console.zoho.com/>).
2. From that client you generate a short-lived **authorization code**.
3. The code is exchanged once for a **refresh token** (never expires).
4. This MCP server stores the refresh token and automatically mints
   short-lived access tokens from it as needed — you never touch codes or
   access tokens day-to-day.

The API itself lives at `https://<your-helpdesk-domain>/api/v3/...` and every
call carries the access token as an `Authorization: Zoho-oauthtoken <token>`
header.

Terminology: SDP = ServiceDesk Plus. A "request" = a ticket. "Technician" =
support agent; "Requester" = end user who files tickets.

## Step 1 — Create the OAuth client (one time)

1. Go to **<https://api-console.zoho.com/>** and sign in with the Zoho/SDP
   admin account for your organization.
2. Click **ADD CLIENT** → choose **Self Client**.
   - *Not* "Server-based", "Web", or "Mobile/Desktop" — those flows require a
     browser redirect this tool doesn't use.
3. Name it something recognizable, e.g. `sdp-mcp-local`.
4. Note the **Client ID** and **Client Secret** (shown on the *Client Secret*
   tab). Treat the secret like a password.

## Step 2 — Generate an authorization code (one time)

1. Open your new Self Client in the API console and click the
   **Generate Code** tab.
2. Enter the scopes you want to grant, comma-separated. For full ticket
   management:

   ```
   SDPOnDemand.requests.ALL,SDPOnDemand.setup.READ,SDPOnDemand.users.READ
   ```

   | Scope | Grants |
   | --- | --- |
   | `SDPOnDemand.requests.ALL` | read/create/update/delete tickets, notes, work logs |
   | `SDPOnDemand.setup.READ` | read priorities, statuses, templates, groups (lookup tools) |
   | `SDPOnDemand.users.READ` | read technicians and requesters (lookup tools) |

   Prefer read-only? Use `SDPOnDemand.requests.READ` instead of `.ALL`.
   **Scopes are frozen into the refresh token when the code is generated —
   they cannot be added later.**
3. Pick any expiry (the default ~3 minutes is fine) and click **CREATE**.
   Select your SDP portal if prompted.
4. **Copy the code immediately** — it is single-use and expires in minutes.

## Step 3 — Configure and bootstrap this server

```bash
git clone <this repo> && cd sdp-mcp
cp .env.example .env       # then edit .env
```

Fill in `.env`:

```env
SDP_BASE_URL=https://help.yourcompany.com      # the URL you log into daily
SDP_CLIENT_ID=1000.XXXX...
SDP_CLIENT_SECRET=xxxx...
SDP_ACCOUNTS_SERVER=https://accounts.zoho.com  # data center, see table below
```

Data center — `SDP_ACCOUNTS_SERVER` must match where your client/portal is
hosted:

| Data center | Accounts server |
| --- | --- |
| US | `https://accounts.zoho.com` |
| EU | `https://accounts.zoho.eu` |
| IN | `https://accounts.zoho.in` |
| AU | `https://accounts.zoho.au` |
| CN | `https://accounts.zoho.cn` |

Then exchange the code for a refresh token (do this right after Step 2):

```bash
pip install -e .
python sdp_mcp.py exchange <authorization-code>
```

Paste the printed `SDP_REFRESH_TOKEN=...` line into `.env`. That's the last
time you'll ever deal with codes.

## Step 4 — Verify

```bash
# 1. The server should list its tools over stdio:
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python sdp_mcp.py

# 2. (container option) build the image:
docker build -t sdp-mcp .
```

Or simply start OpenCode with the server registered (next step) and ask it to
list your open tickets.

## Step 5 — Register with your MCP client

Any MCP client works. For OpenCode (`~/.config/opencode/opencode.json`):

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

For Claude Desktop or other clients, use the equivalent "local/stdio server"
config with the same command (`docker run -i --rm --env-file ... sdp-mcp`, or
`.venv/bin/python sdp_mcp.py` without Docker).

## Daily use

Once registered, talk to your AI client naturally:

- "List my open tickets" / "Show overdue requests"
- "Create a ticket: printer down in accounting, requester Jane Doe"
- "Add a note to #1949: waiting on vendor"
- "Close ticket #1950 with closure code Success"

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `invalid_client` during exchange | Client ID/secret typo, or the code came from a *different* client than the ID/secret you passed. Codes are locked to the client that generated them. |
| `invalid_code` during exchange | Code already used, or older than a few minutes. Generate a fresh one and exchange immediately. |
| Refresh fails with `invalid_grant` at runtime | Refresh token revoked in the Zoho console, or client secret was rotated *and revoked* the token. Generate a new code and re-run the exchange. |
| `AUTHENTICATION_FAILED` / token rejected by SDP | Wrong `SDP_ACCOUNTS_SERVER` for your data center, or the token lacks scopes. |
| Reads work, writes fail with scope errors | The refresh token was generated from a code with `READ` scopes only. Make a new code with `.ALL` and re-exchange. |
| API returns an HTML login page | Wrong base URL. Use the exact domain you log into, without `/app/...` paths. |
| "Generate Code" tab missing | The client is not a **Self Client**. Create a Self Client. |

## Security notes

- `.env` holds live credentials — keep it private (`chmod 600`), never commit
  it (gitignored here).
- The refresh token is long-lived and as powerful as your SDP permissions —
  anyone holding it can read/write tickets. Revoke anytime in the Zoho API
  Console (client → *Settings* → manage tokens) or by regenerating the client
  secret.
- Scopes follow the principle of least privilege: start with `READ`, widen
  only when you need writes.
- The Docker image contains **no** credentials; they are injected at runtime
  via `--env-file`.
