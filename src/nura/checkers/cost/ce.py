import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from botocore.exceptions import ClientError

from nura.rules.loader import RULESET_DIR

SERVICE_EC2 = "Amazon Elastic Compute Cloud - Compute"

# ---- Cost Explorer: always the governance account, filtered to the member ----

def ce_client(ctx):
    return ctx.governance_session.client("ce", region_name="us-east-1")


def ce_window(days):
    end = datetime.now(timezone.utc).date()
    return {"Start": (end - timedelta(days=days)).isoformat(), "End": end.isoformat()}


def linked_account(account_id):
    return {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}}


def ce_paged(call, result_key, token_key="NextPageToken", **kwargs):
    """Cost Explorer pagination (token names differ by API)."""
    while True:
        resp = call(**kwargs)
        yield from resp.get(result_key, [])
        token = resp.get(token_key)
        if not token:
            return
        kwargs[token_key] = token


# ---- Price List API: public on-demand list prices, cached per process ----

HOURS_PER_MONTH = 730            # AWS billing convention
_PRICING_REGION = "us-east-1"    # API is only served from us-east-1, eu-central-1, ap-south-1
_STATIC_DIR = RULESET_DIR / "pricing"
_cache = {}


@lru_cache
def _static_prices(region):
    """Fallback table from rulesets/pricing/<region>.json ({} if absent)."""
    path = _STATIC_DIR / f"{region}.json"
    return json.loads(path.read_text())["prices"] if path.exists() else {}


def _on_demand_usd(ctx, service_code, filters):
    """First-tier (beginRange 0) non-zero on-demand USD unit price matching the filters, or None.

    Tiered products (e.g. S3 Standard) list their tiers in no fixed order.
    """
    pricing = ctx.session.client("pricing", region_name=_PRICING_REGION)
    resp = pricing.get_products(
        ServiceCode=service_code,
        Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()])
    for item in resp["PriceList"]:
        for term in json.loads(item)["terms"].get("OnDemand", {}).values():
            dims = sorted(term["priceDimensions"].values(),
                          key=lambda d: float(d.get("beginRange", 0)))
            for dim in dims:
                price = float(dim["pricePerUnit"].get("USD", 0))
                if price > 0:
                    return price
    return None


def _price(ctx, key, filters, fallback, service_code="AmazonEC2"):
    """Price List API first, static table second; cached per (kind, region, type)."""
    if key not in _cache:
        try:
            price = _on_demand_usd(ctx, service_code, filters)
        except ClientError:          # e.g. caller lacks pricing:GetProducts
            price = None
        if price is None:
            price = fallback()
        if price is None:
            raise LookupError(f"No on-demand price for {key}")
        _cache[key] = price
    return _cache[key]


def ec2_monthly(ctx, instance_type):
    """Monthly on-demand cost of one Linux, shared-tenancy instance in ctx.region."""
    static = _static_prices(ctx.region)
    hourly = _price(ctx, ("ec2", ctx.region, instance_type), {
        "regionCode": ctx.region,
        "instanceType": instance_type,
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
        "licenseModel": "No License required",
    }, lambda: static.get("ec2_hourly_price_usd", {}).get(
        instance_type, static.get("ec2_default_hourly_price_usd")))
    return hourly * HOURS_PER_MONTH


def ebs_monthly(ctx, volume_type, size_gb):
    """Monthly storage cost of one EBS volume in ctx.region (excludes provisioned IOPS/throughput)."""
    static = _static_prices(ctx.region)
    per_gb = _price(ctx, ("ebs", ctx.region, volume_type), {
        "regionCode": ctx.region,
        "productFamily": "Storage",
        "volumeApiName": volume_type,
    }, lambda: static.get("ebs_usd_per_gb_month", {}).get(
        volume_type, static.get("ebs_default_usd_per_gb_month")))
    return per_gb * size_gb


# RDS Engine -> Price List "databaseEngine". Licensed engines (Oracle, SQL Server)
# need edition/license filters too; they use the static table until added.
_RDS_ENGINES = {
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
    "mariadb": "MariaDB",
    "aurora-mysql": "Aurora MySQL",
    "aurora-postgresql": "Aurora PostgreSQL",
}
_RDS_SIZES = ["micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge",
              "8xlarge", "12xlarge", "16xlarge", "24xlarge", "32xlarge"]


