from datetime import datetime, timedelta, timezone


def utcnow():
    return datetime.now(timezone.utc)


def is_primary(ctx):
    """Account-wide checks (Cost Explorer) run once per account, not per region."""
    return ctx.region == ctx.primary_region

def cw_stat(ctx, namespace, metric, dims, days, stat):
    """Average of daily Averages, or max of daily Maximums. None if no data."""
    cw = ctx.session.client("cloudwatch", region_name=ctx.region)
    end = utcnow()
    points = cw.get_metric_statistics(
        Namespace=namespace, MetricName=metric,
        Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        StartTime=end - timedelta(days=days), EndTime=end,
        Period=86400, Statistics=[stat])["Datapoints"]
    if not points:
        return None
    values = [p[stat] for p in points]
    return max(values) if stat == "Maximum" else sum(values) / len(values)

