"""Does an API release touch what the client packages use? Compare two OpenAPI documents against the inventory.

    python scripts/api_compat.py --old old/openapi.json --new new/openapi.json \\
        --old-label 0.9.52 --new-label 0.9.54 --json build/compat.json --markdown build/compat.md

The classification, for the whole release and per package (scripts/api_surface.py says what each one uses):

- `breaking`: something a package uses was removed, renamed or changed so the package's requests or its reading
  of the answers no longer fit: an operation it calls is gone; a body field, query parameter or response field it
  uses is gone; a field's type no longer accepts what the package sends (or an answer may now hold a type the
  package does not expect); a string limit or an enum got narrower; a field or parameter became required that the
  package does not always send.
- `additive_relevant`: something new on a resource a package already wraps: a new operation under the same
  resource (`/v1/actions/...`), a new body field or parameter on an operation it calls, a new field in a typed
  answer, an operation it calls now deprecated.
- `none`: nothing above; changes elsewhere in the API are counted, not listed.

Most of CommitLayer's answers are untyped in the schema (`object`, additional properties), so a response field can
only be checked where the schema names it. Every response field a package reads from an untyped answer is listed
under `unchecked`: the live contract check (tests/live/) is what covers them.

The exit status is 0 whatever the class; the class is the output. 2 means the inputs could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_surface  # noqa: E402

CLASSES = ("none", "additive_relevant", "breaking")
METHODS = ("get", "post", "put", "patch", "delete")
MISSING = object()  # a field the schema does not have
UNTYPED = object()  # a field below an object the schema does not describe


# ---------------- schema helpers ----------------


class Spec:
    def __init__(self, doc: dict[str, Any]):
        self.doc = doc
        self.ops: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for path, item in (doc.get("paths") or {}).items():
            for method in METHODS:
                if isinstance(item, dict) and method in item:
                    self.ops[(method.upper(), api_surface.normalise_path(path))] = (path, item[method])

    def resolve(self, schema: Any, depth: int = 0) -> dict[str, Any]:
        """Follow `$ref`, fold `allOf`, and unwrap an optional (`anyOf: [X, null]`) to X."""
        if not isinstance(schema, dict) or depth > 40:
            return {}
        if "$ref" in schema:
            ref = schema["$ref"]
            node: Any = self.doc
            for part in ref.lstrip("#/").split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            return self.resolve(node, depth + 1)
        if "allOf" in schema:
            merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            for sub in schema["allOf"]:
                r = self.resolve(sub, depth + 1)
                merged["properties"].update(r.get("properties") or {})
                merged["required"] += list(r.get("required") or [])
            return merged
        for key in ("anyOf", "oneOf"):
            if key in schema:
                members = [m for m in schema[key] if self.resolve(m, depth + 1).get("type") != "null"]
                if len(members) == 1:
                    return self.resolve(members[0], depth + 1)
        return schema

    def types(self, schema: Any, depth: int = 0) -> set[str]:
        """The JSON types a schema admits: {"string", "null"}, {"object"}, {"any"}..."""
        if not isinstance(schema, dict) or depth > 40:
            return {"any"}
        if "$ref" in schema:
            return self.types(self.resolve(schema), depth + 1)
        for key in ("anyOf", "oneOf"):
            if key in schema:
                out: set[str] = set()
                for m in schema[key]:
                    out |= self.types(m, depth + 1)
                return out
        if "allOf" in schema:
            return {"object"}
        t = schema.get("type")
        if isinstance(t, list):
            return set(t)
        if isinstance(t, str):
            return {t}
        if "properties" in schema:
            return {"object"}
        if "enum" in schema:
            return {"enum"}
        return {"any"}

    def field(self, schema: Any, dotted: str) -> Any:
        """The schema of `a.b[].c` inside `schema`, MISSING when the schema describes its parent and lacks it,
        UNTYPED when a parent is an object the schema does not describe."""
        node = self.resolve(schema)
        raw: Any = schema
        for part in dotted.split("."):
            many = part.endswith("[]")
            name = part[:-2] if many else part
            if not node or "properties" not in node:
                return UNTYPED
            props = node.get("properties") or {}
            if name not in props:
                return MISSING
            raw = props[name]
            node = self.resolve(raw)
            if many:
                raw = node.get("items") or {}
                node = self.resolve(raw)
        # The leaf as written (an optional stays `anyOf [X, null]`), so a lost or gained null is a type change.
        return raw if isinstance(raw, dict) else node

    def body(self, op: dict[str, Any]) -> Any:
        content = ((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
        return content.get("schema")

    def answer(self, op: dict[str, Any]) -> Any:
        for code in sorted((op.get("responses") or {}).keys()):
            if str(code).startswith("2"):
                content = ((op["responses"][code] or {}).get("content") or {}).get("application/json") or {}
                return content.get("schema")
        return None

    def params(self, op: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        out = {}
        for p in op.get("parameters") or []:
            p = self.resolve(p) if "$ref" in p else p
            out[(p.get("in", ""), p.get("name", ""))] = p
        return out


def _narrower(spec_old: Spec, old: dict[str, Any], spec_new: Spec, new: dict[str, Any]) -> str | None:
    """Why `new` accepts less than `old` (for a value the client sends), or None."""
    ot, nt = spec_old.types(old), spec_new.types(new)
    if "number" in nt:
        nt.add("integer")  # a number field still takes the integer the client sends
    if "any" not in nt and not ot <= nt:
        return f"type {'|'.join(sorted(ot))} → {'|'.join(sorted(nt))}"
    o, n = spec_old.resolve(old), spec_new.resolve(new)
    if isinstance(n.get("maxLength"), int) and n["maxLength"] < (o.get("maxLength") or float("inf")):
        return f"maxLength {o.get('maxLength', 'none')} → {n['maxLength']}"
    if isinstance(n.get("minLength"), int) and n["minLength"] > (o.get("minLength") or 0):
        return f"minLength {o.get('minLength', 0)} → {n['minLength']}"
    if isinstance(n.get("enum"), list) and set(o.get("enum") or n["enum"]) - set(n["enum"]):
        return f"enum lost {sorted(set(o.get('enum') or []) - set(n['enum']))}"
    return None


def _wider(spec_old: Spec, old: dict[str, Any], spec_new: Spec, new: dict[str, Any]) -> str | None:
    """Why `new` may hold more than `old` (for a value the client reads), or None."""
    ot, nt = spec_old.types(old), spec_new.types(new)
    if "number" in ot:
        ot.add("integer")  # an integer is a number the client already reads
    if "any" not in ot and not nt <= ot:
        return f"type {'|'.join(sorted(ot))} → {'|'.join(sorted(nt))}"
    o, n = spec_old.resolve(old), spec_new.resolve(new)
    if isinstance(o.get("enum"), list) and isinstance(n.get("enum"), list) and set(n["enum"]) - set(o["enum"]):
        return f"enum gained {sorted(set(n['enum']) - set(o['enum']))}"
    return None


def _resource(path: str) -> str:
    parts = path.strip("/").split("/")
    return "/" + "/".join(parts[:2])


# ---------------- the comparison ----------------


def compare(
    old_doc: dict[str, Any], new_doc: dict[str, Any], uses: list[api_surface.Use] | None = None
) -> dict[str, Any]:
    old, new = Spec(old_doc), Spec(new_doc)
    uses = list(api_surface.USES if uses is None else uses)
    findings: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    warnings: list[str] = []

    def add(cls: str, u: api_surface.Use, kind: str, detail: str) -> None:
        findings.append(
            {"class": cls, "package": u.package, "method": u.method, "path": u.path, "kind": kind, "detail": detail}
        )

    for u in uses:
        key = (u.method, api_surface.normalise_path(u.path))
        o, n = old.ops.get(key), new.ops.get(key)
        if n is None:
            if o is not None:
                add("breaking", u, "operation_removed", f"{u.method} {u.path} is gone ({u.operation})")
            else:
                warnings.append(f"{u.package}: {u.method} {u.path} is in neither spec")
            continue
        o_op = o[1] if o else {}
        n_op = n[1]
        if n_op.get("deprecated") and not o_op.get("deprecated"):
            add("additive_relevant", u, "operation_deprecated", f"{u.method} {u.path} is now deprecated")

        # body
        ob, nb = old.body(o_op), new.body(n_op)
        if u.request:
            if nb is None and ob is not None:
                add("breaking", u, "request_body_removed", "the operation no longer takes a JSON body")
            elif nb is not None:
                for f in u.request:
                    of, nf = old.field(ob, f) if ob is not None else MISSING, new.field(nb, f)
                    if nf is UNTYPED:
                        continue
                    if nf is MISSING:
                        if of is not MISSING and of is not UNTYPED:
                            add(
                                "breaking", u, "request_field_removed", f"body field `{f}` is gone (removed or renamed)"
                            )
                        elif o is not None:
                            warnings.append(f"{u.package}: {u.method} {u.path} sends `{f}`, which neither spec defines")
                        continue
                    if isinstance(of, dict):
                        why = _narrower(old, of, new, nf)
                        if why:
                            add("breaking", u, "request_field_changed", f"body field `{f}`: {why}")
                # requiredness, top level and inside every object the client sends into
                for prefix in [""] + sorted({f.rsplit(".", 1)[0] for f in u.request if "." in f}):
                    if not prefix:
                        o_obj, n_obj = old.resolve(ob), new.resolve(nb)
                    else:
                        o_obj = old.field(ob, prefix) if ob is not None else {}
                        o_obj = old.resolve(o_obj) if isinstance(o_obj, dict) else {}
                        n_obj = new.field(nb, prefix)
                        n_obj = new.resolve(n_obj) if isinstance(n_obj, dict) else None
                    if not isinstance(n_obj, dict):
                        continue
                    o_req = set((o_obj.get("required") if isinstance(o_obj, dict) else None) or [])
                    n_req = set(n_obj.get("required") or [])
                    always = (
                        {f[len(prefix) + 1 :] for f in u.required if f.startswith(prefix + ".")}
                        if prefix
                        else set(u.required)
                    )
                    for r in sorted(n_req - o_req):
                        full = f"{prefix}.{r}" if prefix else r
                        if r not in always:
                            add(
                                "breaking",
                                u,
                                "request_field_required",
                                f"body field `{full}` is now required and is not always sent",
                            )
                if ob is not None:
                    o_props = set((old.resolve(ob).get("properties") or {}).keys())
                    for p in sorted(set((new.resolve(nb).get("properties") or {}).keys()) - o_props):
                        add("additive_relevant", u, "request_field_added", f"new optional body field `{p}`")

        # parameters
        op_, np_ = old.params(o_op), new.params(n_op)
        for name in u.query:
            pk = ("query", name)
            if pk not in np_:
                if pk in op_:
                    add("breaking", u, "parameter_removed", f"query parameter `{name}` is gone")
                continue
            if pk in op_:
                why = _narrower(old, op_[pk].get("schema") or {}, new, np_[pk].get("schema") or {})
                if why:
                    add("breaking", u, "parameter_changed", f"query parameter `{name}`: {why}")
        for pk, p in sorted(np_.items()):
            if pk[0] == "path" or pk in op_:
                if pk in op_ and p.get("required") and not op_[pk].get("required") and pk[1] not in u.query:
                    add("breaking", u, "parameter_required", f"{pk[0]} parameter `{pk[1]}` is now required")
                continue
            if p.get("required") and not (pk[0] == "query" and pk[1] in u.query):
                add("breaking", u, "parameter_required", f"new required {pk[0]} parameter `{pk[1]}`")
            else:
                add("additive_relevant", u, "parameter_added", f"new optional {pk[0]} parameter `{pk[1]}`")

        # answer
        oa, na = old.answer(o_op), new.answer(n_op)
        for f in u.response:
            nf = new.field(na, f) if na is not None else UNTYPED
            of = old.field(oa, f) if oa is not None else UNTYPED
            if nf is UNTYPED:
                unchecked.append({"package": u.package, "method": u.method, "path": u.path, "field": f})
                continue
            if nf is MISSING:
                if of is not MISSING:
                    add("breaking", u, "response_field_removed", f"answer field `{f}` is gone")
                continue
            if isinstance(of, dict):
                why = _wider(old, of, new, nf)
                if why:
                    add("breaking", u, "response_field_changed", f"answer field `{f}`: {why}")
        if na is not None and oa is not None:
            o_props = set((old.resolve(oa).get("properties") or {}).keys())
            n_props = set((new.resolve(na).get("properties") or {}).keys())
            for p in sorted(n_props - o_props):
                add("additive_relevant", u, "response_field_added", f"new answer field `{p}`")

    # new operations on resources a package wraps
    wrapped: dict[str, set[str]] = {}
    for u in uses:
        wrapped.setdefault(_resource(u.path), set()).add(u.package)
    used = {(u.method, api_surface.normalise_path(u.path)) for u in uses}
    for key in sorted(set(new.ops) - set(old.ops)):
        path = new.ops[key][0]
        for pkg in sorted(wrapped.get(_resource(path), ())):
            if key in used:
                continue
            findings.append(
                {
                    "class": "additive_relevant",
                    "package": pkg,
                    "method": key[0],
                    "path": path,
                    "kind": "operation_added",
                    "detail": f"new operation {key[0]} {path} on {_resource(path)}, which {pkg} wraps",
                }
            )

    # what changed that no package uses
    added = set(new.ops) - set(old.ops)
    removed = set(old.ops) - set(new.ops)
    changed = {k for k in set(old.ops) & set(new.ops) if old.ops[k][1] != new.ops[k][1]}
    touched = {(f["method"], api_surface.normalise_path(f["path"])) for f in findings}
    other = {
        "operations_added": len(added - touched),
        "operations_removed": len(removed - touched),
        "operations_changed": len(changed - touched),
        "schemas_changed": sorted(
            name
            for name in set((old_doc.get("components") or {}).get("schemas") or {})
            | set((new_doc.get("components") or {}).get("schemas") or {})
            if ((old_doc.get("components") or {}).get("schemas") or {}).get(name)
            != ((new_doc.get("components") or {}).get("schemas") or {}).get(name)
        ),
    }

    findings.sort(
        key=lambda f: (-CLASSES.index(f["class"]), f["package"], f["path"], f["method"], f["kind"], f["detail"])
    )
    packages: dict[str, dict[str, Any]] = {}
    for name in sorted(api_surface.PACKAGES):
        mine = [f for f in findings if f["package"] == name]
        cls = max((f["class"] for f in mine), key=CLASSES.index, default="none")
        packages[name] = {"classification": cls, "findings": len(mine)}
    overall = max((p["classification"] for p in packages.values()), key=CLASSES.index, default="none")
    return {
        "schema_version": 1,
        "classification": overall,
        "packages": packages,
        "affected_packages": sorted(n for n, p in packages.items() if p["classification"] != "none"),
        "findings": findings,
        "unchecked_response_fields": sorted(
            unchecked, key=lambda x: (x["package"], x["path"], x["method"], x["field"])
        ),
        "other_changes": other,
        "warnings": sorted(set(warnings)),
    }


def to_markdown(result: dict[str, Any], old_label: str, new_label: str) -> str:
    cls = result["classification"]
    lines = [f"## API compatibility {old_label} → {new_label}: `{cls}`", ""]
    if result["affected_packages"]:
        lines.append(
            "Packages affected: "
            + ", ".join(f"`{p}` ({result['packages'][p]['classification']})" for p in result["affected_packages"])
        )
    else:
        lines.append("No package uses anything that changed.")
    lines.append("")
    if result["findings"]:
        lines += ["| Class | Package | Operation | Change |", "|---|---|---|---|"]
        for f in result["findings"]:
            lines.append(f"| {f['class']} | {f['package']} | {f['method']} `{f['path']}` | {f['detail']} |")
        lines.append("")
    o = result["other_changes"]
    lines.append(
        f"Elsewhere in the API: {o['operations_added']} operation(s) added, {o['operations_removed']} removed, "
        f"{o['operations_changed']} changed; {len(o['schemas_changed'])} schema(s) changed."
    )
    n = len(result["unchecked_response_fields"])
    if n:
        lines.append(
            f"{n} response field(s) the packages read come from untyped answers and cannot be checked from the "
            "schema; the live contract check covers them."
        )
    for w in result["warnings"]:
        lines.append(f"- warning: {w}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Classify an API change against what the client packages use.")
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--old-label", default="old")
    ap.add_argument("--new-label", default="new")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--markdown", dest="md_out", default=None)
    args = ap.parse_args(argv)
    try:
        old_doc = json.loads(Path(args.old).read_text(encoding="utf-8"))
        new_doc = json.loads(Path(args.new).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"cannot read the specs: {e}", file=sys.stderr)
        return 2
    result = compare(old_doc, new_doc)
    result["old"], result["new"] = args.old_label, args.new_label
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    md = to_markdown(result, args.old_label, args.new_label)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    if args.md_out:
        Path(args.md_out).write_text(md, encoding="utf-8")
    if not args.json_out:
        sys.stdout.write(text)
    print(f"classification={result['classification']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