def rds_monthly(ctx, db_class, engine="mysql", multi_az=False, storage_type=None, default=None):
    """Monthly on-demand instance cost (no storage) of one RDS DB instance in ctx.region.

    Falls back to the static table, then to `default`; with neither, raises LookupError.
    """
    pl_engine = _RDS_ENGINES.get(engine)
    aurora = engine.startswith("aurora")
    deployment = "Multi-AZ" if multi_az and not aurora else "Single-AZ"   # Aurora replicas are separate instances
    storage = "Aurora IO Optimization Mode" if storage_type == "aurora-iopt1" else "EBS Only"
    static = _static_prices(ctx.region)
    static_monthly = static.get("rds_monthly_price_usd", {}).get(db_class)

    def fallback():
        if static_monthly is None:
            return None
        return static_monthly * (2 if deployment == "Multi-AZ" else 1) / HOURS_PER_MONTH

    try:
        if pl_engine is None:
            hourly = fallback()
            if hourly is None:
                raise LookupError(f"No on-demand price for ('rds', {ctx.region!r}, {db_class!r}, {engine!r})")
        else:
            hourly = _price(ctx, ("rds", ctx.region, db_class, engine, deployment, storage), {
                "regionCode": ctx.region,
                "instanceType": db_class,
                "databaseEngine": pl_engine,
                "deploymentOption": deployment,
                "storage": storage,
            }, fallback, service_code="AmazonRDS")
    except LookupError:
        if default is None:
            raise
        return default
    return hourly * HOURS_PER_MONTH


def rds_downsize(ctx, db_class, engine="mysql"):
    """Next smaller class in the same family that exists for this engine/region, or None."""
    try:
        prefix, family, size = db_class.split(".")
        idx = _RDS_SIZES.index(size)
    except ValueError:                      # unexpected format or e.g. ".metal"
        return None
    for smaller in reversed(_RDS_SIZES[:idx]):
        candidate = f"{prefix}.{family}.{smaller}"
        try:
            rds_monthly(ctx, candidate, engine)
            return candidate
        except LookupError:
            continue
    return None


# CloudWatch BucketSizeBytes StorageType -> (Price List service code, volumeType)
_S3_CLASSES = {
    "StandardStorage": ("AmazonS3", "Standard"),
    "ReducedRedundancyStorage": ("AmazonS3", "Reduced Redundancy"),
    "StandardIAStorage": ("AmazonS3", "Standard - Infrequent Access"),
    "OneZoneIAStorage": ("AmazonS3", "One Zone - Infrequent Access"),
    "IntelligentTieringStorage": ("AmazonS3", "Intelligent-Tiering Frequent Access"),
    "IntelligentTieringFAStorage": ("AmazonS3", "Intelligent-Tiering Frequent Access"),
    "IntelligentTieringIAStorage": ("AmazonS3", "Intelligent-Tiering Infrequent Access"),
    "IntelligentTieringAIAStorage": ("AmazonS3", "Intelligent-Tiering Archive Instant Access"),
    "IntelligentTieringAAStorage": ("AmazonS3", "IntelligentTieringArchiveAccess"),
    "IntelligentTieringDAAStorage": ("AmazonS3", "IntelligentTieringDeepArchiveAccess"),
    "GlacierInstantRetrievalStorage": ("AmazonS3", "Glacier Instant Retrieval"),
    "GlacierStorage": ("AmazonS3", "Amazon Glacier"),
    # Deep Archive storage is billed under its own service code; the AmazonS3
    # "Glacier Deep Archive" product is only the $0.021 staging charge.
    "DeepArchiveStorage": ("AmazonS3GlacierDeepArchive", "Glacier Deep Archive"),
}
# Keys the static pricing file uses where they differ from CloudWatch's
_S3_STATIC_ALIASES = {
    "GlacierInstantRetrievalStorage": "GlacierInstantRetrieval",
    "GlacierStorage": "GlacierFlexibleRetrieval",
}


def s3_gb_month(ctx, storage_type="StandardStorage"):
    """First-tier USD per GB-month for an S3 storage class in ctx.region.

    `storage_type` is the CloudWatch BucketSizeBytes StorageType (e.g. "StandardIAStorage").
    Request, retrieval and monitoring charges are not included.
    """
    static_table = _static_prices(ctx.region).get("s3_usd_per_gb_month", {})

    def fallback():
        return static_table.get(storage_type, static_table.get(_S3_STATIC_ALIASES.get(storage_type)))

    if storage_type not in _S3_CLASSES:
        price = fallback()
        if price is None:
            raise LookupError(f"No price for S3 storage type {storage_type!r} in {ctx.region}")
        return price
    service_code, volume_type = _S3_CLASSES[storage_type]
    filters = {"regionCode": ctx.region, "volumeType": volume_type}
    if service_code == "AmazonS3":
        filters["productFamily"] = "Storage"
    return _price(ctx, ("s3", ctx.region, storage_type), filters, fallback,
                  service_code=service_code)
