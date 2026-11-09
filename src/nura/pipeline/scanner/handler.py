import json
import logging
import os
from dataclasses import dataclass, field

import boto3

from nura.rules.loader import load_ruleset
from nura.rules.finding_builder import build_finding
from nura.checkers.registry import CHECKERS, load_checkers
from nura.common.findings_repo import put_findings
from nura.common.session import get_or_assume_session, get_scan_regions

logger = logging.getLogger()
logger.setLevel(logging.INFO)


@dataclass
class ScanContext:
    session: boto3.Session              # member account being scanned
    governance_session: boto3.Session   # Cost Explorer lives in the governance account
    account_id: str
    region: str
    primary_region: str                 # account-wide checks run only here
    cache: dict = field(default_factory=dict)   # shared across rules/regions for one account


def run_checks(ctx, domain):
    load_checkers(domain)                  # registers @checker functions for this domain
    findings, errors = [], []
    for rule_id, rule in load_ruleset(domain).items():
        fn = CHECKERS.get(rule_id)
        if fn is None:
            continue                       # not built yet
        try:
            for res_id, evidence, saving in fn(ctx, rule):
                f = build_finding(rule, ctx.account_id, ctx.region, res_id, evidence, saving)
                if f:
                    findings.append(f)
        except Exception as e:
            errors.append({"rule_id": rule_id, "region": ctx.region,
                           "error": f"{type(e).__name__}: {e}"})
    return findings, errors


def scan_account(session, account_id, domain, regions, governance_session=None):
    cache = {}
    findings, errors = [], []
    for region in regions:
        ctx = ScanContext(session=session, governance_session=governance_session or session,
                          account_id=account_id, region=region,
                          primary_region=regions[0], cache=cache)
        f, e = run_checks(ctx, domain)
        findings += f
        errors += [{"account_id": account_id, **err} for err in e]
    return findings, errors


def scan_accounts(account_ids=None, domain="cost", regions=None):
    """Scan each member account from the governance account.

    account_ids: member accounts; default SCAN_ACCOUNTS env var (comma-separated),
                 else just the governance account itself.
    regions:     default SCAN_REGIONS env var (see get_scan_regions).
    """
    governance = boto3.Session()
    governance_id = governance.client("sts").get_caller_identity()["Account"]
    if not account_ids:
        configured = os.environ.get("SCAN_ACCOUNTS", "")
        account_ids = [a.strip() for a in configured.split(",") if a.strip()] or [governance_id]
    regions = regions or get_scan_regions(governance)

    findings, errors = [], []
    for account_id in account_ids:
        try:
            # Governance account: own credentials. Members: assume governance-platform-scanner.
            session = get_or_assume_session(account_id, governance_id)
        except Exception as e:                     # one bad account must not stop the rest
            errors.append({"account_id": account_id, "rule_id": None, "region": None,
                           "error": f"assume role failed: {type(e).__name__}: {e}"})
            continue
        f, e = scan_account(session, account_id, domain, regions, governance_session=governance)
        findings += f
        errors += e
    return findings, errors


def _scan_and_save(account_ids, domain, regions=None):
    findings, errors = scan_accounts(account_ids, domain, regions)
    put_findings(findings)          # async invocations discard the return value
    for err in errors:
        logger.warning("scan error: %s", err)
    return findings, errors


def lambda_handler(event, context):
    """Two entry points:

    SQS (from fanout, batch_size=1): {"Records": [{"body": '{"account_id", "domain", ...}'}]}
        An account that cannot be reached (assume-role failed) is reported as a batch
        item failure, so SQS retries it and then parks it in the DLQ. Failed rules are
        only logged: retrying the whole account would hit the same broken rule.
    Direct: {"account_ids": [...], "domain": "cost", "regions": [...]}, all optional.
    """
    event = event or {}
    if "Records" in event:
        failures = []
        for record in event["Records"]:
            msg = json.loads(record["body"])
            try:
                _, errors = _scan_and_save([msg["account_id"]], msg["domain"])
                if any(e["rule_id"] is None for e in errors):
                    raise RuntimeError(f"account {msg['account_id']} unreachable")
            except Exception:
                logger.exception("scan failed: %s", msg)
                failures.append({"itemIdentifier": record["messageId"]})
        return {"batchItemFailures": failures}

    findings, errors = _scan_and_save(event.get("account_ids"), event.get("domain", "cost"),
                                      event.get("regions"))
    return {"finding_count": len(findings), "error_count": len(errors), "errors": errors}


if __name__ == "__main__":
    findings, errors = scan_accounts()
    if os.environ.get("FINDINGS_TABLE"):   # local runs only print unless a table is set
        put_findings(findings)
        print(f"saved to {os.environ['FINDINGS_TABLE']}")
    print(f"{len(findings)} findings")
    for f in findings:
        print(f.severity, f.finding_id, "-", f.recommendation)
    print("errors:", errors)
