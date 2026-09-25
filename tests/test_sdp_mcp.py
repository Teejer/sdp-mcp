"""Unit tests for sdp_mcp.

The SDP HTTP layer (``_request``) is replaced with a recording fake, so these
tests exercise every declared tool's argument handling and endpoint usage
without any network access or credentials.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import sdp_mcp

EXPECTED_TOOLS = {
    "list_requests",
    "search_requests",
    "get_request",
    "get_request_conversation",
    "list_request_filters",
    "create_request",
    "update_request",
    "assign_request",
    "pickup_request",
    "resolve_request",
    "close_request",
    "add_note",
    "delete_note",
    "add_worklog",
    "list_worklogs",
    "delete_worklog",
    "delete_request",
    "restore_request",
    "list_technicians",
    "list_groups",
    "list_priorities",
    "list_closure_codes",
    "list_statuses",
    "list_request_templates",
    "list_cii_types",
    "list_requesters",
}


def _fake_api(monkeypatch, *, close_fails: bool = False):
    """Replace sdp_mcp._request with a recorder. Returns the call list."""
    calls: list[dict] = []

    def fake(method, path, *, params=None, input_data=None, form=False):
        calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "input_data": input_data,
                "form": form,
            }
        )
        if path == "/statuses":
            return {
                "statuses": [
                    {"id": "st-1", "name": "Open"},
                    {"id": "st-9", "name": "Closed"},
                ]
            }
        if close_fails and path.endswith("/close"):
            raise RuntimeError(
                'HTTP 400: {"response_status": {"status": "error", '
                '"message": {"resolution": "Resolution is required before closing"}}}'
            )
        return {
            "requests": [
                {
                    "id": "101",
                    "subject": "Printer on fire",
                    "status": {"name": "Open"},
                    "priority": {"name": "High"},
                    "technician": {"name": "Te Ch"},
                    "created_time": {"display_value": "today"},
                    "is_overdue": False,
                }
            ],
            "list_info": {"count": 1},
            "notes": [{"id": "n1", "description": "a note"}],
            "conversations": [{"id": "c1"}],
            "worklogs": [{"id": "w1"}],
            "technicians": [
                {
                    "id": "t1",
                    "first_name": "Te",
                    "last_name": "Ch",
                    "email_id": "te@ch.er",
                    "level": {"name": "Agent"},
                }
            ],
            "groups": [{"id": "g1", "name": "Service Desk", "status": {"name": "Active"}}],
            "priorities": [{"id": "p1", "name": "High"}],
            "closure_codes": [
                {"id": "cc1", "name": "Success", "module": {"name": "request"}},
                {"id": "cc2", "name": "Old", "module": {"name": "problem"}},
                {"id": "cc3", "name": "Stale", "module": {"name": "request"}, "inactive": True},
            ],
            "request_templates": [
                {"id": "rt1", "template_name": "Password Reset", "is_service_template": False}
            ],
            "requesttypes": [{"id": "ci1", "name": "Incident"}],
            "requesters": [{"id": "r1", "name": "Bob", "email_id": "bob@x.y"}],
            "response_status": {"status": "success"},
        }

    monkeypatch.setattr(sdp_mcp, "_request", fake)
    # Reset module-level caches so tests don't leak into each other.
    monkeypatch.setattr(sdp_mcp, "_status_ids", None)
    monkeypatch.setattr(sdp_mcp, "_own_tech_id", {})
    monkeypatch.setenv("SDP_TECH_ID", "t1")
    return calls


def _call_of(calls, method, suffix):
    """Find the recorded call for a method whose path ends with suffix."""
    matches = [c for c in calls if c["method"] == method and c["path"].endswith(suffix)]
    assert matches, f"no {method} call ending with {suffix}; got {[(c['method'], c['path']) for c in calls]}"
    return matches[-1]


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def test_list_requests(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_requests(filter_name="Open_Requests", row_count=500))
    call = _call_of(calls, "GET", "/requests")
    list_info = call["input_data"]["list_info"]
    assert list_info["filter_by"] == {"name": "Open_Requests"}
    assert list_info["row_count"] == 100  # capped
    assert out["requests"][0]["id"] == "101"
    assert out["requests"][0]["status"] == "Open"  # flattened to name


def test_search_requests(monkeypatch):
    calls = _fake_api(monkeypatch)
    with pytest.raises(ValueError):
        sdp_mcp.search_requests(field="subject", value="")
    out = json.loads(sdp_mcp.search_requests(field="subject", value="printer"))
    criteria = _call_of(calls, "GET", "/requests")["input_data"]["list_info"]["search_criteria"]
    assert criteria == [{"field": "subject", "value": "printer", "condition": "contains"}]
    assert out["list_info"]["count"] == 1


def test_get_request_with_notes(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.get_request("101", get_notes=True))
    _call_of(calls, "GET", "/requests/101")
    assert out["notes"] == [{"id": "n1", "description": "a note"}]


def test_get_request_conversation(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.get_request_conversation("101"))
    _call_of(calls, "GET", "/requests/101/conversations")
    assert out["conversations"]


def test_list_request_filters_is_local(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_request_filters())
    assert calls == []  # no API call
    names = {f["filter_name"] for f in out["filters"]}
    assert "All_Requests" in names and "My_Open_Requests" in names


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


def test_create_request(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.create_request(
        subject="Broken printer",
        requester_email="bob@x.y",
        technician="Te Ch",
        priority="High",
    )
    call = _call_of(calls, "POST", "/requests")
    assert call["form"] is True
    req = call["input_data"]["request"]
    assert req["subject"] == "Broken printer"
    assert req["requester"] == {"email_id": "bob@x.y"}
    assert req["technician"] == {"name": "Te Ch"}
    assert req["priority"] == {"name": "High"}
    assert "status" not in req  # empty optionals are omitted


def test_create_request_requester_by_name(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.create_request(subject="x", requester_name="Bob")
    req = _call_of(calls, "POST", "/requests")["input_data"]["request"]
    assert req["requester"] == {"name": "Bob"}


def test_update_request_normalizes_fields(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.update_request(
        "101",
        json.dumps(
            {
                "status": "Closed",  # bare string -> resolved to id via /statuses
                "technician": "Te Ch",  # bare string -> {"name": ...}
                "subject": "fixed",
            }
        ),
    )
    call = _call_of(calls, "PUT", "/requests/101")
    assert call["form"] is True
    req = call["input_data"]["request"]
    assert req["status"] == {"id": "st-9"}
    assert req["technician"] == {"name": "Te Ch"}
    assert req["subject"] == "fixed"
    # the status lookup hit /statuses first
    _call_of(calls, "GET", "/statuses")


def test_update_request_rejects_non_object(monkeypatch):
    _fake_api(monkeypatch)
    with pytest.raises(ValueError):
        sdp_mcp.update_request("101", "[1, 2]")


def test_update_request_unknown_status(monkeypatch):
    _fake_api(monkeypatch)
    with pytest.raises(ValueError, match="Unknown status"):
        sdp_mcp.update_request("101", json.dumps({"status": "Nonsense"}))


def test_assign_request(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.assign_request("101", "Te Ch", group="Service Desk")
    call = _call_of(calls, "PUT", "/requests/101/assign")
    assert call["input_data"] == {"technician_name": "Te Ch", "group_name": "Service Desk"}


def test_pickup_request(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.pickup_request("101")
    _call_of(calls, "PUT", "/requests/101/pickup")


def test_resolve_request_sets_resolution(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.resolve_request("101", "Replaced the drum")
    req = _call_of(calls, "PUT", "/requests/101")["input_data"]["request"]
    assert req == {"resolution": {"content": "Replaced the drum"}}


def test_resolve_request_with_close(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(
        sdp_mcp.resolve_request("101", "Done", close=True, closure_code="Success")
    )
    close = _call_of(calls, "PUT", "/requests/101/close")
    info = close["input_data"]["request"]["closure_info"]
    assert info["closure_code"] == {"name": "Success"}
    assert info["closure_comments"] == "Done"  # defaults to the resolution text
    assert "close" in out


def test_close_request_missing_resolution_guidance(monkeypatch):
    _fake_api(monkeypatch, close_fails=True)
    with pytest.raises(RuntimeError, match="resolve_request"):
        sdp_mcp.close_request("101", closure_comments="done")


def test_add_note(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.add_note("101", "Looking into it", notify_requester=True)
    call = _call_of(calls, "POST", "/requests/101/notes")
    note = call["input_data"]["request_note"]
    assert note["description"] == "Looking into it"
    assert note["show_to_requester"] is True


def test_delete_note(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.delete_note("101", "n1")
    _call_of(calls, "DELETE", "/requests/101/notes/n1")


@pytest.mark.parametrize(
    "spec, hours, minutes",
    [("1:30", "1", "30"), ("1.5", "1", "30"), ("90m", "1", "30"),
     ("1h30m", "1", "30"), ("30m", "0", "30"), ("2h", "2", "0"),
     ("0:15", "0", "15"), ("2", "2", "0")],
)
def test_parse_time_spent(spec, hours, minutes):
    assert sdp_mcp._parse_time_spent(spec) == (hours, minutes)


def test_parse_time_spent_invalid():
    with pytest.raises(ValueError):
        sdp_mcp._parse_time_spent("1:2:3")
    with pytest.raises(ValueError):
        sdp_mcp._parse_time_spent("abc")


def test_add_worklog(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.add_worklog("101", "Fixed it", time_spent="1:30", work_done="Replaced drum")
    call = _call_of(calls, "POST", "/requests/101/worklogs")
    log = call["input_data"]["worklog"]
    assert log["time_spent"] == {"hours": "1", "minutes": "30"}
    assert "Fixed it" in log["description"] and "Replaced drum" in log["description"]
    assert log["owner"] == {"id": "t1"}  # from SDP_TECH_ID


def test_add_worklog_large_hours_requires_confirmation(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.add_worklog("101", "Marathon", time_spent="12:00"))
    assert out["confirmation_required"] is True
    assert calls == []  # refused before touching the API
    out = json.loads(
        sdp_mcp.add_worklog("101", "Marathon", time_spent="12:00", confirm_large_hours=True)
    )
    assert len(calls) == 1  # retried with confirmation


def test_list_worklogs(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_worklogs("101"))
    _call_of(calls, "GET", "/requests/101/worklogs")
    assert out["worklogs"]


def test_delete_worklog(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.delete_worklog("101", "w1")
    _call_of(calls, "DELETE", "/requests/101/worklogs/w1")


def test_delete_request_to_trash(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.delete_request("101")
    _call_of(calls, "DELETE", "/requests/101")


def test_delete_request_permanent(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.delete_request("101", permanent=True)
    call = _call_of(calls, "PUT", "/requests/101")
    assert call["input_data"]["request"] == {"is_removed": True}


def test_restore_request(monkeypatch):
    calls = _fake_api(monkeypatch)
    sdp_mcp.restore_request("101")
    _call_of(calls, "PUT", "/requests/101/restore_from_trash")


# ---------------------------------------------------------------------------
# Lookup tools
# ---------------------------------------------------------------------------


def test_list_technicians(monkeypatch):
    _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_technicians())
    assert out[0]["name"] == "Te Ch"
    assert out[0]["level"] == "Agent"


def test_list_groups(monkeypatch):
    _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_groups())
    assert out == [{"id": "g1", "name": "Service Desk", "status": "Active"}]


def test_list_priorities(monkeypatch):
    _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_priorities())
    assert out == [{"id": "p1", "name": "High"}]


def test_list_closure_codes_filters_inactive_and_other_modules(monkeypatch):
    _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_closure_codes())
    assert out == [{"id": "cc1", "name": "Success"}]


def test_list_statuses(monkeypatch):
    _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_statuses())
    assert {"id": "st-1", "name": "Open"} in out


def test_list_request_templates(monkeypatch):
    _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_request_templates())
    assert out == [{"id": "rt1", "name": "Password Reset", "is_service_template": False}]


def test_list_cii_types(monkeypatch):
    _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_cii_types())
    assert out == [{"id": "ci1", "name": "Incident"}]


def test_list_requesters_search(monkeypatch):
    calls = _fake_api(monkeypatch)
    out = json.loads(sdp_mcp.list_requesters(search_text="bob"))
    call = _call_of(calls, "GET", "/requesters")
    assert call["input_data"]["list_info"]["search_text"] == "bob"
    assert out[0]["email_id"] == "bob@x.y"


# ---------------------------------------------------------------------------
# Registration & annotations
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registered_tools():
    return asyncio.run(sdp_mcp.mcp.list_tools())


def test_all_tools_registered(registered_tools):
    assert {t.name for t in registered_tools} == EXPECTED_TOOLS


@pytest.mark.parametrize("tool", sorted(EXPECTED_TOOLS))
def test_every_tool_declares_all_four_hints(registered_tools, tool):
    t = next(x for x in registered_tools if x.name == tool)
    ann = t.annotations
    assert ann is not None, f"{tool} has no annotations"
    for hint in (
        ann.read_only_hint,
        ann.destructive_hint,
        ann.idempotent_hint,
        ann.open_world_hint,
    ):
        assert isinstance(hint, bool), f"{tool} is missing a boolean hint"


def test_annotation_values_are_sane(registered_tools):
    by_name = {t.name: t.annotations for t in registered_tools}
    # read tools never destroy and don't change state
    for name in ("list_requests", "get_request", "search_requests", "list_worklogs"):
        assert by_name[name].read_only_hint is True
        assert by_name[name].destructive_hint is False
    # deletes are flagged destructive
    for name in ("delete_request", "delete_note", "delete_worklog"):
        assert by_name[name].destructive_hint is True
    # creates are not idempotent (each call makes a new object)
    for name in ("create_request", "add_note", "add_worklog"):
        assert by_name[name].idempotent_hint is False
    # the static filter list never leaves the process
    assert by_name["list_request_filters"].open_world_hint is False
    # everything else talks to the external SDP API
    assert by_name["get_request"].open_world_hint is True
