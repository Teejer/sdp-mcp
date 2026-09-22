"""Local MCP server for ManageEngine ServiceDesk Plus Cloud (Private Site) API v3.

Runs over stdio. Configuration via environment variables (or a .env file):

    SDP_BASE_URL      e.g. https://help.example.com

Auth — either:
    SDP_AUTHTOKEN             a plain authtoken (legacy / on-prem style), OR
    Zoho OAuth (SDP Cloud On-Demand v3):
        SDP_CLIENT_ID
        SDP_CLIENT_SECRET
        SDP_REFRESH_TOKEN
        SDP_ACCOUNTS_SERVER   e.g. https://accounts.zoho.com (data-center specific)

To bootstrap SDP_REFRESH_TOKEN, run once:
    python sdp_mcp.py exchange <authorization-code>
using the one-time code from the client's "Generate Code" tab.

Full CRUD on requests (tickets): list/search, get, create, update, assign,
pickup, close, trash/restore, notes, and supporting lookups (technicians,
groups, priorities, statuses, templates, requesters).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

import httpx2
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

load_dotenv()

BASE_URL = os.getenv("SDP_BASE_URL", "").rstrip("/")
AUTHTOKEN = os.getenv("SDP_AUTHTOKEN", "")

# Zoho OAuth (SDP Cloud On-Demand v3)
CLIENT_ID = os.getenv("SDP_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("SDP_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("SDP_REFRESH_TOKEN", "")
ACCOUNTS_SERVER = os.getenv("SDP_ACCOUNTS_SERVER", "https://accounts.zoho.com").rstrip("/")

API_PREFIX = "/api/v3"
ACCEPT_HEADER = "application/vnd.manageengine.sdp.v3+json"

# Cached OAuth access token. Persisted to a small file next to .env so the
# token survives container restarts and we never call the refresh endpoint
# more than about once an hour. Rapid re-refresh of a Zoho refresh token is
# treated as token theft and revokes the whole token chain, so this cache is
# a safety feature, not just an optimization.
_TOKEN_CACHE_PATH = os.environ.get(
    "SDP_TOKEN_CACHE",
    # Home dir, not the module dir: the container runs as a non-root user that
    # can't write to /app. Override to share the cache across containers.
    os.path.join(os.path.expanduser("~"), ".sdp_token_cache.json"),
)
_MIN_REFRESH_INTERVAL = 3000  # hard floor: at most one refresh per 50 minutes
_oauth: dict[str, Any] = {"access_token": "", "expires_at": 0.0}


def _load_token_cache() -> None:
    try:
        with open(_TOKEN_CACHE_PATH) as fh:
            data = json.load(fh)
        if (
            data.get("refresh_token") == REFRESH_TOKEN
            and data.get("access_token")
            and float(data.get("expires_at", 0)) > time.time() + 60
        ):
            _oauth["access_token"] = data["access_token"]
            _oauth["expires_at"] = float(data["expires_at"])
    except (OSError, ValueError, TypeError):
        pass  # no cache / corrupt / different token — start fresh


def _save_token_cache() -> None:
    try:
        with open(_TOKEN_CACHE_PATH, "w") as fh:
            json.dump(
                {
                    "access_token": _oauth["access_token"],
                    "expires_at": _oauth["expires_at"],
                    "refresh_token": REFRESH_TOKEN,
                },
                fh,
            )
        os.chmod(_TOKEN_CACHE_PATH, 0o600)
    except OSError:
        pass  # read-only fs — in-memory cache still works for this process


def _oauth_access_token() -> str:
    """Return a valid Zoho OAuth access token. Uses the in-memory cache, then
    the on-disk cache, and only calls the refresh endpoint if both are
    exhausted — at most once per _MIN_REFRESH_INTERVAL."""
    if _oauth["access_token"] and time.time() < _oauth["expires_at"] - 60:
        return _oauth["access_token"]
    if not _oauth["access_token"]:
        _load_token_cache()
        if _oauth["access_token"] and time.time() < _oauth["expires_at"] - 60:
            return _oauth["access_token"]
    next_ok = _oauth.get("last_refresh_attempt", 0) + _MIN_REFRESH_INTERVAL
    if time.time() < next_ok:
        raise RuntimeError(
            "OAuth access token expired and a refresh was attempted less than "
            f"{_MIN_REFRESH_INTERVAL // 60} minutes ago. Refusing to refresh "
            "again: rapid refresh calls revoke the Zoho refresh token. "
            "Retry later or re-run: python sdp_mcp.py exchange <code>"
        )
    _oauth["last_refresh_attempt"] = time.time()
    resp = httpx2.post(
        f"{ACCOUNTS_SERVER}/oauth/v2/token",
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN,
        },
        timeout=30.0,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"OAuth refresh failed: {json.dumps(data)}")
    _oauth["access_token"] = data["access_token"]
    _oauth["expires_at"] = time.time() + float(data.get("expires_in", 3600))
    _save_token_cache()
    return _oauth["access_token"]


def _auth_header() -> dict[str, str]:
    """Build the auth header for the current config: OAuth if a refresh token
    is configured, else a plain authtoken."""
    if REFRESH_TOKEN:
        return {"Authorization": f"Zoho-oauthtoken {_oauth_access_token()}"}
    return {"authtoken": AUTHTOKEN}


def _exchange_code(code: str) -> None:
    """One-time: swap an authorization code for a refresh token and print the
    SDP_REFRESH_TOKEN line to paste into .env."""
    resp = httpx2.post(
        f"{ACCOUNTS_SERVER}/oauth/v2/token",
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
        },
        timeout=30.0,
    )
    data = resp.json()
    if "refresh_token" not in data:
        raise SystemExit(f"Exchange failed: {json.dumps(data, indent=2)}")
    print("Exchange succeeded. Add to .env:\n")
    print(f"SDP_REFRESH_TOKEN={data['refresh_token']}")
    print(f"SDP_ACCOUNTS_SERVER={ACCOUNTS_SERVER}")
    print(f"# api_domain reported: {data.get('api_domain')}")

API_PREFIX = "/api/v3"
ACCEPT_HEADER = "application/vnd.manageengine.sdp.v3+json"

mcp = MCPServer(
    "servicedesk-plus",
    instructions=(
        "Tools for ManageEngine ServiceDesk Plus (Cloud Private Site). "
        "Tickets are called 'requests'. Use lookup tools (list_technicians, "
        "list_groups, list_request_filters, etc.) to resolve names to IDs "
        "before creating or updating requests. "
        "Site quirks: a ticket requires a resolution before it can be closed — "
        "set one via update_request with fields_json "
        "{\"resolution\": {\"content\": \"...\"}} or use resolve_request; "
        "close_request auto-applies closure_comments as the resolution if the "
        "close is rejected for a missing one. "
        "list_requests filters are built-in SDP filters and may include "
        "Closed requests even when named 'Open' — always filter results by "
        "the status field client-side. "
        "Use list_worklogs to check logged time; the time_elapsed field on "
        "requests is unreliable."
    ),
)


def _require_config() -> None:
    has_oauth = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)
    if not BASE_URL or not (AUTHTOKEN or has_oauth):
        raise RuntimeError(
            "SDP_BASE_URL plus either SDP_AUTHTOKEN or the Zoho OAuth set "
            "(SDP_CLIENT_ID, SDP_CLIENT_SECRET, SDP_REFRESH_TOKEN) must be set "
            "in the environment or a .env file next to the server."
        )


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    input_data: Optional[dict[str, Any]] = None,
    form: bool = False,
) -> dict[str, Any]:
    """Call the SDP v3 API. GET list operations send input_data as a query
    parameter; mutations send it as a form field (form=True). The worklogs
    endpoint in particular mis-parses nested objects like `owner` when
    input_data arrives in the query string."""
    _require_config()
    url = f"{BASE_URL}{API_PREFIX}{path}"
    headers = {
        "Accept": ACCEPT_HEADER,
        **_auth_header(),
    }
    query = dict(params or {})
    body = None
    if input_data is not None:
        if form or method.upper() in ("POST", "PUT"):
            body = {"input_data": json.dumps(input_data)}
        else:
            query["input_data"] = json.dumps(input_data)
    elif method.upper() == "GET" and query.get("list_info"):
        # list_info is only valid *inside* the input_data wrapper
        query["input_data"] = json.dumps({"list_info": query.pop("list_info")})

    resp = httpx2.request(
        method,
        url,
        headers=headers,
        params=query if query else None,
        data=body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        body = resp.text[:2000]
        try:
            err = json.loads(body).get("response_status", {})
            if err:
                body = json.dumps(err)
        except ValueError:
            pass  # HTML error page (e.g. a 401 from the private-site gateway)
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(
            f"SDP returned non-JSON (HTTP {resp.status_code}): {resp.text[:300]!r}"
        )
    status = data.get("response_status")
    if isinstance(status, dict) and status.get("status") in ("error", "failed"):
        raise RuntimeError(json.dumps(status, indent=2))
    if isinstance(status, list):
        warnings = [m for m in status if m.get("type") == "warning"]
        if warnings:
            data["_warnings"] = warnings  # surface warnings to the caller
    return data


def _clean_request_summary(req: dict[str, Any]) -> dict[str, Any]:
    """Trim a request/list entry down to the useful fields."""

    def name_of(v: Any) -> Any:
        if isinstance(v, dict):
            return v.get("name")
        return v

    return {
        "id": req.get("id"),
        "subject": req.get("subject"),
        "status": name_of(req.get("status")),
        "priority": name_of(req.get("priority")),
        "request_type": name_of(req.get("request_type")),
        "technician": name_of(req.get("technician")),
        "group": name_of(req.get("group")),
        "requester": name_of(req.get("requester")),
        "created_time": (req.get("created_time") or {}).get("display_value"),
        "due_by_time": (req.get("due_by_time") or {}).get("display_value"),
        "is_overdue": req.get("is_overdue"),
    }


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_requests(
    filter_name: str = "All_Requests",
    row_count: int = 25,
    start_index: int = 1,
    sort_field: str = "created_time",
    sort_order: str = "desc",
) -> str:
    """List tickets using an SDP list filter (e.g. All_Requests,
    Open_Requests, My_Open_Requests). Use list_request_filters first to see
    available filter names. WARNING: filters on this site are unreliable —
    'Open'/'My_*' filters can include Closed requests and don't truly scope
    to the current user; filter the results by the status field."""
    list_info: dict[str, Any] = {
        "row_count": min(row_count, 100),
        "start_index": start_index,
        "sort_field": sort_field,
        "sort_order": sort_order,
        "filter_by": {"name": filter_name},
    }
    data = _request("GET", "/requests", input_data={"list_info": list_info})
    return json.dumps(
        {
            "requests": [_clean_request_summary(r) for r in data.get("requests", [])],
            "list_info": data.get("list_info", {}),
        },
        indent=2,
    )


@mcp.tool()
def search_requests(
    field: str = "subject",
    value: str = "",
    condition: str = "contains",
    filter_name: str = "All_Requests",
    row_count: int = 25,
) -> str:
    """Search tickets with a search_criteria clause. Common fields: subject,
    status.name, priority.name, technician.name, group.name, requester.name,
    category.name. Common conditions: contains, startwith, endwith, eq,
    noteq, before, after."""
    if not value:
        raise ValueError("value is required")
    list_info: dict[str, Any] = {
        "row_count": min(row_count, 100),
        "search_criteria": [{"field": field, "value": value, "condition": condition}],
        "filter_by": {"name": filter_name},
    }
    data = _request("GET", "/requests", input_data={"list_info": list_info})
    return json.dumps(
        {
            "requests": [_clean_request_summary(r) for r in data.get("requests", [])],
            "list_info": data.get("list_info", {}),
        },
        indent=2,
    )


@mcp.tool()
def get_request(request_id: str, get_notes: bool = False) -> str:
    """Get full details of one ticket by its ID. Set get_notes=true to also
    include its notes (fetched separately — use list_worklogs for logged
    time; the time_elapsed field on requests is unreliable)."""
    data = _request("GET", f"/requests/{request_id}")
    if get_notes:
        data["notes"] = _request("GET", f"/requests/{request_id}/notes").get("notes", [])
    return json.dumps(data, indent=2)


@mcp.tool()
def get_request_conversation(request_id: str, row_count: int = 50) -> str:
    """Get the conversation (comments between technician and requester) of a
    ticket."""
    data = _request(
        "GET",
        f"/requests/{request_id}/conversations",
        params={"list_info": {"row_count": min(row_count, 100)}},
    )
    return json.dumps(data, indent=2)


@mcp.tool()
def list_request_filters() -> str:
    """List the built-in list filters that list_requests accepts
    (filter_name values)."""
    filters = [
        {"filter_name": "All_Requests", "description": "Every request visible to you"},
        {"filter_name": "Open_Requests", "description": "All open requests"},
        {"filter_name": "Overdue_Requests", "description": "SLA-overdue requests"},
        {"filter_name": "Requests_Due_Today", "description": "Due today"},
        {"filter_name": "Unassigned_Requests", "description": "No technician assigned"},
        {"filter_name": "My_Open_Requests", "description": "My open requests"},
        {"filter_name": "My_Pending_Requests", "description": "My pending requests"},
        {"filter_name": "My_Overdue_Requests", "description": "My overdue requests"},
        {"filter_name": "My_Requests_Due_Today", "description": "Mine, due today"},
        {"filter_name": "My_Closed_Requests", "description": "Closed by me"},
        {"filter_name": "Requests_Pending_Approval", "description": "Awaiting approval"},
        {"filter_name": "Waiting_For_My_Update", "description": "Parked on me for input"},
        {"filter_name": "Updated_By_Me", "description": "Recently updated by me"},
    ]
    return json.dumps(
        {
            "filters": filters,
            "note": (
                "Your SDP site does not expose the /list_view_filters/show_all "
                "endpoint, so custom (user-saved) filters are not listed here. "
                "Try a custom filter name with list_requests anyway — "
                "underscore-separated display names usually work."
            ),
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool()
def create_request(
    subject: str,
    description: str = "",
    requester_email: str = "",
    requester_name: str = "",
    technician: str = "",
    group: str = "",
    priority: str = "",
    status: str = "",
    request_type: str = "",
    template: str = "",
    site: str = "",
    cc_emails: str = "",
) -> str:
    """Create a new ticket. Only subject is mandatory. Names (technician,
    group, priority, status, request_type, template, site) are matched by
    name; use the lookup tools to see valid values. Provide requester by
    email (preferred) or name."""
    request: dict[str, Any] = {"subject": subject}
    if description:
        request["description"] = description
    if requester_email:
        request["requester"] = {"email_id": requester_email}
    elif requester_name:
        request["requester"] = {"name": requester_name}
    if technician:
        request["technician"] = {"name": technician}
    if group:
        request["group"] = {"name": group}
    if priority:
        request["priority"] = {"name": priority}
    if status:
        request["status"] = {"name": status}
    if request_type:
        request["request_type"] = {"name": request_type}
    if template:
        request["template"] = {"name": template}
    if site:
        request["site"] = {"name": site}
    if cc_emails:
        request["email_ids_to_notify"] = cc_emails
    data = _request("POST", "/requests", input_data={"request": request}, form=True)
    return json.dumps(data, indent=2)


@mcp.tool()
def update_request(request_id: str, fields_json: str) -> str:
    """Update fields on a ticket. fields_json is a JSON object of request
    fields, e.g. {"subject": "...", "status": {"name": "Open"},
    "priority": {"name": "High"}, "technician": {"name": "John Doe"},
    "group": {"name": "Service Desk"}, "description": "...",
    "resolution": {"content": "..."}}.
    Raw udf fields can be included as "udf_fields": {...}.
    Bare strings for status/priority/technician/group/urgency are accepted
    and normalized to {"name": ...}."""
    fields = json.loads(fields_json)
    if not isinstance(fields, dict):
        raise ValueError("fields_json must be a JSON object")
    if isinstance(fields.get("status"), dict) and "name" in fields["status"] and "id" not in fields["status"]:
        fields["status"] = {"id": _status_id_by_name(fields["status"]["name"])}
    elif isinstance(fields.get("status"), str):
        fields["status"] = {"id": _status_id_by_name(fields["status"])}
    for key in ("priority", "technician", "group", "urgency", "site"):
        if isinstance(fields.get(key), str):
            fields[key] = {"name": fields[key]}
    data = _request("PUT", f"/requests/{request_id}", input_data={"request": fields}, form=True)
    return json.dumps(data, indent=2)


@mcp.tool()
def assign_request(request_id: str, technician: str, group: str = "") -> str:
    """Assign a ticket to a technician (optionally also to a group)."""
    input_data: dict[str, Any] = {"technician_name": technician}
    if group:
        input_data["group_name"] = group
    data = _request("PUT", f"/requests/{request_id}/assign", input_data=input_data, form=True)
    return json.dumps(data, indent=2)


@mcp.tool()
def pickup_request(request_id: str) -> str:
    """Pick up (take ownership of) a ticket."""
    data = _request("PUT", f"/requests/{request_id}/pickup")
    return json.dumps(data, indent=2)


def _set_resolution(request_id: str, content: str) -> dict[str, Any]:
    """Set the ticket's resolution field (required by this site before a
    ticket can be closed)."""
    return _request(
        "PUT",
        f"/requests/{request_id}",
        input_data={"request": {"resolution": {"content": content}}},
        form=True,
    )


@mcp.tool()
def resolve_request(
    request_id: str,
    resolution: str,
    close: bool = False,
    closure_comments: str = "",
    closure_code: str = "",
) -> str:
    """Set a resolution on a ticket — REQUIRED before closing on this site.
    With close=true this sets the resolution and closes in one step (the
    recommended way to close). Log work with add_worklog first when you
    can: without a work log SDP attaches a 'No work log found' warning to
    the close. closure_code optional; see list_closure_codes."""
    data = _set_resolution(request_id, resolution)
    if close:
        try:
            close_data = _close_call(
                request_id,
                closure_comments or resolution,
                closure_code,
                requester_ack_resolution=False,
                requester_ack_comments="",
            )
        except RuntimeError as exc:
            _raise_if_missing_resolution(exc)
            raise
        data = {"resolution_update": data.get("response_status", "ok"), "close": close_data}
    return json.dumps(data, indent=2)


def _close_call(
    request_id: str,
    closure_comments: str,
    closure_code: str,
    requester_ack_resolution: bool,
    requester_ack_comments: str,
) -> dict[str, Any]:
    closure_info: dict[str, Any] = {}
    if closure_code:
        closure_info["closure_code"] = {"name": closure_code}
    if closure_comments:
        closure_info["closure_comments"] = closure_comments
    if requester_ack_comments:
        closure_info["requester_ack_comments"] = requester_ack_comments
    if requester_ack_resolution:
        closure_info["requester_ack_resolution"] = True
    return _request(
        "PUT",
        f"/requests/{request_id}/close",
        input_data={"request": {"closure_info": closure_info}},
        form=True,
    )


def _raise_if_missing_resolution(exc: RuntimeError) -> None:
    """Re-raise unless the error is the missing-resolution warning; raise a
    clear instruction for that case."""
    if '"resolution"' not in str(exc):
        return
    raise RuntimeError(
        json.dumps(
            {
                "error": "SDP requires a resolution before this ticket can be "
                "closed, and no work log was found.",
                "fix": "Call resolve_request(request_id, resolution=<text>, "
                "close=true) — it sets the resolution and closes in one "
                "step. Ideally log work first with add_worklog; without a "
                "work log SDP attaches a 'No work log found' warning.",
            },
            indent=2,
        )
    )


@mcp.tool()
def close_request(
    request_id: str,
    closure_comments: str = "",
    closure_code: str = "",
    requester_ack_resolution: bool = False,
    requester_ack_comments: str = "",
) -> str:
    """Close a ticket. This site REQUIRES a resolution to already be set —
    if the close fails for a missing resolution this tool raises a clear
    error pointing at resolve_request. closure_code is optional (see
    list_closure_codes: Success, Cancelled, Failed, Moved, Postponed,
    Rejected, Unable to Reproduce); most tickets on this site close
    without one."""
    try:
        data = _close_call(
            request_id,
            closure_comments,
            closure_code,
            requester_ack_resolution,
            requester_ack_comments,
        )
    except RuntimeError as exc:
        _raise_if_missing_resolution(exc)
        raise
    return json.dumps(data, indent=2)


def _parse_time_spent(spec: str) -> tuple[str, str]:
    """Accept '1:30', '1.5', '90m', '1h30m', '0:15' -> (hours, minutes).
    Passed through as-is; your SDP instance records arbitrary durations
    (live worklogs include 0:01, 0:05, 0:10, 2:30...)."""
    spec = spec.strip().lower().replace("h", ":").replace("m", ":")
    parts = [p for p in spec.replace(".", ":").split(":") if p != ""]
    if len(parts) == 1:
        hours, minutes = parts[0], "0"
    elif len(parts) == 2:
        hours, minutes = parts[0], parts[1]
    else:
        raise ValueError(f"Cannot parse time_spent: {spec!r} (use 'H:MM' like '1:30')")
    return str(int(hours or 0)), str(int(minutes or 0))


def _now_ms() -> str:
    return str(int(time.time() * 1000))


_own_tech_id: dict[str, str] = {}
_status_ids: Optional[dict[str, str]] = None


def _status_id_by_name(name: str) -> str:
    """Map a status display name to its SDP id (cached). On this site a
    status PUT by {"name": ...} is rejected; only {"id": ...} is accepted,
    so we resolve names against /statuses first."""
    global _status_ids
    if _status_ids is None:
        data = _request("GET", "/statuses")
        _status_ids = {
            s.get("name", "").lower(): s.get("id", "") for s in data.get("statuses", [])
        }
    sid = _status_ids.get(name.lower())
    if not sid:
        raise ValueError(
            f"Unknown status {name!r}. Valid: {sorted(_status_ids)}"
        )
    return sid


def _own_technician_id() -> str:
    """Technician id this server acts as by default. Cached after first
    lookup; configure SDP_TECH_ID (see list_technicians) or pass owner_id
    per call."""
    if _own_tech_id.get("id"):
        return _own_tech_id["id"]
    env_id = os.getenv("SDP_TECH_ID")
    if env_id:
        _own_tech_id["id"] = env_id
        return env_id
    email = os.getenv("SDP_TECH_EMAIL")
    if email:
        try:
            data = _request("GET", "/technicians")
            for t in data.get("technicians", []):
                if t.get("email_id") == email:
                    _own_tech_id["id"] = t["id"]
                    return t["id"]
        except RuntimeError:
            pass
    raise RuntimeError(
        "Could not determine the default worklog owner. Set SDP_TECH_ID "
        "(or SDP_TECH_EMAIL) in the environment, or pass owner_id "
        "(see list_technicians)."
    )


@mcp.tool()
def add_note(
    request_id: str,
    description: str,
    notify_requester: bool = False,
    notify_technician: bool = False,
    mark_first_response: bool = False,
    add_to_linked_requests: bool = False,
) -> str:
    """Add a note to a ticket. Private by default; notify_requester=true makes
    it visible to (and emails) the requester."""
    note: dict[str, Any] = {
        "description": description,
        "show_to_requester": notify_requester,
        "notify_technician": notify_technician,
        "mark_first_response": mark_first_response,
        "add_to_linked_requests": add_to_linked_requests,
    }
    data = _request(
        "POST",
        f"/requests/{request_id}/notes",
        input_data={"request_note": note},
        form=True,
    )
    return json.dumps(data, indent=2)


@mcp.tool()
def delete_note(request_id: str, note_id: str) -> str:
    """Delete a note from a ticket."""
    data = _request("DELETE", f"/requests/{request_id}/notes/{note_id}")
    return json.dumps(data, indent=2)


_LARGE_WORKLOG_HOURS = 10


@mcp.tool()
def add_worklog(
    request_id: str,
    description: str,
    time_spent: str = "0:15",
    work_done: str = "",
    owner_id: str = "",
    start_time_ms: str = "",
    end_time_ms: str = "",
    confirm_large_hours: bool = False,
) -> str:
    """Log work on a ticket. time_spent like '1:30' (1 hr 30 min) or '30m'.
    work_done is the 'work performed' text appended to the description.
    owner_id is the technician's SDP id (defaults to the technician this
    server authenticates as — see list_technicians). start/end default to
    now. Note: this SDP instance rejects status/is_billable keys.
    Durations over 10 hours are refused unless confirm_large_hours=true —
    ask the user first, then retry with it set if they really meant it."""
    hours, minutes = _parse_time_spent(time_spent)
    total_minutes = int(hours) * 60 + int(minutes)
    if total_minutes > _LARGE_WORKLOG_HOURS * 60 and not confirm_large_hours:
        return json.dumps(
            {
                "confirmation_required": True,
                "message": (
                    f"That's {hours}h {minutes}m of work — over "
                    f"{_LARGE_WORKLOG_HOURS} hours. Show this to the user and "
                    "ask: 'You're logging "
                    f"{hours}h {minutes}m on ticket "
                    f"{request_id}. Confirm?' Only retry with "
                    "confirm_large_hours=true if they say yes."
                ),
                "request_id": request_id,
                "time_spent": f"{hours}:{minutes.zfill(2)}",
            },
            indent=2,
        )
    desc = description or work_done
    if work_done and description:
        desc = f"{description}\n\nWork done: {work_done}"
    worklog: dict[str, Any] = {
        "description": desc,
        "start_time": {"value": start_time_ms or _now_ms()},
        "end_time": {"value": end_time_ms or _now_ms()},
        "time_spent": {"hours": hours, "minutes": minutes},
        "owner": {"id": owner_id or _own_technician_id()},
    }
    data = _request(
        "POST",
        f"/requests/{request_id}/worklogs",
        input_data={"worklog": worklog},
        form=True,
    )
    return json.dumps(data, indent=2)


@mcp.tool()
def list_worklogs(request_id: str) -> str:
    """List work logs recorded on a ticket."""
    data = _request("GET", f"/requests/{request_id}/worklogs")
    return json.dumps(data, indent=2)


@mcp.tool()
def delete_worklog(request_id: str, worklog_id: str) -> str:
    """Delete a work log from a ticket (see list_worklogs for ids)."""
    data = _request("DELETE", f"/requests/{request_id}/worklogs/{worklog_id}")
    return json.dumps(data, indent=2)


@mcp.tool()
def delete_request(request_id: str, permanent: bool = False) -> str:
    """Move a ticket to trash (default), or delete it permanently
    (permanent=true)."""
    if permanent:
        data = _request(
            "PUT", f"/requests/{request_id}",
            input_data={"request": {"is_removed": True}}, form=True,
        )
    else:
        data = _request("DELETE", f"/requests/{request_id}")
    return json.dumps(data, indent=2)


@mcp.tool()
def restore_request(request_id: str) -> str:
    """Restore a trashed ticket back to the helpdesk."""
    data = _request("PUT", f"/requests/{request_id}/restore_from_trash")
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Lookup tools (for resolving names/IDs)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_technicians(level: str = "", row_count: int = 50) -> str:
    """List technicians. Optionally filter by level name."""
    params: dict[str, Any] = {"list_info": {"row_count": min(row_count, 200)}}
    if level:
        params["list_info"]["search_criteria"] = [
            {"field": "level.name", "value": level, "condition": "eq"}
        ]
    data = _request("GET", "/technicians", params=params)
    techs = [
        {
            "id": t.get("id"),
            "name": t.get("first_name", "") + " " + t.get("last_name", ""),
            "email_id": t.get("email_id"),
            "level": (t.get("level") or {}).get("name"),
        }
        for t in data.get("technicians", [])
    ]
    return json.dumps(techs, indent=2)


@mcp.tool()
def list_groups(row_count: int = 100) -> str:
    """List support groups."""
    data = _request("GET", "/groups", params={"list_info": {"row_count": min(row_count, 200)}})
    groups = [
        {"id": g.get("id"), "name": g.get("name"), "status": (g.get("status") or {}).get("name")}
        for g in data.get("groups", [])
    ]
    return json.dumps(groups, indent=2)


@mcp.tool()
def list_priorities() -> str:
    """List priority definitions."""
    data = _request("GET", "/priorities")
    priorities = [
        {"id": p.get("id"), "name": p.get("name")} for p in data.get("priorities", [])
    ]
    return json.dumps(priorities, indent=2)


@mcp.tool()
def list_closure_codes() -> str:
    """List closure codes configured for requests on this site."""
    data = _request("GET", "/closure_codes")
    codes = [
        {"id": c.get("id"), "name": c.get("name")}
        for c in data.get("closure_codes", [])
        if (c.get("module") or {}).get("name") == "request" and not c.get("inactive")
    ]
    return json.dumps(codes, indent=2)


@mcp.tool()
def list_statuses() -> str:
    """List request statuses."""
    data = _request("GET", "/statuses")
    statuses = [
        {"id": s.get("id"), "name": s.get("name")} for s in data.get("statuses", [])
    ]
    return json.dumps(statuses, indent=2)


@mcp.tool()
def list_request_templates(row_count: int = 100) -> str:
    """List request (incident/service request) templates."""
    data = _request(
        "GET",
        "/request_templates",
        params={"list_info": {"row_count": min(row_count, 200)}},
    )
    templates = [
        {
            "id": t.get("id"),
            "name": t.get("template_name"),
            "is_service_template": t.get("is_service_template"),
        }
        for t in data.get("request_templates", [])
    ]
    return json.dumps(templates, indent=2)


@mcp.tool()
def list_cii_types() -> str:
    """List request types (Incident, Service Request, Problem, Change...)."""
    data = _request("GET", "/requesttypes")
    types = [
        {"id": t.get("id"), "name": t.get("name")} for t in data.get("requesttypes", [])
    ]
    return json.dumps(types, indent=2)


@mcp.tool()
def list_requesters(search_text: str = "", row_count: int = 50) -> str:
    """List requesters. Pass search_text to search by name."""
    list_info: dict[str, Any] = {"row_count": min(row_count, 200)}
    if search_text:
        list_info["search_text"] = search_text
    data = _request("GET", "/requesters", input_data={"list_info": list_info})
    requesters = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "email_id": r.get("email_id"),
        }
        for r in data.get("requesters", [])
    ]
    return json.dumps(requesters, indent=2)


def main() -> None:
    # One-time bootstrap: `python sdp_mcp.py exchange <auth-code>`
    if len(sys.argv) >= 3 and sys.argv[1] == "exchange":
        _exchange_code(sys.argv[2])
        return
    try:
        _require_config()
    except RuntimeError as exc:
        print(f"WARNING: {exc} Tools will fail until configured.", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
