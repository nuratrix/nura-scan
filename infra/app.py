import os

import aws_cdk as cdk

from governance_stack import GovernanceStack

app = cdk.App()
GovernanceStack(app, "NuraGovernance", env=cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1")))
app.synth()
