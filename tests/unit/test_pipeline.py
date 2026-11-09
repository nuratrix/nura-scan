"""account_sync -> fanout -> scanner (SQS entry point), against moto."""
import json

import boto3
import pytest
from moto import mock_aws

from nura.pipeline.account_sync import handler as account_sync
from nura.pipeline.fanout import handler as fanout
from nura.pipeline.scanner import handler as scanner


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    for k in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]:
        monkeypatch.setenv(k, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        org = boto3.client("organizations")
        org.create_organization(FeatureSet="ALL")
        members = [org.create_account(AccountName=f"member-{i}", Email=f"m{i}@example.com")
                   ["CreateAccountStatus"]["AccountId"] for i in range(2)]
        table = boto3.resource("dynamodb").create_table(
            TableName="AccountRegistry", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "account_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "account_id", "AttributeType": "S"}])
        queue_url = boto3.client("sqs").create_queue(QueueName="scan")["QueueUrl"]
        monkeypatch.setenv("ACCOUNT_REGISTRY_TABLE", "AccountRegistry")
        monkeypatch.setenv("SCAN_QUEUE_URL", queue_url)
        mgmt = org.describe_organization()["Organization"]["MasterAccountId"]
        yield {"org": org, "table": table, "queue_url": queue_url,
               "members": members, "management": mgmt}


def test_sync_upserts_accounts_and_disables_management(aws):
    result = account_sync.lambda_handler({}, None)
    assert result["synced"] == 3 and result["removed"] == 0

    mgmt = aws["table"].get_item(Key={"account_id": aws["management"]})["Item"]
    assert mgmt["is_management"] is True and mgmt["scan_enabled"] is False
    member = aws["table"].get_item(Key={"account_id": aws["members"][0]})["Item"]
    assert member["state"] == "ACTIVE" and member["scan_enabled"] is True
    assert member["name"] == "member-0"


def test_sync_keeps_operator_scan_flag_and_marks_removed(aws):
    account_sync.lambda_handler({}, None)
    first = aws["members"][0]
    first_synced = aws["table"].get_item(Key={"account_id": first})["Item"]["first_synced_at"]
    aws["table"].update_item(Key={"account_id": first},
                             UpdateExpression="SET scan_enabled = :f",
                             ExpressionAttributeValues={":f": False})
    aws["table"].put_item(Item={"account_id": "999999999999", "state": "ACTIVE"})  # left the org

    result = account_sync.lambda_handler({}, None)

    item = aws["table"].get_item(Key={"account_id": first})["Item"]
    assert item["scan_enabled"] is False                 # operator's choice survives a sync
    assert item["first_synced_at"] == first_synced
    assert result["removed"] == 1
    gone = aws["table"].get_item(Key={"account_id": "999999999999"})["Item"]
    assert gone["state"] == "REMOVED" and "removed_at" in gone


def test_fanout_queues_one_message_per_scannable_account(aws):
    account_sync.lambda_handler({}, None)
    result = fanout.lambda_handler({"domain": "cost"}, None)
    assert result["queued"] == 2                         # management account excluded

    sqs = boto3.client("sqs")
    bodies = []
    while True:
        msgs = sqs.receive_message(QueueUrl=aws["queue_url"], MaxNumberOfMessages=10).get("Messages", [])
        if not msgs:
            break
        bodies += [json.loads(m["Body"]) for m in msgs]
        for m in msgs:
            sqs.delete_message(QueueUrl=aws["queue_url"], ReceiptHandle=m["ReceiptHandle"])
    assert sorted(b["account_id"] for b in bodies) == sorted(aws["members"])
    assert {b["domain"] for b in bodies} == {"cost"}
    assert len({b["scan_id"] for b in bodies}) == 1


def test_fanout_batches_beyond_ten(aws):
    for i in range(23):
        aws["table"].put_item(Item={"account_id": f"{i:012d}", "state": "ACTIVE", "scan_enabled": True})
    assert fanout.lambda_handler({"domain": "security"}, None)["queued"] == 23


def _sqs_event(*bodies):
    return {"Records": [{"messageId": f"m{i}", "body": json.dumps(b)} for i, b in enumerate(bodies)]}


def test_scanner_reports_unreachable_account_for_retry(monkeypatch):
    def fake_scan(account_ids, domain, regions=None):
        if account_ids == ["222222222222"]:
            return [], [{"account_id": "222222222222", "rule_id": None, "error": "assume role failed"}]
        return [], [{"account_id": account_ids[0], "rule_id": "COST-EC2-001", "error": "boom"}]
    monkeypatch.setattr(scanner, "scan_accounts", fake_scan)
    monkeypatch.setattr(scanner, "put_findings", lambda f: len(f))

    ok = scanner.lambda_handler(_sqs_event({"account_id": "111111111111", "domain": "cost"}), None)
    assert ok == {"batchItemFailures": []}               # rule errors are logged, not retried

    bad = scanner.lambda_handler(_sqs_event({"account_id": "222222222222", "domain": "cost"}), None)
    assert bad == {"batchItemFailures": [{"itemIdentifier": "m0"}]}
