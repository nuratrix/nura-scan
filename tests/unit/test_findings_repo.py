import boto3
import pytest
from moto import mock_aws

from nura.common import findings_repo
from nura.common.models import Finding


def _key(name):
    return [{"AttributeName": name, "KeyType": "HASH"},
            {"AttributeName": "last_seen_at", "KeyType": "RANGE"}]


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("FINDINGS_TABLE", "findings")
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="findings", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "finding_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in
                                  ["finding_id", "account_id", "severity", "resource_id", "last_seen_at"]],
            GlobalSecondaryIndexes=[
                {"IndexName": f"by-{n}", "KeySchema": _key(a), "Projection": {"ProjectionType": "ALL"}}
                for n, a in [("account", "account_id"), ("severity", "severity"), ("resource", "resource_id")]])
        monkeypatch.setattr(findings_repo, "_table", None)
        yield


def _finding(detected_at, saving=12.5):
    return Finding(rule_id="COST-EC2-003", domain="cost", account_id="111111111111",
                   region="us-east-1", resource_id="vol-1", resource_type="AWS::EC2::Volume",
                   severity="LOW", title="Unattached EBS volume", recommendation="Delete vol-1",
                   evidence={"size_gb": 100, "auto_remediable": False},
                   estimated_monthly_saving=saving, detected_at=detected_at)


def test_rescan_updates_same_item_and_keeps_first_seen(table):
    findings_repo.put_findings([_finding("2026-11-01T00:00:00+00:00")])
    findings_repo.put_findings([_finding("2026-11-02T00:00:00+00:00", saving=8.0)])

    item = findings_repo.get_finding("111111111111#us-east-1#COST-EC2-003#vol-1")
    assert item["first_seen_at"] == "2026-11-01T00:00:00+00:00"
    assert item["last_seen_at"] == "2026-11-02T00:00:00+00:00"
    assert item["estimated_monthly_saving"] == 8.0          # Decimal converted back
    assert item["evidence"] == {"size_gb": 100, "auto_remediable": False}
    assert item["expires_at"] == 1801353600                  # 2027-01-31: last_seen + 90 days


def test_queries_by_index(table):
    findings_repo.put_findings([_finding("2026-11-02T00:00:00+00:00")])
    assert len(findings_repo.findings_for_account("111111111111")) == 1
    assert len(findings_repo.findings_by_severity("LOW", since="2026-11-01")) == 1
    assert findings_repo.findings_by_severity("LOW", since="2026-11-03") == []
    assert len(findings_repo.findings_for_resource("vol-1")) == 1


def test_missing_finding_returns_none(table):
    assert findings_repo.get_finding("nope") is None
