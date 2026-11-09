"""
Validate rule_sets/*.json.

JSON Schema covers shape and the operator vocabulary. This script adds the checks a
schema cannot express:
  - unreachable severity bands (an earlier band already covers a later one), which is
    how a boundary mistake silently changes severities
  - rule_id unique across all agents
  - the owning agent is listed first in `agents`
  - recommended_action values match conflict_rules.ACTIONS (no drift)

Usage: python rule_sets/validate_rule_sets.py      (requires: pip install jsonschema)
Exit code 1 on any error.
"""

import importlib.util
import json
import sys
from pathlib import Path

import jsonschema

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CONFLICT_RULES = REPO / "src" / "nura" / "agents" / "orchestrator" / "lambdas" / "conflict_rules.py"

LOWER = {"gt", "gte"}   # value above a threshold
UPPER = {"lt", "lte"}   # value below a threshold


def _covers(earlier, later):
    """True if every value matching `later` already matched `earlier`."""
    (eop, ex), (lop, lx) = earlier, later
    if eop in LOWER and lop in LOWER:
        return lx > ex if (eop == "gt" and lop == "gte") else lx >= ex
    if eop in UPPER and lop in UPPER:
        return lx < ex if (eop == "lt" and lop == "lte") else lx <= ex
    if eop in ("eq", "in") and lop in ("eq", "in"):
        evals = ex if eop == "in" else [ex]
        lvals = lx if lop == "in" else [lx]
        return all(v in evals for v in lvals)
    return False


def unreachable_bands(severity):
    errors, seen = [], []
    for i, band in enumerate(severity.get("bands", [])):
        metric = band.get("metric", severity.get("metric"))
        op = next(k for k in band if k not in ("metric", "level"))
        for j, (m, prev) in enumerate(seen):
            if m == metric and _covers(prev, (op, band[op])):
                errors.append(f"band {i} ({metric} {op} {band[op]}) is unreachable: band {j} already covers it")
        seen.append((metric, (op, band[op])))
    return errors


def load_conflict_actions():
    spec = importlib.util.spec_from_file_location("conflict_rules", CONFLICT_RULES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ACTIONS


def main():
    errors = []
    ruleset_schema = json.loads((HERE / "ruleset.schema.json").read_text(encoding="utf-8"))
    pricing_schema = json.loads((HERE / "pricing.schema.json").read_text(encoding="utf-8"))
    ruleset_validator = jsonschema.Draft202012Validator(ruleset_schema)
    actions = load_conflict_actions()
    schema_actions = set(ruleset_schema["$defs"]["rule"]["properties"]["recommended_action"]["enum"]) - {None}
    if schema_actions != actions:
        errors.append(f"recommended_action enum drifted from conflict_rules.ACTIONS: "
                      f"only in schema {sorted(schema_actions - actions)}, only in code {sorted(actions - schema_actions)}")

    for err in jsonschema.Draft202012Validator(pricing_schema).iter_errors(
            json.loads((HERE / "pricing.json").read_text(encoding="utf-8"))):
        errors.append(f"pricing.json: {err.json_path}: {err.message}")

    owners, rule_ids = {}, {}
    for path in sorted(HERE.glob("*.json")):
        if path.name.endswith(".schema.json") or path.name == "pricing.json":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for err in ruleset_validator.iter_errors(doc):
            errors.append(f"{path.name}: {err.json_path}: {err.message}")
        for rule in doc.get("rules", []):
            rid = rule.get("rule_id", "?")
            if rid in rule_ids:
                errors.append(f"{path.name}: {rid} already defined in {rule_ids[rid]}")
            rule_ids[rid] = path.name
            owner = owners.setdefault(path.name, rule["agents"][0])
            if rule["agents"][0] != owner:
                errors.append(f"{path.name}: {rid}: owning agent must be first in agents (expected {owner})")
            for msg in unreachable_bands(rule.get("severity", {})):
                errors.append(f"{path.name}: {rid}: {msg}")

    for e in errors:
        print("ERROR", e)
    print(f"{len(rule_ids)} rules checked, {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
