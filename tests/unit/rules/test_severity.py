# tests/unit/rules/test_severity.py
from nura.rules.severity import evaluate

SPEC = {"metric": "days_stopped",
        "bands": [{"gt": 30, "level": "HIGH"}, {"gt": 7, "level": "MEDIUM"}],
        "default": "LOW"}

def test_bands():
    assert evaluate(SPEC, {"days_stopped": 31}) == "HIGH"
    assert evaluate(SPEC, {"days_stopped": 30}) == "MEDIUM"   # boundary
    assert evaluate(SPEC, {"days_stopped": 7})  == "LOW"      # boundary