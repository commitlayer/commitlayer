"""scripts/api_compat.py on fixture specs: one change of each kind, and the class it must produce.

fixtures/openapi-base.json is the backend's 0.9.54 schema cut down to the operations the packages use (request
schemas as the backend writes them), with typed answers on the verification reads so the response checks have
something to compare. Each test changes one thing and reads the verdict.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import api_compat  # noqa: E402

BASE = json.loads((Path(__file__).parent / "fixtures" / "openapi-base.json").read_text(encoding="utf-8"))
ALL = sorted(api_compat.api_surface.PACKAGES)
SDKS = ["sdk-js", "sdk-python"]
ACTION_WRITERS = ALL  # every package records actions


def changed(fn) -> dict:
    new = copy.deepcopy(BASE)
    fn(new)
    return api_compat.compare(BASE, new)


def kinds(result: dict, cls: str) -> set[str]:
    return {f["kind"] for f in result["findings"] if f["class"] == cls}


def affected(result: dict, cls: str) -> list[str]:
    return sorted(p for p, v in result["packages"].items() if v["classification"] == cls)


def schema(doc: dict, name: str) -> dict:
    return doc["components"]["schemas"][name]


def op(doc: dict, path: str, method: str) -> dict:
    return doc["paths"][path][method]


# ---------------- none ----------------


def test_identical_specs_are_none():
    r = api_compat.compare(BASE, copy.deepcopy(BASE))
    assert r["classification"] == "none" and r["findings"] == [] and r["affected_packages"] == []
    assert r["warnings"] == []


def test_a_change_no_package_uses_is_none_but_counted():
    def fn(d):
        d["paths"]["/v1/onboarding/sample"]["post"]["parameters"] = [
            {"name": "x", "in": "query", "required": True, "schema": {"type": "string"}}
        ]
        d["paths"]["/v1/onboarding/other"] = {"get": {"responses": {"200": {"description": "ok"}}}}
        d["paths"].pop("/v1/onboarding/sample")

    r = changed(fn)
    assert r["classification"] == "none"
    assert r["other_changes"]["operations_added"] == 1 and r["other_changes"]["operations_removed"] == 1


def test_the_version_alone_is_none():
    r = changed(lambda d: d["info"].update(version="9.9.9"))
    assert r["classification"] == "none"


def test_a_field_widened_in_a_request_is_none():
    def fn(d):
        schema(d, "ActionIn")["properties"]["subject_id"]["maxLength"] = 400
        schema(d, "ActionIn")["properties"]["contract_version"] = {"anyOf": [{"type": "number"}, {"type": "null"}]}

    assert changed(fn)["classification"] == "none"


# ---------------- breaking ----------------


def test_removing_an_operation_a_package_calls_is_breaking_for_that_package_only():
    r = changed(lambda d: d["paths"].pop("/v1/packs/{pack_id}/coverage"))
    assert r["classification"] == "breaking" and kinds(r, "breaking") == {"operation_removed"}
    assert affected(r, "breaking") == SDKS and affected(r, "none") == sorted(set(ALL) - set(SDKS))


def test_renaming_a_request_field_is_breaking():
    def fn(d):
        props = schema(d, "ActionIn")["properties"]
        props["contract"] = props.pop("contract_id")
        schema(d, "ActionIn")["required"] = ["contract", "subject_id"]

    r = changed(fn)
    assert "request_field_removed" in kinds(r, "breaking") and affected(r, "breaking") == ACTION_WRITERS


def test_a_request_type_that_no_longer_takes_what_is_sent_is_breaking():
    r = changed(lambda d: schema(d, "ActionIn")["properties"].update(subject_id={"type": "integer"}))
    assert kinds(r, "breaking") == {"request_field_changed"}
    assert any("type string → integer" in f["detail"] for f in r["findings"])


def test_a_narrower_string_limit_is_breaking():
    r = changed(lambda d: schema(d, "ActionIn")["properties"]["subject_id"].update(maxLength=50))
    assert kinds(r, "breaking") == {"request_field_changed"} and "maxLength 200 → 50" in r["findings"][0]["detail"]


def test_optional_null_no_longer_accepted_is_breaking():
    # draft_contract sends action_type: null when none is given
    r = changed(lambda d: schema(d, "DraftIn")["properties"].update(action_type={"type": "string"}))
    assert r["classification"] == "breaking" and affected(r, "breaking") == SDKS


def test_a_new_required_field_the_package_does_not_send_is_breaking():
    def fn(d):
        schema(d, "ActionIn")["properties"]["tenant"] = {"type": "string"}
        schema(d, "ActionIn")["required"].append("tenant")

    r = changed(fn)
    assert "request_field_required" in kinds(r, "breaking") and affected(r, "breaking") == ACTION_WRITERS


def test_an_optional_field_made_required_is_breaking_where_it_is_not_always_sent():
    r = changed(lambda d: schema(d, "ActionIn")["required"].append("channel"))
    # the recorders and integrations always send a channel; the SDKs send one only when given
    assert affected(r, "breaking") == SDKS


def test_a_field_made_required_that_every_package_always_sends_is_not_breaking():
    r = changed(lambda d: schema(d, "ActionIn")["required"].append("action_id"))
    assert r["classification"] == "none"


def test_a_new_required_field_inside_a_fact_breaks_the_package_that_builds_facts():
    def fn(d):
        schema(d, "FactIn")["properties"]["confidence"] = {"type": "number"}
        schema(d, "FactIn")["required"].append("confidence")

    r = changed(fn)
    assert affected(r, "breaking") == ["integrations-js", "integrations-python"]
    assert any(f["detail"].startswith("body field `facts[].confidence`") for f in r["findings"])


def test_a_query_parameter_removed_or_a_new_required_one_is_breaking():
    def removed(d):
        op(d, "/v1/actions/{action_ref}/wait", "get")["parameters"] = [
            p for p in op(d, "/v1/actions/{action_ref}/wait", "get")["parameters"] if p["name"] != "timeout"
        ]

    assert kinds(changed(removed), "breaking") == {"parameter_removed"}

    def required(d):
        op(d, "/v1/actions/{action_ref}", "get")["parameters"].append(
            {"name": "workspace", "in": "query", "required": True, "schema": {"type": "string"}}
        )

    r = changed(required)
    assert kinds(r, "breaking") == {"parameter_required"} and r["classification"] == "breaking"


def test_a_required_header_is_breaking():
    def fn(d):
        op(d, "/v1/facts", "post")["parameters"] = [
            {"name": "X-Source", "in": "header", "required": True, "schema": {"type": "string"}}
        ]

    r = changed(fn)
    assert "parameter_required" in kinds(r, "breaking")


def test_a_response_field_a_package_reads_removed_is_breaking():
    r = changed(lambda d: schema(d, "Verification")["properties"].pop("summary"))
    assert kinds(r, "breaking") == {"response_field_removed"}
    assert affected(r, "breaking") == ["integrations-js", "integrations-python"]


def test_a_response_type_that_can_now_hold_something_else_is_breaking():
    r = changed(
        lambda d: schema(d, "Verification")["properties"].update(
            timed_out={"anyOf": [{"type": "boolean"}, {"type": "null"}]}
        )
    )
    assert kinds(r, "breaking") == {"response_field_changed"}
    assert set(affected(r, "breaking")) == {"integrations-python", "sdk-js", "sdk-python"}


def test_a_new_verdict_value_is_breaking_for_the_readers():
    r = changed(lambda d: schema(d, "Lens")["properties"]["verdict"]["enum"].append("COMPENSATED"))
    assert kinds(r, "breaking") == {"response_field_changed"} and "COMPENSATED" in r["findings"][0]["detail"]


def test_a_removed_json_body_is_breaking():
    r = changed(lambda d: op(d, "/v1/contracts/validate", "post").pop("requestBody"))
    assert kinds(r, "breaking") == {"request_body_removed"} and affected(r, "breaking") == SDKS


# ---------------- additive_relevant ----------------


def test_a_new_optional_request_field_is_additive_relevant():
    r = changed(
        lambda d: schema(d, "ActionIn")["properties"].update(priority={"anyOf": [{"type": "string"}, {"type": "null"}]})
    )
    assert r["classification"] == "additive_relevant" and kinds(r, "additive_relevant") == {"request_field_added"}
    assert affected(r, "additive_relevant") == ACTION_WRITERS


def test_a_new_operation_on_a_wrapped_resource_is_additive_relevant():
    def fn(d):
        d["paths"]["/v1/packs/{pack_id}/history"] = {"get": {"responses": {"200": {"description": "ok"}}}}

    r = changed(fn)
    assert r["classification"] == "additive_relevant" and affected(r, "additive_relevant") == SDKS
    assert kinds(r, "additive_relevant") == {"operation_added"}


def test_a_new_optional_parameter_and_a_new_answer_field_are_additive_relevant():
    def fn(d):
        op(d, "/v1/actions/{action_ref}", "get")["parameters"].append(
            {"name": "expand", "in": "query", "required": False, "schema": {"type": "string"}}
        )
        schema(d, "Verification")["properties"]["confidence"] = {"type": "number"}

    r = changed(fn)
    assert r["classification"] == "additive_relevant"
    assert kinds(r, "additive_relevant") == {"parameter_added", "response_field_added"}


def test_deprecating_an_operation_in_use_is_additive_relevant():
    r = changed(lambda d: op(d, "/v1/facts", "post").update(deprecated=True))
    assert kinds(r, "additive_relevant") == {"operation_deprecated"}


def test_breaking_outranks_additive():
    def fn(d):
        schema(d, "ActionIn")["properties"]["priority"] = {"type": "string"}
        d["paths"].pop("/v1/contracts")

    r = changed(fn)
    assert r["classification"] == "breaking"
    assert r["packages"]["sdk-python"]["classification"] == "breaking"
    assert r["packages"]["recorder-js"]["classification"] == "additive_relevant"


# ---------------- output ----------------


def test_untyped_answers_are_listed_as_unchecked():
    r = api_compat.compare(BASE, BASE)
    fields = {(u["package"], u["field"]) for u in r["unchecked_response_fields"]}
    assert ("integrations-python", "action.action_id") in fields  # the POST /v1/actions answer is untyped


def test_cli_writes_json_and_markdown(tmp_path):
    new = copy.deepcopy(BASE)
    new["paths"].pop("/v1/packs/{pack_id}/coverage")
    (tmp_path / "old.json").write_text(json.dumps(BASE))
    (tmp_path / "new.json").write_text(json.dumps(new))
    code = api_compat.main(
        [
            "--old",
            str(tmp_path / "old.json"),
            "--new",
            str(tmp_path / "new.json"),
            "--old-label",
            "0.9.1",
            "--new-label",
            "0.9.2",
            "--json",
            str(tmp_path / "c.json"),
            "--markdown",
            str(tmp_path / "c.md"),
        ]
    )
    assert code == 0
    data = json.loads((tmp_path / "c.json").read_text())
    assert data["classification"] == "breaking" and data["old"] == "0.9.1" and data["new"] == "0.9.2"
    md = (tmp_path / "c.md").read_text()
    assert (
        md.startswith("## API compatibility 0.9.1 → 0.9.2: `breaking`")
        and "| breaking | sdk-js | GET `/v1/packs/{pack_id}/coverage` |" in md
    )


def test_cli_refuses_unreadable_input(tmp_path):
    (tmp_path / "bad.json").write_text("{")
    assert api_compat.main(["--old", str(tmp_path / "bad.json"), "--new", str(tmp_path / "bad.json")]) == 2


@pytest.mark.parametrize("cls", api_compat.CLASSES)
def test_markdown_names_the_class(cls):
    r = {
        "classification": cls,
        "affected_packages": [],
        "packages": {},
        "findings": [],
        "unchecked_response_fields": [],
        "other_changes": {
            "operations_added": 0,
            "operations_removed": 0,
            "operations_changed": 0,
            "schemas_changed": [],
        },
        "warnings": [],
    }
    assert f"`{cls}`" in api_compat.to_markdown(r, "a", "b")
