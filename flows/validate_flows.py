"""Static validator for Power Automate style flow definitions.

Checks (each issue carries a JSON path):

structure    export envelope, $schema, contentVersion, triggers, actions
triggers     exactly one trigger, known type, valid recurrence / request kind
actions      known type, explicit ``runAfter``, unique names in the whole flow
run order    runAfter targets exist in the same scope, valid statuses, no
             self-reference, no cycles; expressions may only reference
             (body/outputs/actions) actions that are guaranteed to have run
             before (upstream in runAfter or upstream of an enclosing scope),
             ``items('X')`` only inside foreach X, ``parameters('p')`` declared
dead branch  If with empty true branch or constant condition, Switch without
             cases / empty or duplicate case, empty Foreach/Until/Scope,
             actions that run after a Terminate
api          (optional ``--check-api``) every Http call to ``apiBaseUrl``
             matches a route of the FastAPI app (method + path)

Usage:  python -m flows.validate_flows [--check-api] [files...]
Exit code 0 = no errors, 1 = errors found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

FLOWS_DIR = Path(__file__).resolve().parent

TRIGGER_TYPES = {"Request", "Recurrence", "OpenApiConnectionWebhook", "OpenApiConnection",
                 "ApiConnectionWebhook", "ApiConnection"}
ACTION_TYPES = {"Http", "OpenApiConnection", "OpenApiConnectionWebhook", "ApiConnection",
                "ApiConnectionWebhook", "If", "Switch", "Foreach", "Until", "Scope", "Compose",
                "Terminate", "InitializeVariable", "SetVariable", "IncrementVariable",
                "AppendToArrayVariable", "ParseJson", "Response", "Wait", "Query", "Select", "Table"}
RUN_STATUSES = {"Succeeded", "Failed", "Skipped", "TimedOut"}
FREQUENCIES = {"Second", "Minute", "Hour", "Day", "Week", "Month"}
CONTAINERS = {"If", "Switch", "Foreach", "Until", "Scope"}

REF_RE = re.compile(r"\b(body|outputs|actions|result|items)\(\s*'([^']+)'\s*\)")
PARAM_RE = re.compile(r"\bparameters\(\s*'([^']+)'\s*\)")
API_PREFIX = "@{parameters('apiBaseUrl')}"
INLINE_EXPR_RE = re.compile(r"@\{[^}]*\}")


@dataclass(frozen=True)
class Issue:
    flow: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.flow}: {self.path}: {self.message}"


@dataclass
class ActionInfo:
    name: str
    path: str
    body: dict
    scope_id: str          # identifies the sibling group (scope) the action lives in
    container: str | None  # name of the enclosing container action, None at top level


# --------------------------------------------------------------------------- helpers
def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _child_scopes(action: dict) -> list[tuple[str, dict]]:
    """(suffix, actions-dict) for every nested action group of a container action."""
    kind = action.get("type")
    groups: list[tuple[str, dict]] = []
    if kind == "If":
        groups.append(("actions", action.get("actions") or {}))
        if "else" in action:
            groups.append(("else.actions", (action.get("else") or {}).get("actions") or {}))
    elif kind == "Switch":
        for case_name, case in (action.get("cases") or {}).items():
            groups.append((f"cases.{case_name}.actions", (case or {}).get("actions") or {}))
        if "default" in action:
            groups.append(("default.actions", (action.get("default") or {}).get("actions") or {}))
    elif kind in ("Foreach", "Until", "Scope"):
        groups.append(("actions", action.get("actions") or {}))
    return groups


def _own_expression_fields(action: dict) -> dict:
    """The parts of an action that are evaluated by the action itself (not its children)."""
    keys = ("inputs", "expression", "foreach", "limit")
    return {k: action[k] for k in keys if k in action}


def _is_constant_condition(expression: Any) -> bool:
    if isinstance(expression, bool):
        return True
    if isinstance(expression, str):
        return expression.strip().lower() in ("@true", "@false", "true", "false") or "@" not in expression
    return not any("@" in s for s in _strings(expression))


# --------------------------------------------------------------------------- validator
class FlowValidator:
    def __init__(self, flow_name: str, document: dict) -> None:
        self.flow = flow_name
        self.doc = document
        self.issues: list[Issue] = []
        self.actions: dict[str, ActionInfo] = {}
        self.scopes: dict[str, dict[str, dict]] = {}       # scope_id -> sibling actions
        self.scope_owner: dict[str, str | None] = {}       # scope_id -> container action name

    def error(self, path: str, message: str) -> None:
        self.issues.append(Issue(self.flow, path, message))

    # ---- entry point
    def validate(self) -> list[Issue]:
        definition = self._check_envelope()
        if definition is None:
            return self.issues
        self.parameters = set((definition.get("parameters") or {}).keys())
        trigger_names = self._check_triggers(definition)
        actions = definition.get("actions")
        if not isinstance(actions, dict) or not actions:
            self.error("definition.actions", "flow has no actions")
            return self.issues
        self._collect("root", None, "definition.actions", actions, trigger_names)
        for scope_id, siblings in self.scopes.items():
            self._check_run_after(scope_id, siblings)
        for info in self.actions.values():
            self._check_action(info)
        return self.issues

    def _check_envelope(self) -> dict | None:
        if not isinstance(self.doc, dict):
            self.error("$", "document is not a JSON object")
            return None
        if not self.doc.get("name"):
            self.error("name", "missing flow name")
        props = self.doc.get("properties")
        if not isinstance(props, dict):
            self.error("properties", "missing properties object")
            return None
        if not props.get("displayName"):
            self.error("properties.displayName", "missing displayName")
        definition = props.get("definition")
        if not isinstance(definition, dict):
            self.error("properties.definition", "missing workflow definition")
            return None
        if "workflowdefinition.json" not in str(definition.get("$schema", "")):
            self.error("definition.$schema", "not a Logic Apps / Power Automate workflow definition schema")
        if not definition.get("contentVersion"):
            self.error("definition.contentVersion", "missing contentVersion")
        return definition

    def _check_triggers(self, definition: dict) -> set[str]:
        triggers = definition.get("triggers")
        if not isinstance(triggers, dict) or not triggers:
            self.error("definition.triggers", "flow has no trigger")
            return set()
        if len(triggers) > 1:
            self.error("definition.triggers", f"exactly one trigger allowed, found {len(triggers)}")
        for name, trig in triggers.items():
            path = f"definition.triggers.{name}"
            kind = (trig or {}).get("type")
            if kind not in TRIGGER_TYPES:
                self.error(path, f"unknown trigger type {kind!r}")
            if kind == "Recurrence":
                rec = trig.get("recurrence") or {}
                if rec.get("frequency") not in FREQUENCIES:
                    self.error(path, f"invalid recurrence frequency {rec.get('frequency')!r}")
                if not isinstance(rec.get("interval"), int) or rec.get("interval", 0) < 1:
                    self.error(path, "recurrence interval must be an integer >= 1")
            if kind == "Request" and trig.get("kind") not in ("Http", "Button"):
                self.error(path, f"request trigger kind must be Http or Button, got {trig.get('kind')!r}")
        return set(triggers)

    def _collect(self, scope_id: str, container: str | None, path: str, actions: dict,
                 reserved: set[str]) -> None:
        self.scopes[scope_id] = actions
        self.scope_owner[scope_id] = container
        for name, body in actions.items():
            apath = f"{path}.{name}"
            if not isinstance(body, dict):
                self.error(apath, "action is not an object")
                continue
            if name in self.actions or name in reserved:
                self.error(apath, f"duplicate action name {name!r} (names must be unique in a flow)")
                continue
            self.actions[name] = ActionInfo(name, apath, body, scope_id, container)
            for suffix, child in _child_scopes(body):
                self._collect(f"{name}:{suffix}", name, f"{apath}.{suffix}", child, reserved)

    # ---- run order
    def _check_run_after(self, scope_id: str, siblings: dict) -> None:
        graph: dict[str, list[str]] = {}
        for name, body in siblings.items():
            if not isinstance(body, dict) or self.actions.get(name, None) is None \
                    or self.actions[name].scope_id != scope_id:
                continue
            path = self.actions[name].path
            if "runAfter" not in body:
                self.error(path, "missing explicit runAfter (use {} for a first action)")
                graph[name] = []
                continue
            run_after = body["runAfter"]
            if not isinstance(run_after, dict):
                self.error(path, "runAfter must be an object")
                graph[name] = []
                continue
            deps = []
            for dep, statuses in run_after.items():
                if dep == name:
                    self.error(path, "action runs after itself")
                    continue
                if dep not in siblings:
                    where = "another scope" if dep in self.actions else "nowhere"
                    self.error(path, f"runAfter references {dep!r} which is defined in {where}")
                    continue
                if not isinstance(statuses, list) or not statuses:
                    self.error(path, f"runAfter {dep!r} needs a non-empty status list")
                elif set(statuses) - RUN_STATUSES:
                    self.error(path, f"runAfter {dep!r} has invalid status {sorted(set(statuses) - RUN_STATUSES)}")
                deps.append(dep)
                if (siblings.get(dep) or {}).get("type") == "Terminate":
                    self.error(path, f"dead branch: runs after Terminate action {dep!r}, which ends the run")
            graph[name] = deps
        cycle = _find_cycle(graph)
        if cycle:
            self.error(self.actions[cycle[0]].path, "runAfter cycle: " + " -> ".join(cycle))

    def _upstream(self, name: str) -> set[str]:
        """All actions guaranteed to have finished before ``name`` starts."""
        seen: set[str] = set()
        info = self.actions[name]
        stack = list((info.body.get("runAfter") or {}).keys()) if isinstance(info.body.get("runAfter"), dict) else []
        while stack:
            dep = stack.pop()
            if dep in seen or dep not in self.actions:
                continue
            seen.add(dep)
            dep_ra = self.actions[dep].body.get("runAfter")
            if isinstance(dep_ra, dict):
                stack.extend(dep_ra.keys())
        if info.container is not None:
            seen |= self._upstream(info.container)
        return seen

    def _enclosing(self, name: str) -> list[str]:
        chain, current = [], self.actions[name].container
        while current is not None:
            chain.append(current)
            current = self.actions[current].container
        return chain

    # ---- per action
    def _check_action(self, info: ActionInfo) -> None:
        body, path = info.body, info.path
        kind = body.get("type")
        if kind not in ACTION_TYPES:
            self.error(path, f"unknown action type {kind!r}")
        if kind == "If":
            if not (body.get("actions") or {}):
                self.error(path, "dead branch: If has an empty true branch (invert the condition)")
            if "expression" not in body:
                self.error(path, "If without expression")
            elif _is_constant_condition(body["expression"]):
                self.error(path, "dead branch: If condition is constant, one branch can never run")
        if kind == "Switch":
            cases = body.get("cases") or {}
            if not cases:
                self.error(path, "dead branch: Switch without cases")
            values = []
            for case_name, case in cases.items():
                value = (case or {}).get("case")
                if value in (None, ""):
                    self.error(f"{path}.cases.{case_name}", "case value missing")
                if value in values:
                    self.error(f"{path}.cases.{case_name}", f"dead branch: duplicate case value {value!r}")
                values.append(value)
                if not (case or {}).get("actions"):
                    self.error(f"{path}.cases.{case_name}", "dead branch: case has no actions")
        if kind in ("Foreach", "Until", "Scope") and not (body.get("actions") or {}):
            self.error(path, f"dead branch: {kind} has no actions")
        if kind == "Foreach" and "foreach" not in body:
            self.error(path, "Foreach without 'foreach' expression")
        if kind == "Http":
            inputs = body.get("inputs") or {}
            if not inputs.get("method") or not inputs.get("uri"):
                self.error(path, "Http action needs inputs.method and inputs.uri")

        upstream = self._upstream(info.name)
        enclosing = self._enclosing(info.name)
        for text in _strings(_own_expression_fields(body)):
            for func, ref in REF_RE.findall(text):
                if func == "items":
                    if ref not in enclosing or self.actions[ref].body.get("type") != "Foreach":
                        self.error(path, f"items('{ref}') used outside of foreach {ref!r}")
                    continue
                if ref not in self.actions:
                    self.error(path, f"{func}('{ref}') references an unknown action")
                elif ref not in upstream:
                    self.error(path, f"run order: {func}('{ref}') is not guaranteed to have run before this action")
            for param in PARAM_RE.findall(text):
                if param not in self.parameters:
                    self.error(path, f"parameters('{param}') is not declared")


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return stack[stack.index(dep):] + [dep]
            if state.get(dep) is None:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in graph:
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return None


# --------------------------------------------------------------------------- API cross-check
def http_calls(document: dict) -> list[tuple[str, str, str]]:
    """(action name, METHOD, normalised path) for Http actions that call apiBaseUrl."""
    calls = []

    def walk(actions: dict) -> None:
        for name, body in (actions or {}).items():
            if not isinstance(body, dict):
                continue
            if body.get("type") == "Http":
                inputs = body.get("inputs") or {}
                uri = str(inputs.get("uri", ""))
                if uri.startswith(API_PREFIX):
                    # replace inline expressions first: they contain '?' and must not be
                    # mistaken for the start of a query string
                    path = INLINE_EXPR_RE.sub("{}", uri[len(API_PREFIX):]).split("?")[0]
                    segments = ["{}" if "{}" in seg else seg for seg in path.split("/")]
                    calls.append((name, str(inputs.get("method", "")).upper(), "/".join(segments)))
            for _, child in _child_scopes(body):
                walk(child)

    walk(((document.get("properties") or {}).get("definition") or {}).get("actions") or {})
    return calls


def api_routes() -> list[tuple[str, list[str]]]:
    from app.main import create_app
    api = create_app(":memory:")
    routes = []
    for route in api.routes:
        for method in getattr(route, "methods", None) or ():
            routes.append((method, route.path.split("/")))
    return routes


def _route_matches(path: str, route_segments: list[str]) -> bool:
    segments = path.split("/")
    if len(segments) != len(route_segments):
        return False
    for seg, rseg in zip(segments, route_segments):
        is_param = rseg.startswith("{") and rseg.endswith("}")
        if seg == "{}" and not is_param:
            return False
        if seg != "{}" and not is_param and seg != rseg:
            return False
    return True


def check_api(flow_name: str, document: dict, routes: list[tuple[str, list[str]]]) -> list[Issue]:
    issues = []
    for action, method, path in http_calls(document):
        if not any(m == method and _route_matches(path, r) for m, r in routes):
            issues.append(Issue(flow_name, action, f"{method} {path} does not match any API route"))
    return issues


# --------------------------------------------------------------------------- CLI
def validate_document(flow_name: str, document: Any) -> list[Issue]:
    return FlowValidator(flow_name, document).validate()


def validate_file(path: Path, routes: list | None = None) -> list[Issue]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [Issue(path.name, "$", f"invalid JSON: {exc}")]
    issues = validate_document(path.name, document)
    if routes is not None and isinstance(document, dict):
        issues += check_api(path.name, document, routes)
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate flow definitions")
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--check-api", action="store_true", help="match Http calls against FastAPI routes")
    args = parser.parse_args(argv)
    files = args.files or sorted(FLOWS_DIR.glob("*.json"))
    routes = api_routes() if args.check_api else None
    all_issues: list[Issue] = []
    for path in files:
        issues = validate_file(path, routes)
        all_issues += issues
        print(f"{'FAIL' if issues else 'OK  '} {path.name}")
        for issue in issues:
            print(f"     {issue.path}: {issue.message}")
    print(f"{len(files)} flow(s) checked, {len(all_issues)} error(s)")
    return 1 if all_issues else 0


if __name__ == "__main__":
    sys.exit(main())
