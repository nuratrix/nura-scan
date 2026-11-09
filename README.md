# Nura: Deterministic Scan

Nura is a multi-account AWS governance platform. It continuously checks every account in an AWS Organization across nine domains: security, infrastructure, compliance, cost, cost anomaly, incident, network security, access analyzer, and backup/DR. It then decides, with evidence, what should be done about each problem it finds.

This repository is the **deterministic scan**. It finds problems and stores them as findings. There is no model anywhere in this project: every finding exists because a checker measured something and a versioned rule said it matters.

## Deterministic vs. judgment: two projects

Nura is split into two projects on purpose:

| | This project (deterministic) | Judgment project |
|---|---|---|
| Job | Find problems | Decide what to do about them |
| How | Checkers (plain code) run on a schedule against every member account | Rules route each finding to AI agents; code resolves their opinions |
| Uses a model? | **Never** | Only for HIGH and CRITICAL findings |
| Output | Findings in the `Findings` DynamoDB table | Decisions in the decision log (S3) and on SNS |

The two projects meet only at the `Findings` table. This project owns it and writes to it. The judgment project reads it and reacts to its change stream. Keeping them apart means:

- **Detection is never affected by a model.** The same account, scanned with the same rulesets, always produces the same findings.
- **Scanning costs no tokens.** Every rule runs on every account, every cycle, without any per-finding model cost.
- **Each side can be deployed, tested and changed on its own.** The scanner runs without the judgment layer; findings simply accumulate in the table.

## Architecture

![Nura deterministic scan pipeline](Nura%20%E2%80%94%20Deterministic%20Scan.png)

A scan moves through these steps:

1. **account_sync Lambda** (hourly). Calls AWS Organizations `ListAccounts` and upserts every account into the `AccountRegistry` table: name, email, state, and whether it is the management account.
   - Each account gets a `scan_enabled` flag on its first sync. Later syncs never overwrite it, so an operator can switch scanning off for an account and it stays off.
   - The management account starts with `scan_enabled = false`, because service-managed StackSets don't deploy the scanner role there.
   - Accounts that leave the organization are marked `REMOVED`, not deleted.
2. **EventBridge: 9 scheduled rules,** one per domain, staggered hourly from 01:00 to 09:00 UTC. Each sends `{"domain": "<domain>"}` to the Fanout Lambda.
3. **Fanout Lambda.** Reads the registry and sends **one SQS message per account** that is `ACTIVE` with `scan_enabled = true`.
4. **Scan queue** (SQS). Feeds the Scanner with `batch_size = 1`, so **1 message = 1 account = 1 scanner invocation**. Maximum concurrency (default 10) keeps the Lambda inside its account limit. After 3 failed attempts, a message is parked in the **dead-letter queue** (kept 14 days).
5. **Scanner Lambda.** For one account:
   - assumes `governance-platform-scanner` in the member account (read-only, external ID);
   - loads the domain's ruleset and runs every rule that has a checker, in each scan region;
   - turns each result into a finding: severity from the rule's bands, recommendation from the rule's template. Results below the rule's `min_saving_usd` are suppressed;
   - upserts the findings into the `Findings` table.
6. **Findings table** (DynamoDB). Key `finding_id = account#region#rule_id#resource_id`, so a rescan updates the same item instead of adding a duplicate. Its change stream (new and old images) is what the judgment layer consumes.

The diagram shows the target design. Differences from what deploys today:

