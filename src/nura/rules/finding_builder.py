from nura.common.models import Finding
from nura.rules.severity import evaluate

def build_finding(rule, account_id, region, resource_id, evidence, saving=None):
    if saving is not None and saving < rule.get("min_saving_usd", 0):
        return None                                   # suppressed
    values = {**rule["params"], **evidence, "resource_id": resource_id,
              "estimated_monthly_saving": round(saving, 2) if saving else 0}
    return Finding(
        rule_id=rule["rule_id"], domain=rule["domain"],
        account_id=account_id, region=region,
        resource_id=resource_id, resource_type=rule["resource_type"],
        severity=evaluate(rule["severity"], evidence),
        title=rule["title"],
        recommendation=rule["recommendation"].format_map(values),
        evidence=evidence, estimated_monthly_saving=saving)