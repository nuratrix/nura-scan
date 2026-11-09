from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

@dataclass
class Finding:
    rule_id: str
    domain: str
    account_id: str
    region: str
    resource_id: str
    resource_type: str
    severity: str                      # LOW | MEDIUM | HIGH | CRITICAL
    title: str
    recommendation: str
    evidence: dict = field(default_factory=dict)       # measured values
    estimated_monthly_saving: float | None = None
    ruleset_version: str = "local"
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def finding_id(self) -> str:
        return f"{self.account_id}#{self.region}#{self.rule_id}#{self.resource_id}"

    def to_item(self) -> dict:
        return {**asdict(self), "finding_id": self.finding_id}