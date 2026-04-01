# 09 – CodePipeline Cross-Account & Cross-Region

## Architecture

```
TOOLS ACCOUNT (us-east-1)          TARGET ACCOUNT (us-east-2)
┌──────────────────────────┐        ┌──────────────────────────┐
│  CodePipeline            │        │  CodeDeploy              │
│  ├─ Source (GitHub)      │        │  ├─ Application          │
│  ├─ Build  (CodeBuild)   │──────▶ │  └─ Deployment Group     │
│  └─ Deploy               │ assume │                          │
│                          │  role  │  IAM CrossAccountRole    │
│  KMS Key                 │        │  (trusted by tools acct) │
│  Artifact S3 Bucket      │        └──────────────────────────┘
│  Replication S3 Bucket   │
│  (us-east-2)             │
└──────────────────────────┘
```

## Key Concepts

| Concept | How it works here |
|---|---|
| Cross-account | `CrossAccountRole` in target account trusts the tools account principal |
| Cross-region | `cross_region_replication_buckets` in the Pipeline copies artifacts to `us-east-2` |
| Artifact encryption | KMS key in tools account; target account is granted `kms:Decrypt` |
| Action role | `DeployActionRole` bridges the pipeline to the cross-account role via `sts:AssumeRole` |

## Deploy Order

Both accounts must be CDK-bootstrapped with trust between them.

```bash
# 1. Bootstrap TARGET account, trusting the TOOLS account
cdk bootstrap \
  --profile target-profile \
  --trust 111111111111 \
  --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess \
  aws://222222222222/us-east-2

# 2. Bootstrap TOOLS account
cdk bootstrap --profile tools-profile aws://111111111111/us-east-1

# 3. Deploy target stack first (creates the cross-account role)
cdk deploy TargetAccountStack --profile target-profile

# 4. Deploy pipeline stack
cdk deploy PipelineStack --profile tools-profile
```

## Configuration

Edit `app.py` to set your real account IDs and regions:

```python
TOOLS_ACCOUNT  = "111111111111"
TOOLS_REGION   = "us-east-1"
TARGET_ACCOUNT = "222222222222"
TARGET_REGION  = "us-east-2"
```

Also update `stack.py` → `PipelineStack` with your GitHub org/repo and
the Secrets Manager secret name holding your GitHub OAuth token.
