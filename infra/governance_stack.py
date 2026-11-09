"""Governance account stack: deterministic scan pipeline.

    EventBridge (hourly) -> account_sync -> AccountRegistry
    EventBridge (9 daily rules, one per domain) -> Fanout -> ScanQueue (1 msg = 1 account)
        -> Scanner (batch_size=1) -> Findings;  3 failed attempts -> ScanDLQ

Context (cdk.json or -c key=value):
    org_read_role_arn   role in the management account for ListAccounts, if this
                        account is neither the management account nor Organizations
                        delegated admin
    scan_regions        "" (stack region), "all", or "us-east-1,eu-west-1"
    scanner_max_concurrency   default 10
    import_tables_only  "true": synthesize only Findings + AccountRegistry, for `cdk import`
"""
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

REPO = Path(__file__).resolve().parents[1]
RUNTIME = lambda_.Runtime.PYTHON_3_12

# One scheduled rule per agent domain; names match rulesets/<domain>.json.
DOMAINS = ["security", "infrastructure", "compliance", "cost", "cost-anomaly",
           "incident", "network-security", "access-analyzer", "backup-dr"]

MEMBER_SCANNER_ROLE = "governance-platform-scanner"   # infra/member-scanner-role.yaml
SCANNER_TIMEOUT = Duration.minutes(15)


class GovernanceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        self._tables()
        if ctx("import_tables_only"):
            # `cdk import` cannot create resources in the same operation, so adopting the
            # existing tables is a stack holding only them. See infra/README.md.
            return

        code = lambda_.Code.from_asset(str(REPO / "src"), exclude=["**/__pycache__"])
        rulesets = lambda_.LayerVersion(
            self, "Rulesets",
            code=lambda_.Code.from_asset(str(REPO / "rulesets"), exclude=["schema", "*.py"]),
            compatible_runtimes=[RUNTIME],
            description="Nura rule catalog + static pricing, mounted at /opt")

        def fn(name, handler, timeout, env, **kw):
            return lambda_.Function(
                self, name, runtime=RUNTIME, code=code, handler=handler,
                timeout=timeout, memory_size=kw.pop("memory_size", 256), environment=env,
                log_group=logs.LogGroup(self, f"{name}Logs",
                                        retention=logs.RetentionDays.ONE_MONTH,
                                        removal_policy=RemovalPolicy.DESTROY),
                **kw)

        # ---- account_sync: hourly ListAccounts -> AccountRegistry ----
        org_role = ctx("org_read_role_arn")
        sync_env = {"ACCOUNT_REGISTRY_TABLE": self.registry_table.table_name}
        if org_role:
            sync_env["ORG_READ_ROLE_ARN"] = org_role
        account_sync = fn("AccountSync", "nura.pipeline.account_sync.handler.lambda_handler",
                          Duration.minutes(5), sync_env)
        self.registry_table.grant_read_write_data(account_sync)
        if org_role:
            account_sync.add_to_role_policy(iam.PolicyStatement(
                actions=["sts:AssumeRole"], resources=[org_role]))
        else:
            account_sync.add_to_role_policy(iam.PolicyStatement(
                actions=["organizations:ListAccounts", "organizations:DescribeOrganization"],
                resources=["*"]))
        events.Rule(self, "AccountSyncHourly", schedule=events.Schedule.rate(Duration.hours(1)),
                    targets=[targets.LambdaFunction(account_sync)])

        # ---- scan queue + DLQ ----
        dlq = sqs.Queue(self, "ScanDLQ", retention_period=Duration.days(14),
                        encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True)
        queue = sqs.Queue(
            self, "ScanQueue",
            # AWS guidance for Lambda SQS sources: >= 6x the function timeout
            visibility_timeout=Duration.minutes(SCANNER_TIMEOUT.to_minutes() * 6),
            encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3))

        # ---- fanout: 9 domain schedules -> 1 message per account ----
        fanout = fn("Fanout", "nura.pipeline.fanout.handler.lambda_handler",
                    Duration.minutes(2),
                    {"ACCOUNT_REGISTRY_TABLE": self.registry_table.table_name,
                     "SCAN_QUEUE_URL": queue.queue_url})
        self.registry_table.grant_read_data(fanout)
        queue.grant_send_messages(fanout)
        for hour, domain in enumerate(DOMAINS, start=1):   # staggered 01:00-09:00 UTC
            events.Rule(
                self, f"Scan-{domain}",
                schedule=events.Schedule.cron(minute="0", hour=str(hour)),
                targets=[targets.LambdaFunction(
                    fanout, event=events.RuleTargetInput.from_object({"domain": domain}))])

        # ---- scanner: 1 invocation = 1 account ----
        scanner = fn("Scanner", "nura.pipeline.scanner.handler.lambda_handler",
                     SCANNER_TIMEOUT,
                     {"FINDINGS_TABLE": self.findings_table.table_name,
                      "NURA_RULESET_DIR": "/opt",
                      "SCAN_REGIONS": ctx("scan_regions") or ""},
                     memory_size=512, layers=[rulesets])
        self.findings_table.grant_read_write_data(scanner)
        scanner.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=[f"arn:aws:iam::*:role/{MEMBER_SCANNER_ROLE}"]))
        # Scanning the governance account itself, Cost Explorer and the Price List API
        # use the scanner's own role. Narrow this once the full rule set is known.
        scanner.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("ReadOnlyAccess"))
        scanner.add_event_source(sources.SqsEventSource(
            queue, batch_size=1, report_batch_item_failures=True,
            max_concurrency=int(ctx("scanner_max_concurrency") or 10)))

        for name, value in {
            "FindingsTableName": self.findings_table.table_name,
            "FindingsTableStreamArn": self.findings_table.table_stream_arn or "",
            "AccountRegistryTableName": self.registry_table.table_name,
            "ScanQueueUrl": queue.queue_url,
            "ScanDLQUrl": dlq.queue_url,
            "AccountSyncFunction": account_sync.function_name,
            "FanoutFunction": fanout.function_name,
            "ScannerFunction": scanner.function_name,
        }.items():
            CfnOutput(self, name, value=value)

    def _tables(self):
        def gsi(name, partition):
            return ddb.GlobalSecondaryIndexPropsV2(
                index_name=name,
                partition_key=ddb.Attribute(name=partition, type=ddb.AttributeType.STRING),
                sort_key=ddb.Attribute(name="last_seen_at", type=ddb.AttributeType.STRING))

        # Operational store, not the audit record (that is the WORM decision log).
        # finding_id = account#region#rule#resource, so a rescan overwrites the same item.
        # expires_at is pushed out 90 days every time a finding is seen again,
        # so items expire 90 days after the issue was last observed.
        self.findings_table = ddb.TableV2(
            self, "Findings",
            table_name="Findings",
            partition_key=ddb.Attribute(name="finding_id", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            dynamo_stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,   # HIGH/CRIT -> Orchestrator
            removal_policy=RemovalPolicy.RETAIN,
            global_secondary_indexes=[
                gsi("by-account", "account_id"),     # domain agents: everything in one account
                gsi("by-severity", "severity"),      # triage: recent HIGH / CRITICAL
                gsi("by-resource", "resource_id"),   # related findings on one resource
            ],
        )

        # Organization accounts, upserted hourly by account_sync. Small: fanout scans it.
        self.registry_table = ddb.TableV2(
            self, "AccountRegistry",
            table_name="AccountRegistry",
            partition_key=ddb.Attribute(name="account_id", type=ddb.AttributeType.STRING),
            billing=ddb.Billing.on_demand(),
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
