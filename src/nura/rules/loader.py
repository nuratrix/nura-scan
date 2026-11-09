import json
import os
from pathlib import Path

# Lambda: rulesets layer at /opt (NURA_RULESET_DIR). Locally: <repo>/rulesets.
RULESET_DIR = Path(os.environ.get("NURA_RULESET_DIR")
                   or Path(__file__).resolve().parents[3] / "rulesets")

def load_ruleset(domain: str) -> dict:
    data = json.loads((RULESET_DIR / f"{domain}.json").read_text())
    return {r["rule_id"]: r for r in data["rules"] if r.get("enabled", True)}