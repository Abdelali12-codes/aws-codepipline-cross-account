#!/usr/bin/env python3
import aws_cdk as cdk
from stack import PipelineStack, TargetAccountStack

# ── Account / region configuration ──────────────────────────────────────────
# Replace these with your actual account IDs and regions.
TOOLS_ACCOUNT  = "080266302756"
TOOLS_REGION   = "us-east-2"

TARGET_ACCOUNT = "100275906299"
TARGET_REGION  = "us-east-2"

app = cdk.App()

target_stack = TargetAccountStack(
    app, "TargetAccountStack",
    tools_account_id=TOOLS_ACCOUNT,
    env=cdk.Environment(account=TARGET_ACCOUNT, region=TARGET_REGION),
)

# ── Step 2: deploy PipelineStack into the TOOLS account ──────────────────────
# Pass the cross-account role ARN output from the target stack.
PipelineStack(
    app, "PipelineStack",
    target_account_id=TARGET_ACCOUNT,
    target_region=TARGET_REGION,
    env=cdk.Environment(account=TOOLS_ACCOUNT, region=TOOLS_REGION),
)

app.synth()
