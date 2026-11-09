# infra

| File | What it deploys |
|---|---|
| `app.py`, `governance_stack.py`, `cdk.json` | CDK stack `NuraGovernance` in the governance account: Findings and AccountRegistry tables, account_sync, Fanout, scan queue + DLQ, Scanner |
| `member-scanner-role.yaml`, `deploy_scanner_role.ps1` | `governance-platform-scanner` in every member account, via the service-managed StackSet `nura-member-scanner-role` (run from the management account). Trusted only by the scanner Lambda |
| `import-tables.json` | Resource mapping for adopting existing tables (below) |

Run CDK commands from this folder. `cdk diff` before every `cdk deploy`.

## Ownership of the Findings table

Nura-Deterministic owns `Findings`: this stack defines it, the scanner writes it.
Nura-Judgment only reads it and consumes its stream, and must deploy with its
foundation stack reusing the existing table, never creating one.

## Member account roles: one per project

| Role | Owner | Assumed by | Grants |
|---|---|---|---|
| `governance-platform-scanner` | Nura-Deterministic (this folder) | `NuraGovernance-Scanner*` Lambda role | What the checkers read: SecurityAudit, ViewOnlyAccess, CloudWatch metrics, S3 bucket configuration |
| `governance-platform-reader` | Nura-Judgment (its `deploy/member-reader-role.yaml`) | `nura-judgment-runtime` | Exactly the agents' tool reads |

Each project deploys and changes its own role. Cost Explorer and the Price List API are
called from the governance account with the scanner's own role, so the member role
does not grant them.

## Adopting existing tables

Both tables use `RemovalPolicy.RETAIN`, so a destroyed `NuraGovernance` stack leaves them
behind, and a fresh `cdk deploy` then fails because the names are taken. Adopt them
instead of recreating them (data is untouched):

```powershell
# 1. Stack containing only the two tables, imported (CloudFormation cannot create
#    other resources in the same operation)
cdk import -c import_tables_only=true --version-reporting false `
    --resource-mapping import-tables.json

# 2. Everything else, added to the now-existing stack
cdk diff
cdk deploy
```

The logical IDs in `import-tables.json` come from `cdk synth -c import_tables_only=true`.
They change only if the construct IDs `Findings` / `AccountRegistry` are renamed.
