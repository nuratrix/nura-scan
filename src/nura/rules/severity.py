OPS = {
    "lt":  lambda v, t: v <  t,
    "lte": lambda v, t: v <= t,
    "gt":  lambda v, t: v >  t,
    "gte": lambda v, t: v >= t,
}

def evaluate(severity_spec: dict, evidence: dict) -> str:
    metric = severity_spec.get("metric")
    if metric is not None:
        value = evidence[metric]               # KeyError = bug, fail loudly
        for band in severity_spec.get("bands", []):
            op, threshold = next((k, band[k]) for k in OPS if k in band)
            if OPS[op](value, threshold):
                return band["level"]
    return severity_spec["default"]