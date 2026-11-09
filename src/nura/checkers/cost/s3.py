"""S3 cost checkers: COST-S3-001 .. 004

S3 lists buckets globally, so each bucket is checked only in its home region.
Lookups are cached on ctx.cache so four rules don't repeat the same API calls.
"""
from datetime import timedelta

from botocore.exceptions import ClientError

from nura.checkers.registry import checker
from nura.common.utils import utcnow
from nura.checkers.cost.ce import s3_gb_month

GB = 1024 ** 3
_LEGACY_LOCATIONS = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}


def _s3(ctx):
    return ctx.session.client("s3", region_name=ctx.region)


def _buckets_here(ctx):
    if "s3_buckets" not in ctx.cache:
        s3 = _s3(ctx)
        found = []
        for b in s3.list_buckets()["Buckets"]:
            try:
                loc = s3.get_bucket_location(Bucket=b["Name"]).get("LocationConstraint")
            except ClientError:
                continue
            found.append((b["Name"], _LEGACY_LOCATIONS.get(loc, loc)))
        ctx.cache["s3_buckets"] = found
    return [name for name, loc in ctx.cache["s3_buckets"] if loc == ctx.region]


def _standard_size_gb(ctx, bucket):
    """Latest daily BucketSizeBytes for Standard storage (the part these rules target)."""
    sizes = ctx.cache.setdefault("s3_size_gb", {})
    if bucket not in sizes:
        cw = ctx.session.client("cloudwatch", region_name=ctx.region)
        end = utcnow()
        points = cw.get_metric_statistics(
            Namespace="AWS/S3", MetricName="BucketSizeBytes",
            Dimensions=[{"Name": "BucketName", "Value": bucket},
                        {"Name": "StorageType", "Value": "StandardStorage"}],
            StartTime=end - timedelta(days=3), EndTime=end,
            Period=86400, Statistics=["Average"])["Datapoints"]
        latest = max(points, key=lambda p: p["Timestamp"]) if points else None
        sizes[bucket] = latest["Average"] / GB if latest else 0.0
    return sizes[bucket]


def _enabled_lifecycle_rules(ctx, bucket):
    rules = ctx.cache.setdefault("s3_lifecycle", {})
    if bucket not in rules:
        try:
            all_rules = _s3(ctx).get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
                raise
            all_rules = []
        rules[bucket] = [r for r in all_rules if r.get("Status") == "Enabled"]
    return rules[bucket]


@checker("COST-S3-001")
def no_lifecycle_policy(ctx, rule):
    p = rule["params"]
    delta = s3_gb_month(ctx, "StandardStorage") - s3_gb_month(ctx, "StandardIAStorage")
    for bucket in _buckets_here(ctx):
        size = _standard_size_gb(ctx, bucket)
        if size < p["min_bucket_size_gb"] or _enabled_lifecycle_rules(ctx, bucket):
            continue
        yield bucket, {"bucket_size_gb": round(size, 1)}, size * delta * p["saving_fraction"]


def _uses_intelligent_tiering(ctx, bucket):
    for r in _enabled_lifecycle_rules(ctx, bucket):
        if any(t.get("StorageClass") == "INTELLIGENT_TIERING" for t in r.get("Transitions", [])):
            return True
    resp = _s3(ctx).list_bucket_intelligent_tiering_configurations(Bucket=bucket)
    return bool(resp.get("IntelligentTieringConfigurationList"))


@checker("COST-S3-002")
def no_intelligent_tiering(ctx, rule):
    p = rule["params"]
    for bucket in _buckets_here(ctx):
        size = _standard_size_gb(ctx, bucket)
        if size < p["min_bucket_size_gb"] or _uses_intelligent_tiering(ctx, bucket):
            continue
        yield (bucket, {"bucket_size_gb": round(size, 1)},
               size * s3_gb_month(ctx, "StandardStorage") * p["saving_fraction"])


@checker("COST-S3-003")
def no_noncurrent_expiry(ctx, rule):
    p = rule["params"]
    s3 = _s3(ctx)
    for bucket in _buckets_here(ctx):
        if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
            continue
        size = _standard_size_gb(ctx, bucket)
        if size < p["min_bucket_size_gb"]:
            continue
        handled = any("NoncurrentVersionExpiration" in r or "NoncurrentVersionTransitions" in r
                      for r in _enabled_lifecycle_rules(ctx, bucket))
        if handled:
            continue
        yield (bucket, {"bucket_size_gb": round(size, 1)},
               size * s3_gb_month(ctx, "StandardStorage") * p["saving_fraction"])


@checker("COST-S3-004")
def no_storage_class_analysis(ctx, rule):
    p = rule["params"]
    s3 = _s3(ctx)
    for bucket in _buckets_here(ctx):
        size = _standard_size_gb(ctx, bucket)
        if size < p["min_bucket_size_gb"]:
            continue
        configs = s3.list_bucket_analytics_configurations(Bucket=bucket)
        if configs.get("AnalyticsConfigurationList"):
            continue
        yield bucket, {"bucket_size_gb": round(size, 1)}, None   # no estimate: analysis is free
