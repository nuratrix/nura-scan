# Deploy governance-platform-scanner (Nura-Deterministic) to every member account with a service-managed StackSet.
#
#   .\infra\deploy_scanner_role.ps1     # first deploy and every template change
#
# Run from the repository root in the management account (884458154630). Targets the org root, so every
# current account and any account added later gets the role (auto-deployment). The management
# account itself is never targeted by service-managed StackSets; Nura uses its own credentials there.
$ErrorActionPreference = "Stop"
$StackSet = "nura-member-scanner-role"
$Template = "file://infra/member-scanner-role.yaml"
$Region   = "us-east-1"                 # IAM is global: one region is enough

function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

$Root = (aws organizations list-roots --query "Roots[0].Id" --output text); Check "list-roots"

$ErrorActionPreference = "Continue"     # PS 5.1 turns native stderr into errors under Stop
aws cloudformation describe-stack-set --stack-set-name $StackSet --call-as SELF 2>$null | Out-Null
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"

if (-not $exists) {
    Write-Host "Creating StackSet $StackSet"
    aws cloudformation create-stack-set --stack-set-name $StackSet --template-body $Template `
        --permission-model SERVICE_MANAGED `
        --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false `
        --capabilities CAPABILITY_NAMED_IAM `
        --description "Nura scanner cross-account role (governance-platform-scanner)" | Out-Null
    Check "create-stack-set"

    Write-Host "Deploying to all accounts under $Root"
    $op = (aws cloudformation create-stack-instances --stack-set-name $StackSet `
        --deployment-targets OrganizationalUnitIds=$Root --regions $Region `
        --operation-preferences FailureToleranceCount=0,MaxConcurrentCount=5 `
        --query OperationId --output text)
    Check "create-stack-instances"
} else {
    Write-Host "Updating StackSet $StackSet"
    $op = (aws cloudformation update-stack-set --stack-set-name $StackSet --template-body $Template `
        --capabilities CAPABILITY_NAMED_IAM `
        --operation-preferences FailureToleranceCount=0,MaxConcurrentCount=5 `
        --query OperationId --output text)
    Check "update-stack-set"
}

do {
    Start-Sleep -Seconds 10
    $status = (aws cloudformation describe-stack-set-operation --stack-set-name $StackSet `
        --operation-id $op --query StackSetOperation.Status --output text)
    Write-Host "  operation $op : $status"
} while ($status -in @("RUNNING", "QUEUED", "STOPPING"))

aws cloudformation list-stack-instances --stack-set-name $StackSet `
    --query "Summaries[].{account:Account,status:Status,detail:StatusReason}" --output table
if ($status -ne "SUCCEEDED") { throw "StackSet operation $status" }
