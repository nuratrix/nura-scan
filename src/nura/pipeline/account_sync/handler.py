"""account_sync: hourly EventBridge -> Organizations ListAccounts -> upsert account_registry.

Runs in the governance account. Organizations is called directly, which works when
this is the management account or the delegated admin for Organizations. Otherwise set
ORG_READ_ROLE_ARN to a role in the management account allowing ListAccounts and
DescribeOrganization.

Registry item (key account_id):
  name, email, state      from Organizations (ACTIVE | SUSPENDED | PENDING_CLOSURE | CLOSED,
                          or REMOVED once the account leaves the organization)
  is_management           the org's management account
  scan_enabled            set once on first sync, never overwritten: operators may flip it.
                          Defaults to False for the management account, because the
                          SERVICE_MANAGED StackSet does not deploy the scanner role there.
  joined_at, first_synced_at, last_synced_at
"""
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _org_client():
    role_arn = os.environ.get("ORG_READ_ROLE_ARN")
    if not role_arn:
        return boto3.client("organizations")
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn, RoleSessionName="nura-account-sync")["Credentials"]
    return boto3.client("organizations",
                        aws_access_key_id=creds["AccessKeyId"],
                        aws_secret_access_key=creds["SecretAccessKey"],
                        aws_session_token=creds["SessionToken"])


def _registry_table():
    return boto3.resource("dynamodb").Table(os.environ["ACCOUNT_REGISTRY_TABLE"])


def _upsert(table, account, management_id, now):
    is_management = account["Id"] == management_id
    joined = account.get("JoinedTimestamp")
    table.update_item(
        Key={"account_id": account["Id"]},
        UpdateExpression=(
            "SET #name = :name, #email = :email, #state = :state, #is_management = :mgmt,"
            " #joined_at = :joined, #last_synced_at = :now,"
            " #first_synced_at = if_not_exists(#first_synced_at, :now),"
            " #scan_enabled = if_not_exists(#scan_enabled, :enabled)"
            " REMOVE #removed_at"),
        ExpressionAttributeNames={f"#{k}": k for k in [
            "name", "email", "state", "is_management", "joined_at", "last_synced_at",
            "first_synced_at", "scan_enabled", "removed_at"]},
        ExpressionAttributeValues={
            ":name": account.get("Name", ""),
            ":email": account.get("Email", ""),
            # "State" replaced the deprecated "Status" field; accept either.
            ":state": account.get("State") or account.get("Status", "UNKNOWN"),
            ":mgmt": is_management,
            ":joined": joined.isoformat() if joined else None,
            ":now": now,
            ":enabled": not is_management,
        })


def _mark_removed(table, seen, now):
    """Accounts in the registry that are no longer in the organization."""
    removed = 0
    kwargs = {"ProjectionExpression": "account_id, #state",
              "ExpressionAttributeNames": {"#state": "state"}}
    while True:
        page = table.scan(**kwargs)
        for item in page["Items"]:
            if item["account_id"] in seen or item.get("state") == "REMOVED":
                continue
            table.update_item(
                Key={"account_id": item["account_id"]},
                UpdateExpression="SET #state = :removed, #removed_at = :now",
                ExpressionAttributeNames={"#state": "state", "#removed_at": "removed_at"},
                ExpressionAttributeValues={":removed": "REMOVED", ":now": now})
            removed += 1
        if "LastEvaluatedKey" not in page:
            return removed
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def sync_accounts(org=None, table=None):
    org = org or _org_client()
    table = table or _registry_table()
    now = datetime.now(timezone.utc).isoformat()
    management_id = org.describe_organization()["Organization"]["MasterAccountId"]

    seen = set()
    for page in org.get_paginator("list_accounts").paginate():
        for account in page["Accounts"]:
            _upsert(table, account, management_id, now)
            seen.add(account["Id"])
    removed = _mark_removed(table, seen, now)
    return {"synced": len(seen), "removed": removed, "management_account_id": management_id}


def lambda_handler(event, context):
    result = sync_accounts()
    logger.info("account_sync: %s", result)
    return result
