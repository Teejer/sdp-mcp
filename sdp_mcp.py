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

# Cached OAuth access token (module-level, refreshed near expiry)
_oauth: dict[str, Any] = {"access_token": "", "expires_at": 0.0}


def _oauth_access_token() -> str:
    """Return a valid Zoho OAuth access token, refreshing via the stored
    refresh token when missing or within 60s of expiry."""
    if _oauth["access_token"] and time.time() < _oauth["expires_at"] - 60:
        return _oauth["access_token"]
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
        "before creating or updating requests."
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
) -> dict[str, Any]:
    """Call the SDP v3 API. Mutations send input_data as a form field;
    GET list operations send it as a query parameter."""
    _require_config()
    url = f"{BASE_URL}{API_PREFIX}{path}"
    headers = {
        "Accept": ACCEPT_HEADER,
        **_auth_header(),
    }
    query = dict(params or {})
    if input_data is not None:
        query["input_data"] = json.dumps(input_data)

    resp = httpx2.request(
        method,
        url,
        headers=headers,
        params=query if query else None,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:2000]}")
    if not resp.content:
        return {}
    data = resp.json()
    status = data.get("response_status")
    if isinstance(status, dict) and status.get("status") == "error":
        raise RuntimeError(json.dumps(status, indent=2))
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
    available filter names."""
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
    include its notes."""
    params = {"get_notes": "true"} if get_notes else None
    data = _request("GET", f"/requests/{request_id}", params=params)
    return json.dumps(data, indent=2)


@mcp.tool()
def get_request_conversation(request_id: str, row_count: int = 50) -> str:
    """Get the conversation (comments between technician and requester) of a
    ticket."""
    data = _request(
        "GET",
        f"/requests/{request_id}/conversations",
        params={"row_count": min(row_count, 100)},
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
    data = _request("POST", "/requests", input_data={"request": request})
    return json.dumps(data, indent=2)


@mcp.tool()
def update_request(request_id: str, fields_json: str) -> str:
    """Update fields on a ticket. fields_json is a JSON object of request
    fields, e.g. {"subject": "...", "status": {"name": "Open"},
    "priority": {"name": "High"}, "technician": {"name": "John Doe"},
    "group": {"name": "Service Desk"}, "description": "..."}.
    Raw udf fields can be included as "udf_fields": {...}."""
    fields = json.loads(fields_json)
    if not isinstance(fields, dict):
        raise ValueError("fields_json must be a JSON object")
    data = _request("PUT", f"/requests/{request_id}", input_data={"request": fields})
    return json.dumps(data, indent=2)


@mcp.tool()
def assign_request(request_id: str, technician: str, group: str = "") -> str:
    """Assign a ticket to a technician (optionally also to a group)."""
    input_data: dict[str, Any] = {"technician_name": technician}
    if group:
        input_data["group_name"] = group
    data = _request("PUT", f"/requests/{request_id}/assign", input_data=input_data)
    return json.dumps(data, indent=2)


@mcp.tool()
def pickup_request(request_id: str) -> str:
    """Pick up (take ownership of) a ticket."""
    data = _request("PUT", f"/requests/{request_id}/pickup")
    return json.dumps(data, indent=2)


@mcp.tool()
def close_request(
    request_id: str,
    closure_comments: str = "",
    closure_code: str = "Success",
    requester_ack_resolution: bool = True,
    requester_ack_comments: str = "",
) -> str:
    """Close a ticket with optional closure comments and closure code
    (e.g. Success, Canceled, Task not defined)."""
    closure_info: dict[str, Any] = {
        "closure_code": {"name": closure_code},
        "requester_ack_resolution": requester_ack_resolution,
    }
    if closure_comments:
        closure_info["closure_comments"] = closure_comments
    if requester_ack_comments:
        closure_info["requester_ack_comments"] = requester_ack_comments
    data = _request(
        "PUT", f"/requests/{request_id}/close", input_data={"request": {"closure_info": closure_info}}
    )
    return json.dumps(data, indent=2)


@mcp.tool()
def add_note(
    request_id: str,
    description: str,
    notify_requester: bool = False,
    time_spent: str = "",
    work_done: str = "",
) -> str:
    """Add a note to a ticket. Optionally include a work log (time_spent like
    '1:30' hours, and work_done text)."""
    note: dict[str, Any] = {
        "parent": {"id": request_id},
        "description": description,
        "add_worklog": bool(time_spent or work_done),
    }
    if notify_requester:
        note["mail_details"] = {"notify": True}
    if time_spent:
        note["time_spent"] = {"value": time_spent}
    if work_done:
        note["work_done"] = work_done
    data = _request("POST", "/requests/notes", input_data={"note": note})
    return json.dumps(data, indent=2)


@mcp.tool()
def delete_request(request_id: str, permanent: bool = False) -> str:
    """Move a ticket to trash (default), or delete it permanently
    (permanent=true)."""
    data = _request("DELETE", f"/requests/{request_id}", params={"delete_type": "permanent"} if permanent else None)
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
    params: dict[str, Any] = {"row_count": min(row_count, 200)}
    if level:
        params["level_name"] = level
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
    data = _request("GET", "/groups", params={"row_count": min(row_count, 200)})
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
def list_statuses() -> str:
    """List request statuses."""
    data = _request("GET", "/statuses")
    statuses = [
        {"id": s.get("id"), "name": s.get("name")} for s in data.get("status", [])
    ]
    return json.dumps(statuses, indent=2)


@mcp.tool()
def list_request_templates(row_count: int = 100) -> str:
    """List request (incident/service request) templates."""
    data = _request(
        "GET", "/request_templates", params={"row_count": min(row_count, 200)}
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
