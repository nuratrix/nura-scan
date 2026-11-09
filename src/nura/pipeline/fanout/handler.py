"""fanout: EventBridge domain schedule -> 1 SQS message per account -> Scanner.

One EventBridge rule per agent domain invokes this with {"domain": "<domain>"}.
Reads account_registry and queues {"account_id", "domain", "scan_id", "requested_at"}
for every account with state ACTIVE and scan_enabled true. The scan queue feeds the
Scanner with batch_size=1, so 1 message = 1 account = 1 scanner invocation.
"""
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SQS_BATCH = 10   # SendMessageBatch limit


def accounts_to_scan(table):
    kwargs = {"FilterExpression": Attr("state").eq("ACTIVE") & Attr("scan_enabled").eq(True),
              "ProjectionExpression": "account_id"}
    ids = []
    while True:
        page = table.scan(**kwargs)
        ids += [item["account_id"] for item in page["Items"]]
        if "LastEvaluatedKey" not in page:
            return sorted(ids)
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def queue_scans(domain, account_ids, sqs, queue_url, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    scan_id = f"{domain}#{now}"
    failed = []
    for i in range(0, len(account_ids), SQS_BATCH):
        batch = account_ids[i:i + SQS_BATCH]
        resp = sqs.send_message_batch(QueueUrl=queue_url, Entries=[
            {"Id": account_id,
             "MessageBody": json.dumps({"account_id": account_id, "domain": domain,
                                        "scan_id": scan_id, "requested_at": now})}
            for account_id in batch])
        failed += [f"{f['Id']}: {f.get('Message', f.get('Code'))}" for f in resp.get("Failed", [])]
    if failed:
        # Raising makes EventBridge retry the whole fan-out; the scanner's upserts
        # are idempotent, so accounts queued twice are just scanned twice.
        raise RuntimeError(f"{len(failed)} scan messages not queued: {failed}")
    return {"domain": domain, "scan_id": scan_id, "queued": len(account_ids)}


def lambda_handler(event, context):
    domain = event["domain"]
    table = boto3.resource("dynamodb").Table(os.environ["ACCOUNT_REGISTRY_TABLE"])
    account_ids = accounts_to_scan(table)
    result = queue_scans(domain, account_ids, boto3.client("sqs"), os.environ["SCAN_QUEUE_URL"])
    logger.info("fanout: %s", result)
    return result
