"""Reservation / Savings Plan checkers: COST-RI-001 .. 005

Cost Explorer runs in the governance account with a LINKED_ACCOUNT filter.
It only returns member-account data when the governance account is the
organization's management (payer) account.
"""
from collections import defaultdict

from nura.checkers.registry import checker
from nura.common.utils import is_primary, utcnow
from nura.checkers.cost.ce import (
    SERVICE_EC2, ce_client, ce_paged, ce_window, linked_account)


@checker("COST-RI-001")
def ri_coverage(ctx, rule):
    if not is_primary(ctx):
        return
    p = rule["params"]
    ce = ce_client(ctx)
    families = defaultdict(lambda: {"reserved": 0.0, "total": 0.0, "on_demand_cost": 0.0})
    periods = ce_paged(
        ce.get_reservation_coverage, "CoveragesByTime",
        TimePeriod=ce_window(p["lookback_days"]),   # no Granularity: CE rejects it with GroupBy
        GroupBy=[{"Type": "DIMENSION", "Key": "INSTANCE_TYPE"}],
        Filter={"And": [linked_account(ctx.account_id),
                        {"Dimensions": {"Key": "SERVICE", "Values": [SERVICE_EC2]}}]})
    for period in periods:
        for group in period.get("Groups", []):
            instance_type = next(iter(group.get("Attributes", {}).values()), "unknown")
            family = instance_type.split(".")[0]
            hours = group["Coverage"]["CoverageHours"]
            cost = group["Coverage"].get("CoverageCost", {})
            f = families[family]
            f["reserved"] += float(hours.get("ReservedHours", 0))
            f["total"] += float(hours.get("TotalRunningHours", 0))
            f["on_demand_cost"] += float(cost.get("OnDemandCost", 0))
    for family, f in families.items():
        if f["total"] == 0:
            continue
        pct = f["reserved"] / f["total"] * 100
        if pct >= p["coverage_below_pct"]:
            continue
        yield (family, {"coverage_pct": round(pct, 1)},
               f["on_demand_cost"] * p["saving_fraction"])   # min $50 applied by builder


@checker("COST-RI-002")
def ri_utilisation(ctx, rule):
    if not is_primary(ctx):
        return
    p = rule["params"]
    ce = ce_client(ctx)
    subs = defaultdict(lambda: {"purchased": 0.0, "actual": 0.0, "fee": 0.0, "attrs": {}})
    periods = ce_paged(
        ce.get_reservation_utilization, "UtilizationsByTime",
        TimePeriod=ce_window(p["lookback_days"]),   # no Granularity: CE rejects it with GroupBy
        GroupBy=[{"Type": "DIMENSION", "Key": "SUBSCRIPTION_ID"}],
        Filter=linked_account(ctx.account_id))
    for period in periods:
        for group in period.get("Groups", []):
            u = group["Utilization"]
            s = subs[group["Value"]]
            s["purchased"] += float(u.get("PurchasedHours", 0))
            s["actual"] += float(u.get("TotalActualHours", 0))
            s["fee"] += float(u.get("TotalAmortizedFee", 0))
            s["attrs"] = group.get("Attributes", {})
    for sub_id, s in subs.items():
        if s["purchased"] == 0:
            continue
        pct = s["actual"] / s["purchased"] * 100
        if pct >= p["utilisation_below_pct"]:
            continue
        wasted = s["fee"] * (1 - s["actual"] / s["purchased"])   # unused share of what you paid
        yield (sub_id,
               {"utilisation_pct": round(pct, 1),
                "instance_type": s["attrs"].get("instanceType", "unknown")},
               wasted)   # min $20 applied by builder


@checker("COST-RI-003")
def expiring_reservations(ctx, rule):
    p = rule["params"]
    ec2 = ctx.session.client("ec2", region_name=ctx.region)   # regional, member account
    active = ec2.describe_reserved_instances(
        Filters=[{"Name": "state", "Values": ["active"]}])["ReservedInstances"]
    for ri in active:
        days = (ri["End"] - utcnow()).days
        if days > p["expiry_window_days"]:
            continue
        yield (ri["ReservedInstancesId"],
               {"days_until_expiry": days, "instance_type": ri["InstanceType"],
                "expiry_date": ri["End"].date().isoformat()},
               None)   # informational, no estimate


@checker("COST-RI-004")
def savings_plan_coverage(ctx, rule):
    if not is_primary(ctx):
        return
    p = rule["params"]
    ce = ce_client(ctx)
    covered = on_demand = 0.0
    items = ce_paged(
        ce.get_savings_plans_coverage, "SavingsPlansCoverages", token_key="NextToken",
        TimePeriod=ce_window(p["lookback_days"]), Granularity="MONTHLY",
        Filter=linked_account(ctx.account_id))
    for item in items:
        c = item.get("Coverage", {})
        covered += float(c.get("SpendCoveredBySavingsPlans", 0))
        on_demand += float(c.get("OnDemandCost", 0))
    total = covered + on_demand
    if total == 0:
        return
    pct = covered / total * 100
    if pct >= p["coverage_below_pct"]:
        return
    yield (f"{ctx.account_id}-compute-savings-plans",
           {"coverage_pct": round(pct, 1)},
           on_demand * p["saving_fraction"])   # min $50 applied by builder


@checker("COST-RI-005")
def ri_purchase_recommendations(ctx, rule):
    if not is_primary(ctx):
        return
    p = rule["params"]
    recs = ce_client(ctx).get_reservation_purchase_recommendation(
        AccountId=ctx.account_id, AccountScope="LINKED", Service=p["service"],
        LookbackPeriodInDays=p["lookback_period"], TermInYears=p["term"],
        PaymentOption=p["payment_option"]).get("Recommendations", [])
    for rec in recs:
        total = float(rec.get("RecommendationSummary", {})
                      .get("TotalEstimatedMonthlySavingsAmount", 0))
        if total < p["min_recommendation_saving_usd"]:
            continue
        details = sorted(rec.get("RecommendationDetails", []),
                         key=lambda d: float(d.get("EstimatedMonthlySavingsAmount", 0)),
                         reverse=True)[:p["max_details_per_recommendation"]]
        for d in details:
            ec2 = d.get("InstanceDetails", {}).get("EC2InstanceDetails", {})
            itype, region = ec2.get("InstanceType", "unknown"), ec2.get("Region", "unknown")
            yield (f"ri-rec-{itype}-{region}",
                   {"recommended_count": d.get("RecommendedNumberOfInstancesToPurchase"),
                    "instance_type": itype, "region": region},
                   float(d.get("EstimatedMonthlySavingsAmount", 0)))   # min $10 each
