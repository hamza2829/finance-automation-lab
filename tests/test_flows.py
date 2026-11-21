import copy
import json
from pathlib import Path

import pytest

from flows import validate_flows as vf

FLOW_FILES = sorted(vf.FLOWS_DIR.glob("*.json"))

MINIMAL = {
    "name": "t",
    "properties": {
        "displayName": "T",
        "definition": {
            "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {"apiBaseUrl": {"type": "String", "defaultValue": "http://x"}},
            "triggers": {"manual": {"type": "Request", "kind": "Http", "inputs": {}}},
            "actions": {
                "Get_invoice": {"type": "Http", "runAfter": {},
                                "inputs": {"method": "GET", "uri": "@{parameters('apiBaseUrl')}/invoices/@{triggerBody()?['id']}"}},
                "Check": {"type": "If", "runAfter": {"Get_invoice": ["Succeeded"]},
                          "expression": {"equals": ["@body('Get_invoice')?['status']", "approved"]},
                          "actions": {"Done": {"type": "Compose", "runAfter": {}, "inputs": "@body('Get_invoice')"}}},
            },
        },
    },
}


def flow(mutate=None) -> dict:
    doc = copy.deepcopy(MINIMAL)
    if mutate:
        mutate(doc["properties"]["definition"])
    return doc


def messages(doc) -> list[str]:
    return [i.message for i in vf.validate_document("t", doc)]


@pytest.mark.parametrize("path", FLOW_FILES, ids=[p.stem for p in FLOW_FILES])
def test_repository_flows_are_valid(path):
    assert vf.validate_file(path) == []


def test_repository_has_the_three_flows():
    assert {p.stem for p in FLOW_FILES} == {"invoice_received", "cost_center_approval", "escalation_after_3_days"}


def test_repository_flows_only_call_existing_api_routes():
    routes = vf.api_routes()
    for path in FLOW_FILES:
        assert vf.validate_file(path, routes) == [], path.name


def test_minimal_flow_is_valid():
    assert messages(flow()) == []


def test_missing_trigger():
    assert "flow has no trigger" in messages(flow(lambda d: d.update(triggers={})))


def test_two_triggers_are_rejected():
    def two(d):
        d["triggers"]["second"] = {"type": "Recurrence", "recurrence": {"frequency": "Day", "interval": 1}}
    assert any("exactly one trigger" in m for m in messages(flow(two)))


def test_invalid_recurrence():
    def rec(d):
        d["triggers"] = {"daily": {"type": "Recurrence", "recurrence": {"frequency": "Fortnight", "interval": 0}}}
    msgs = messages(flow(rec))
    assert any("frequency" in m for m in msgs) and any("interval" in m for m in msgs)


def test_unknown_action_type():
    assert any("unknown action type" in m for m in messages(flow(lambda d: d["actions"]["Get_invoice"].update(type="Magic"))))


def test_missing_run_after_is_an_error():
    assert any("missing explicit runAfter" in m for m in messages(flow(lambda d: d["actions"]["Get_invoice"].pop("runAfter"))))


def test_run_after_unknown_action():
    def bad(d):
        d["actions"]["Check"]["runAfter"] = {"Nope": ["Succeeded"]}
    assert any("references 'Nope'" in m for m in messages(flow(bad)))


def test_run_after_invalid_status():
    def bad(d):
        d["actions"]["Check"]["runAfter"] = {"Get_invoice": ["Finished"]}
    assert any("invalid status" in m for m in messages(flow(bad)))


def test_run_after_cycle_is_detected():
    def cycle(d):
        d["actions"]["Get_invoice"]["runAfter"] = {"Check": ["Succeeded"]}
    assert any("cycle" in m for m in messages(flow(cycle)))


def test_action_after_terminate_is_a_dead_branch():
    def dead(d):
        d["actions"]["Stop"] = {"type": "Terminate", "runAfter": {"Check": ["Succeeded"]}, "inputs": {"runStatus": "Cancelled"}}
        d["actions"]["Never"] = {"type": "Compose", "runAfter": {"Stop": ["Succeeded"]}, "inputs": 1}
    assert any("runs after Terminate" in m for m in messages(flow(dead)))


def test_empty_true_branch_is_a_dead_branch():
    assert any("empty true branch" in m for m in messages(flow(lambda d: d["actions"]["Check"].update(actions={}))))


def test_constant_condition_is_a_dead_branch():
    def const(d):
        d["actions"]["Check"]["expression"] = {"equals": [1, 1]}
    assert any("condition is constant" in m for m in messages(flow(const)))


def test_switch_with_duplicate_and_empty_cases():
    def sw(d):
        d["actions"]["Route"] = {
            "type": "Switch", "runAfter": {"Get_invoice": ["Succeeded"]},
            "expression": "@body('Get_invoice')?['status']",
            "cases": {"A": {"case": "x", "actions": {"Ax": {"type": "Compose", "runAfter": {}, "inputs": 1}}},
                      "B": {"case": "x", "actions": {}}},
        }
    msgs = messages(flow(sw))
    assert any("duplicate case value" in m for m in msgs) and any("case has no actions" in m for m in msgs)


def test_reference_to_action_that_has_not_run_yet():
    def order(d):
        d["actions"]["Get_invoice"]["inputs"]["uri"] = "@{parameters('apiBaseUrl')}/invoices/@{body('Check')}"
    assert any("run order" in m for m in messages(flow(order)))


def test_reference_from_nested_action_to_parent_scope_is_allowed():
    # 'Done' (inside the If) reads body('Get_invoice'), which ran before the If
    assert messages(flow()) == []


def test_items_outside_foreach():
    def items(d):
        d["actions"]["Check"]["actions"]["Done"]["inputs"] = "@items('Loop')"
    assert any("items('Loop') used outside" in m for m in messages(flow(items)))


def test_undeclared_parameter():
    def param(d):
        d["actions"]["Get_invoice"]["inputs"]["uri"] = "@{parameters('missing')}/x"
    assert any("parameters('missing') is not declared" in m for m in messages(flow(param)))


def test_duplicate_action_name_across_scopes():
    def dup(d):
        d["actions"]["Check"]["actions"]["Get_invoice"] = {"type": "Compose", "runAfter": {}, "inputs": 1}
    assert any("duplicate action name" in m for m in messages(flow(dup)))


def test_empty_foreach_is_a_dead_branch():
    def loop(d):
        d["actions"]["Loop"] = {"type": "Foreach", "runAfter": {}, "foreach": "@triggerBody()", "actions": {}}
    assert any("Foreach has no actions" in m for m in messages(flow(loop)))


def test_unknown_api_route_is_reported():
    doc = flow(lambda d: d["actions"]["Get_invoice"]["inputs"].update(uri="@{parameters('apiBaseUrl')}/payments/@{triggerBody()}"))
    issues = vf.check_api("t", doc, vf.api_routes())
    assert len(issues) == 1 and "GET /payments/{} does not match" in issues[0].message


def test_inline_expression_with_question_mark_is_not_a_query_string():
    calls = vf.http_calls(flow())
    assert calls == [("Get_invoice", "GET", "/invoices/{}")]


def test_cli_exit_codes(tmp_path: Path, capsys):
    assert vf.main([str(p) for p in FLOW_FILES]) == 0
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(flow(lambda d: d.update(triggers={}))), encoding="utf-8")
    assert vf.main([str(broken)]) == 1
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    assert vf.main([str(invalid)]) == 1
    assert "invalid JSON" in capsys.readouterr().out
