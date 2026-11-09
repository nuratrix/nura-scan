"""
Shared Utilities — Multi-Account Edition
Adds assume_role_session() helper used by all checker Lambdas to scan member accounts.
"""

import os
import boto3
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger()

# Module-level cache: persists across warm Lambda invocations.
# Key: (account_id, role_name) → (boto3.Session, expiry datetime)
_SESSION_CACHE: dict = {}


def get_regions(service: str = "ec2", session=None) -> list:
    """Return all enabled regions for a service."""
    ec2 = (session or boto3).client("ec2", region_name="us-east-1")
    regions = ec2.describe_regions(AllRegions=False)["Regions"]
    return [r["RegionName"] for r in regions]


def get_scan_regions(session=None) -> list:
    """Return the list of regions to scan for this invocation.

    Resolution order:
      1. SCAN_REGIONS env var not set / empty → deployment region only (default, no extra cost)
      2. SCAN_REGIONS=all                     → every enabled region via describe_regions
      3. SCAN_REGIONS=us-east-1,eu-west-1     → explicit list

    Customers opt in to multi-region by setting SCAN_REGIONS in their
    GitHub Environment — single-region behaviour is unchanged by default.
    """
    configured = os.environ.get("SCAN_REGIONS", "").strip()
    if not configured:
        return [os.environ.get("AWS_REGION", "us-east-1")]
    regions = [r.strip() for r in configured.split(",") if r.strip()]
    if regions == ["all"]:
        return get_regions(session=session)
    return regions


# ─── Multi-Account: cross-account assume-role helper ──────────────────────────

def assume_role_session(account_id: str, role_name: str = "governance-platform-scanner",
                        region: str = "us-east-1",
                        external_id: str = "governance-platform") -> boto3.Session:
    """
    Assume the cross-account scanner role and return a boto3.Session.
    Results are cached in _SESSION_CACHE for the lifetime of the Lambda
    container (warm invocations skip STS entirely).
    """
    if not account_id:
        raise ValueError("assume_role_session called with empty account_id — check sessionAttributes propagation")

    cache_key = (account_id, role_name)
    cached = _SESSION_CACHE.get(cache_key)
    if cached:
        session, expiry = cached
        if datetime.now(timezone.utc) < expiry - timedelta(minutes=5):
            logger.debug(f"STS cache hit for account {account_id}")
            return session

    role_arn     = f"arn:aws:iam::{account_id}:role/{role_name}"
    session_name = f"governance-scan-{account_id}"
    logger.info(f"Assuming role {role_arn} for account {account_id}")

    sts  = boto3.client("sts")
    resp = sts.assume_role(
        RoleArn         = role_arn,
        RoleSessionName = session_name,
        ExternalId      = external_id,
        DurationSeconds = 3600,
    )

    creds  = resp["Credentials"]
    expiry = creds["Expiration"]
    session = boto3.Session(
        aws_access_key_id     = creds["AccessKeyId"],
        aws_secret_access_key = creds["SecretAccessKey"],
        aws_session_token     = creds["SessionToken"],
        region_name           = region,
    )
    _SESSION_CACHE[cache_key] = (session, expiry)
    return session


def get_or_assume_session(account_id: str, governance_account_id: str,
                           role_name: str = "governance-platform-scanner",
                           region: str = "us-east-1") -> boto3.Session:
    """
    Return the default session if account_id matches the governance account,
    otherwise assume the cross-account scanner role.
    Avoids unnecessary assume-role calls when scanning the governance account itself.
    """
    if account_id == governance_account_id:
        logger.info(f"Scanning governance account {account_id} — using default session")
        return boto3.Session(region_name=region)
    return assume_role_session(account_id, role_name=role_name, region=region)