- Rulesets ship with the Scanner as a **Lambda layer** (mounted at `/opt`), not from S3. Changing a rule means redeploying the stack.
- The registry table is named `AccountRegistry`, not `account_registry`.
- Only the **cost** domain has checkers so far. The other eight domains are scheduled and load their rulesets, but produce no findings yet. See [Status](#status).

## Design principles

### Deterministic: code and rules decide, nothing guesses
- **Checkers measure; rules judge.** A checker only reports facts about a resource (for example, average CPU over 14 days). The ruleset decides whether that is a finding, how severe it is and what to recommend.
- **Severity is data, not code.** Each rule has a `default` severity and optional bands on one evidence metric, evaluated in order. For example, `COST-EC2-001` is `HIGH` when `avg_cpu_pct < 1`, otherwise `MEDIUM` ([`severity.py`](src/nura/rules/severity.py)).
- **Thresholds live in the ruleset.** Lookback windows, CPU thresholds and saving fractions are rule `params`, so tuning a rule never changes code.
- **Prices come from AWS.** Savings are calculated from the AWS Price List API for the scanned region, with a static fallback in [`rulesets/pricing/`](rulesets/pricing/) ([`ce.py`](src/nura/checkers/cost/ce.py)).

### Idempotent: scanning twice changes nothing
- **One item per problem.** A rescan overwrites the same `finding_id`. `first_seen_at` is kept from the first scan, while `last_seen_at` and the 90-day expiry move forward each time.
- **Safe retries everywhere.** Fanout retries resend messages, and SQS retries rescan accounts. Both only repeat the same upserts.
- **Resolved problems age out.** A finding that is no longer detected is not deleted. It expires 90 days after it was last seen, so readers filter on `last_seen_at` for "current".

### Least privilege: read-only, one role per job
- **The scanner can only read.** In member accounts it uses `governance-platform-scanner`: `SecurityAudit`, `ViewOnlyAccess`, CloudWatch metrics and S3 bucket configuration. It never reads data contents such as objects, items or secrets.
- **One role per project.** Only the Scanner Lambda's role can assume the scanner role, and only with the external ID. The judgment agents use their own role, `governance-platform-reader`, owned by the judgment project.
- **Account-wide data stays central.** Cost Explorer and the Price List API are called from the governance account, so member accounts grant neither.

### Resilient: one failure never hides the rest
- **A broken rule doesn't stop the scan.** Its error is logged with the account, region and rule, and every other rule still runs. Retrying the account wouldn't help, because the rule would fail the same way.
- **An unreachable account is retried, then parked.** If the scanner role can't be assumed, the message is retried 3 times and then moved to the DLQ, where it waits for a fix and a redrive.
- **New accounts join by themselves.** A new account appears in the registry within an hour, and the StackSet deploys its scanner role automatically.

## Rulesets and checkers

Each domain has a ruleset in [`rulesets/<domain>.json`](rulesets/), with 209 rules in total. A rule looks like this:

```json
{
  "rule_id": "COST-EC2-001",
  "domain": "cost",
  "enabled": true,
  "title": "Idle EC2 instance",
  "params": { "cpu_threshold_pct": 5, "lookback_days": 14, "saving_fraction": 0.9 },
  "severity": { "metric": "avg_cpu_pct", "bands": [{ "lt": 1, "level": "HIGH" }], "default": "MEDIUM" },
  "min_saving_usd": 0,
  "recommendation": "Stop or downsize {resource_id}: avg CPU {avg_cpu_pct}% over {lookback_days} days. Estimated saving: ${estimated_monthly_saving}/month.",
  "recommended_action": "STOP",
  "remediation": null,
  "agents": ["cost-agent"],
  "resource_type": "AWS::EC2::Instance"
}
```

`recommended_action`, `remediation` and `agents` are not used here. They are for the judgment layer, which routes HIGH and CRITICAL findings to the listed agents. The rulesets are shared between the two projects.

A checker is a generator registered under its rule ID. It yields `(resource_id, evidence, monthly_saving)` for each resource that matches:

```python
@checker("COST-EC2-004")
def unused_elastic_ips(ctx, rule):
    for addr in _ec2(ctx).describe_addresses()["Addresses"]:
        if "AssociationId" not in addr:
            yield addr["AllocationId"], {"public_ip": addr["PublicIp"]}, ...
```

- Checkers live in `src/nura/checkers/<domain>/`. Every module there is imported automatically when its domain is scanned, so a new file needs no registration.
- `ctx` carries the member account's `session`, the `region`, the `account_id`, a governance-account session for Cost Explorer, and a `cache` shared by the rules of one account scan.
- A rule with no checker yet is skipped.

| Domain | Rules | Checkers built |
|---|---|---|
| cost | 18 | 18: EC2, EBS, Elastic IP, RDS, S3, reservations and Savings Plans |
| security | 79 | 0 |
| compliance | 50 | 0 |
| infrastructure | 18 | 0 |
| network-security | 14 | 0 |
| incident | 9 | 0 |
| backup-dr | 9 | 0 |
| cost-anomaly | 7 | 0 |
| access-analyzer | 5 | 0 |

## Repository layout

```
src/nura/
  checkers/       one package per domain (only cost/ has checkers); registry.py: @checker + auto-discovery
  rules/          loader.py (rulesets), severity.py (bands), finding_builder.py (rule + evidence -> Finding)
  pipeline/
    account_sync/ hourly Organizations -> AccountRegistry
    fanout/       domain schedule -> 1 SQS message per account
    scanner/      SQS -> run_checks() for one account -> Findings
  common/         models.py (Finding), findings_repo.py (DynamoDB), session.py (cross-account), utils.py
rulesets/         rule catalog per domain + pricing/ fallback prices
infra/            CDK stack NuraGovernance, member scanner role StackSet (see infra/README.md)
tests/unit/       moto-based tests: findings repo, account_sync -> fanout -> scanner, severity
```

## Deploy

### What gets created

`cdk deploy` creates the stack `NuraGovernance` in the governance account:

| Resource | Name | Notes |
|---|---|---|
| Findings table | `Findings` | DynamoDB, on-demand. Stream (new and old images), 90-day TTL on `expires_at`, point-in-time recovery, indexes `by-account`, `by-severity`, `by-resource`. Kept if the stack is deleted |
| Account registry | `AccountRegistry` | DynamoDB, on-demand, point-in-time recovery. Kept if the stack is deleted |
| account_sync Lambda | `NuraGovernance-AccountSync…` | Hourly EventBridge rule |
| Fanout Lambda | `NuraGovernance-Fanout…` | 9 daily EventBridge rules, one per domain |
| Scan queue and DLQ | `NuraGovernance-ScanQueue…`, `…ScanDLQ…` | SQS-managed encryption, TLS required, 3 attempts before the DLQ |
| Scanner Lambda | `NuraGovernance-Scanner…` | 15-minute timeout, rulesets layer, max concurrency 10 |

`infra/deploy_scanner_role.ps1` deploys the StackSet `nura-member-scanner-role`, which creates `governance-platform-scanner` in every member account.

### Prerequisites

- Credentials for the governance account with rights to create IAM roles, CloudFormation stacks, Lambda, SQS, DynamoDB and EventBridge resources.
- For account_sync, the governance account must be the management account or a delegated administrator for AWS Organizations. Otherwise, pass a role in the management account: `cdk deploy -c org_read_role_arn=arn:aws:iam::<mgmt>:role/<role>`.
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [uv](https://docs.astral.sh/uv/), Node.js 22 or 24, and the CDK CLI (`npm install -g aws-cdk`).
- Python dependencies: `uv sync --extra infra`.
- The CDK bootstrap, once per account and region: `cdk bootstrap aws://<account>/<region>`.

### 1. Deploy the stack

From `infra/`:
```powershell
cdk diff
cdk deploy
```

| Context (`-c key=value`) | Default | Purpose |
|---|---|---|
| `org_read_role_arn` | none | Role in the management account for `ListAccounts`, if needed (see Prerequisites) |
| `scan_regions` | stack region | `all`, or a list such as `us-east-1,eu-west-1` |
| `scanner_max_concurrency` | `10` | Scanner Lambdas running at once |
| `import_tables_only` | off | Adopting existing tables (below) |

**If the tables already exist.** Both tables are kept when the stack is deleted, so a later `cdk deploy` fails because the names are taken. Adopt them instead of recreating them; their data is untouched:
```powershell
cdk import -c import_tables_only=true --version-reporting false --resource-mapping import-tables.json
cdk deploy
```

### 2. Deploy the scanner role to member accounts

From the repository root, with management-account credentials:
```powershell
.\infra\deploy_scanner_role.ps1
```
It targets the organization root, so every current account and any account added later gets the role. The management account is never targeted, which is why it starts with `scan_enabled = false`.

### 3. The judgment layer

The judgment project reads the `Findings` table. Deploy its foundation stack so that it **reuses** this table, never creating its own. Use the stack output `FindingsTableStreamArn` for its triage trigger.

## Run locally

```powershell
uv sync --extra infra
uv run pytest                                                   # unit tests, no AWS calls

# Scan with your own credentials and print the findings (nothing is written)
uv run python -m nura.pipeline.scanner.handler

# Choose accounts and regions; also save to the table
$env:SCAN_ACCOUNTS = "111111111111,222222222222"
$env:SCAN_REGIONS  = "us-east-1,eu-west-1"
$env:FINDINGS_TABLE = "Findings"
uv run python -m nura.pipeline.scanner.handler
```

Scanning a member account locally requires the scanner role to trust your own principal. Deploy it with the `AdditionalPrincipalArn` parameter of [`member-scanner-role.yaml`](infra/member-scanner-role.yaml) set to your IAM user or role, and leave that parameter empty in production.

## Operations

- **Run a scan now** without waiting for the schedule:
  ```powershell
  aws lambda invoke --function-name <AccountSyncFunction> out.json
  aws lambda invoke --function-name <FanoutFunction> --cli-binary-format raw-in-base64-out --payload '{\"domain\":\"cost\"}' out.json
  ```
  Function names are in the stack outputs.
- **Watch it:** `aws logs tail /aws/lambda/<ScannerFunction> --follow`. Rule errors appear as `scan error:` lines with the account, region and rule ID.
- **Failed accounts:** check the DLQ (`ScanDLQUrl` output). The usual cause is a member account without the scanner role. Once fixed, use the SQS console's *Start DLQ redrive*.
- **Stop scanning one account:**
  ```powershell
  aws dynamodb update-item --table-name AccountRegistry --key '{\"account_id\": {\"S\": \"<id>\"}}' `
    --update-expression "SET scan_enabled = :f" --expression-attribute-values '{\":f\": {\"BOOL\": false}}'
  ```
- **Cost:** no model, so spend scales with accounts × domains × regions in Lambda time and AWS API calls. DynamoDB writes are one upsert per finding per scan.

## Clean up

```powershell
cd infra
cdk destroy                                           # Lambdas, queues, rules, roles
aws cloudformation delete-stack-instances --stack-set-name nura-member-scanner-role `
  --deployment-targets OrganizationalUnitIds=<root-id> --regions us-east-1 --no-retain-stacks
aws cloudformation delete-stack-set --stack-set-name nura-member-scanner-role   # after the instances are gone
```

`Findings` and `AccountRegistry` are **kept** when the stack is destroyed. Delete them yourself if you no longer need them. If the judgment layer is still deployed, it depends on `Findings`.

## Status

| Area | State |
|---|---|
| account_sync, Fanout, scan queue and DLQ, Scanner, Findings and AccountRegistry tables | Built |
| Cost checkers (18 rules), live pricing from the AWS Price List API | Built |
| Checkers for the other eight domains (191 rules) | Planned. Their schedules run and load the rulesets, but produce no findings |
| Member scanner role (StackSet) | Built |
| Rulesets from S3 (diagram) | Planned. Today they ship as a Lambda layer |
| Ruleset validator (`rulesets/validate_rule_sets.py`) | Broken since the split: it expects the schemas next to it (they are in `rulesets/schema/`) and imports the judgment project's conflict rules |
| Checker tests, ruleset/registry contract test | Planned (`tests/unit/checkers/` and `tests/contract/` are empty) |
| `ruleset_version` on findings | Always `local`; the ruleset's version is not stamped yet |
| Scanner IAM in the governance account | Uses the AWS-managed `ReadOnlyAccess`; to be narrowed once more domains are built |
| CloudWatch alarms (DLQ depth, scanner errors) | Planned |
