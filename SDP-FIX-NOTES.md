# ServiceDesk Plus MCP — field-tested fix notes

Collected 2026-09-22 from a live session that used this MCP server to triage and close tickets
(two tickets closed successfully — one SSMS, one AD password audit). Everything below was
observed against the live cloud site, not guessed.

## Confirmed bugs / broken endpoints

1. **`add_worklog` (was broken, signature since changed)** — old signature used `owner` and failed
   with an opaque `Error executing tool`. New signature (`description` required, `owner_id`,
   `start_time_ms`/`end_time_ms`) works. Worklog created successfully:
   `owner_id: "<technician-id>"` (the site's default technician), epoch-ms start/end times, `time_spent: "1:00"`.
   ✅ Resolved — keep the new shape.

2. **`close_request` rejects cleanly but fails opaquely when the site requires a resolution.**
   - With `closure_code: "Success"` → hard error (opaque `Error executing tool`). This site has
     **no closure codes configured** (closed tickets show `closure_info.closure_code: null`).
   - Without `closure_code` → returns `status_code: 3000` with a warning
     `{type: "warning", fields: ["resolution"]}` and the ticket stays **Open**.
   - **Working close procedure:** first `update_request` with
     `fields_json: {"resolution": {"content": "..."}}` (returns 2000 success), then
     `close_request` with only `request_id` + `closure_comments` → succeeds
     (`"Request(s) closed"`).
   - **Suggested fixes:**
     a. Surface real API warnings/errors instead of a generic tool exception (this was the single
        biggest blocker — the agent had to bisect parameters to discover the resolution warning).
     b. Make `close_request` auto-populate `resolution.content` from `closure_comments` when the
        site requires it, or add a `resolve_request` tool.
     c. Drop `closure_code` default `"Success"` — it's invalid on this site.

3. **`update_request` with `fields_json: {"status": "Closed"}` (plain string) errors.**
   Object-shaped fields work (`{"resolution": {...}}`). Either document accepted shapes per field
   or normalize strings to `{name: ...}`.

4. **Endpoints that consistently error on this site** (opaque `Error executing tool`):
   - `list_statuses` — also note it *did* return once as literal `"[]"` (empty) even though
     statuses exist on tickets.
   - `list_technicians`
   - `get_request_conversation`
   These need real error surfacing too (likely 403/404 from the private-site API).

5. **`get_request` → `time_elapsed: "0"` is misleading.** It showed `"0"` on a ticket that had a
   1-hour worklog. Don't let agents infer worklog presence from it — point them to
   `list_worklogs` in the tool description (good — that tool now exists and works).

## Data-quality quirks (document in server instructions)

- **`My_Open_Requests` / `My_Pending_Requests` filters are unreliable:** they return **Closed**
  tickets too, and both filters returned identical results. Agents must client-side filter
  `requests[].status !== "Closed"`. Filters also don't scope to "my" tickets the way you'd expect
  (returned the whole IT group's queue).
- Tool descriptions should say: *"Filters are built-in SDP filters and may include Closed
  requests; always filter by status client-side."*

## Server `instructions` field — suggested text

Add this to the MCP server instructions so every session sees it without setup:

> ManageEngine ServiceDesk Plus (Cloud Private Site). Tickets are 'requests'.
> - Before closing: set a resolution first via `update_request` with
>   `fields_json: {"resolution": {"content": "..."}}`, then `close_request` WITHOUT
>   `closure_code` (this site has no closure codes).
> - `list_requests` filters are unreliable (may include Closed requests) — client-filter by
>   `status`.
> - Use `list_worklogs` to check logged time; `time_elapsed` on requests is unreliable.
> - `list_statuses`, `list_technicians`, `get_request_conversation` are not available on this
>   site.

## Nice-to-haves

- `resolve_request` convenience tool (sets resolution + optionally closes).
- A `list_closure_codes` (or confirm none exist and remove the parameter).
- When an SDP call fails, return the raw `response_status` block — the agent can do a lot with
  `{status_code: 3000, messages: [...]}` and nothing at all with `Error executing tool`.

## Resolution notes (fixed 2026-09-22)

All items above are addressed. Corrections to a few findings from the triage session:

- **Closure codes DO exist on this site** — 7 active request codes: Success, Cancelled, Failed,
  Moved, Postponed, Rejected, Unable to Reproduce (`GET /closure_codes`, exposed as
  `list_closure_codes`). Closed tickets show `closure_code: null` because the codes are optional,
  not absent. `close_request` still defaults to no code.
- **There is no "In Progress" status.** Site statuses: Canceled, Closed, On Hold, Open, Resolved.
  `update_request` now resolves status strings/names to a status **id** automatically — this site
  rejects a status PUT by `{"name": ...}` and accepts only `{"id": ...}`.
- **The close blocker has two parts:** a missing resolution (hard requirement) AND a
  "No work log found" warning. A close with a resolution but no worklog returns
  `{status_code: 3000, fields: ["No work log found"]}` and the ticket STAYS OPEN. Log a worklog
  (or accept the warning surfacing) before closing.
- `requester_ack_resolution` now defaults **false** — true sends the requester a
  resolution-confirmation email on every close.
- New recommended close path: `resolve_request(request_id, resolution, close=true)` after an
  optional `add_worklog`. `close_request` alone raises a clear error pointing there when the
  resolution is missing.
- Warnings are surfaced: list-style responses get a `_warnings` key; failed/non-JSON responses
  raise the parsed `response_status` instead of a generic exception.
- `list_statuses`/`list_technicians`/`get_request_conversation` are now working (row_count must be
  inside `input_data.list_info`; statuses response key is `statuses`).
- `get_request(get_notes=true)` fetches notes in a second call (the `get_notes` query param is
  rejected). `delete_request(permanent=true)` uses PUT `is_removed` (`delete_type` param rejected).
