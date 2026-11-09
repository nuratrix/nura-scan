"""EC2 cost checkers: COST-EC2-001 .. 005"""
import re
from datetime import datetime, timezone

from botocore.exceptions import ClientError

from nura.checkers.registry import checker
from nura.common.utils import cw_stat, utcnow
from nura.checkers.cost.ce import ec2_monthly, ebs_monthly

_STOPPED_AT = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)")


def _ec2(ctx):
    return ctx.session.client("ec2", region_name=ctx.region)


def _instances(ctx, state):
    for page in _ec2(ctx).get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": [state]}]):
        for reservation in page["Reservations"]:
            yield from reservation["Instances"]


@checker("COST-EC2-001")
def idle_instances(ctx, rule):
    p = rule["params"]
    for inst in _instances(ctx, "running"):
        cpu = cw_stat(ctx, "AWS/EC2", "CPUUtilization",
                      {"InstanceId": inst["InstanceId"]}, p["lookback_days"], "Average")
        if cpu is None or cpu > p["cpu_threshold_pct"]:
            continue
        yield (inst["InstanceId"],
               {"avg_cpu_pct": round(cpu, 2), "instance_type": inst["InstanceType"]},
               ec2_monthly(ctx, inst["InstanceType"]) * p["saving_fraction"])


def _days_stopped(inst):
    # AWS records the stop time only in this free-text field.
    m = _STOPPED_AT.search(inst.get("StateTransitionReason", ""))
    if not m:
        return 0
    stopped = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (utcnow() - stopped).days


@checker("COST-EC2-002")
def stopped_instances(ctx, rule):
    p = rule["params"]
    ec2 = _ec2(ctx)
    for inst in _instances(ctx, "stopped"):
        volume_ids = [m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) if "Ebs" in m]
        cost = 0.0
        if volume_ids:
            try:
                for v in ec2.describe_volumes(VolumeIds=volume_ids)["Volumes"]:
                    cost += ebs_monthly(ctx, v["VolumeType"], v["Size"])
            except (ClientError, LookupError):
                cost = p["fallback_volume_cost_usd"] * len(volume_ids)
        yield (inst["InstanceId"],
               {"days_stopped": _days_stopped(inst), "volume_count": len(volume_ids)},
               cost)


@checker("COST-EC2-003")
def unattached_volumes(ctx, rule):
    p = rule["params"]
    pages = _ec2(ctx).get_paginator("describe_volumes").paginate(
        Filters=[{"Name": "status", "Values": ["available"]}])
    for page in pages:
        for v in page["Volumes"]:
            # Age since creation: AWS keeps no detach time on the volume.
            # CloudTrail DetachVolume would be exact; good enough for now.
            days = (utcnow() - v["CreateTime"]).days
            yield (v["VolumeId"],
                   {"days_unattached": days, "size_gb": v["Size"], "volume_type": v["VolumeType"],
                    "auto_remediable": days > p["auto_remediable_after_days"]},
                   ebs_monthly(ctx, v["VolumeType"], v["Size"]))


@checker("COST-EC2-004")
def unused_elastic_ips(ctx, rule):
    for addr in _ec2(ctx).describe_addresses()["Addresses"]:
        if "AssociationId" not in addr:
            yield (addr["AllocationId"],
                   {"public_ip": addr["PublicIp"], "auto_remediable": True},
                   rule["params"]["monthly_cost_usd"])


@checker("COST-EC2-005")
def previous_gen_instances(ctx, rule):
    p = rule["params"]
    for inst in _instances(ctx, "running"):
        itype = inst["InstanceType"]
        family = itype.split(".")[0]
        if family not in p["previous_gen_families"]:
            continue
        yield (inst["InstanceId"],
               {"instance_type": itype,
                "current_gen_suggestion": p["current_gen_map"].get(family, p["current_gen_default"])},
               ec2_monthly(ctx, itype) * p["saving_fraction"])
