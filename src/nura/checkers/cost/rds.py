"""RDS cost checkers: COST-RDS-001 .. 004"""
from nura.checkers.registry import checker
from nura.common.utils import cw_stat, utcnow
from nura.checkers.cost.ce import rds_monthly, rds_downsize


def _rds(ctx):
    return ctx.session.client("rds", region_name=ctx.region)


def _available_dbs(ctx):
    for page in _rds(ctx).get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            if db["DBInstanceStatus"] == "available":
                yield db


def _monthly(ctx, db, cls=None, default=None):
    return rds_monthly(ctx, cls or db["DBInstanceClass"], db["Engine"], db.get("MultiAZ", False),
                       db.get("StorageType"), default)


@checker("COST-RDS-001")
def idle_databases(ctx, rule):
    p = rule["params"]
    for db in _available_dbs(ctx):
        db_id = db["DBInstanceIdentifier"]
        conns = cw_stat(ctx, "AWS/RDS", "DatabaseConnections",
                        {"DBInstanceIdentifier": db_id}, p["lookback_days"], "Maximum")
        if conns is None or conns > p["max_connections_threshold"]:
            continue
        cls = db["DBInstanceClass"]
        yield (db_id,
               {"max_connections": conns, "instance_class": cls},
               _monthly(ctx, db, default=p.get("default_monthly_price_usd")))


@checker("COST-RDS-002")
def oversized_databases(ctx, rule):
    p = rule["params"]
    for db in _available_dbs(ctx):
        cls = db["DBInstanceClass"]
        if any(s in cls for s in p["skip_class_substrings"]):
            continue
        dims = {"DBInstanceIdentifier": db["DBInstanceIdentifier"]}
        avg = cw_stat(ctx, "AWS/RDS", "CPUUtilization", dims, p["lookback_days"], "Average")
        peak = cw_stat(ctx, "AWS/RDS", "CPUUtilization", dims, p["lookback_days"], "Maximum")
        if avg is None or peak is None:
            continue
        if avg >= p["avg_cpu_below_pct"] or peak >= p["max_cpu_below_pct"]:
            continue
        current = _monthly(ctx, db, default=p.get("default_monthly_price_usd"))
        target = rds_downsize(ctx, cls, db["Engine"])
        if target:
            saving = current - _monthly(ctx, db, target, default=current * (1 - p["fallback_saving_fraction"]))
        else:
            target, saving = "the next smaller class", current * p["fallback_saving_fraction"]
        yield (db["DBInstanceIdentifier"],
               {"avg_cpu_pct": round(avg, 2), "max_cpu_pct": round(peak, 2),
                "current_instance_class": cls, "recommended_instance_class": target},
               saving)   # builder suppresses below min_saving_usd ($20)


@checker("COST-RDS-003")
def single_az_large_databases(ctx, rule):
    p = rule["params"]
    for db in _available_dbs(ctx):
        if db.get("MultiAZ"):
            continue
        cls = db["DBInstanceClass"]
        price = _monthly(ctx, db, default=0)   # unknown class -> not flagged
        if price < p["min_monthly_cost_usd"]:
            continue
        yield (db["DBInstanceIdentifier"],
               {"instance_class": cls, "monthly_cost_usd": price,
                "recommended_instance_class": rds_downsize(ctx, cls, db["Engine"]) or "a smaller class"},
               price * p["saving_fraction"])


@checker("COST-RDS-004")
def old_manual_snapshots(ctx, rule):
    p = rule["params"]
    pages = _rds(ctx).get_paginator("describe_db_snapshots").paginate(SnapshotType="manual")
    for page in pages:
        for snap in page["DBSnapshots"]:
            if "SnapshotCreateTime" not in snap:      # still being created
                continue
            age = (utcnow() - snap["SnapshotCreateTime"]).days
            if age <= p["max_age_days"]:
                continue
            # AllocatedStorage overstates incremental snapshot size: upper bound.
            yield (snap["DBSnapshotIdentifier"],
                   {"age_days": age, "size_gb": snap["AllocatedStorage"]},
                   snap["AllocatedStorage"] * p["storage_usd_per_gb_month"])
