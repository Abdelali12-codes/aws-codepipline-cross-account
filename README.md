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

## CDK Bootstrap — Cross-Account Trust

Before deploying, both accounts must be CDK-bootstrapped.
The TARGET account bootstrap must explicitly trust the TOOLS account
so CDK can deploy resources into it from the tools account.

```bash
# 1. Bootstrap TOOLS account (us-east-1)
cdk bootstrap \
  --profile tools-profile \
  aws://111111111111/us-east-1

# 2. Bootstrap TARGET account (us-east-2)
#    --trust         → allows the TOOLS account to deploy into this account
#    --trust-for-lookup → allows CDK context lookups from the TOOLS account
#    --cloudformation-execution-policies → permissions CDK gets when deploying
cdk bootstrap \
  --profile target-profile \
  --trust 111111111111 \
  --trust-for-lookup 111111111111 \
  --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess \
  aws://222222222222/us-east-2
```

Replace `111111111111` with your tools account ID and `222222222222` with your target account ID.

## Deploy Order

```bash
# 1. Deploy TargetAccountStack first (creates the CrossAccountRole + CodeDeploy resources)
cdk deploy TargetAccountStack --profile target-profile

# 2. Deploy PipelineStack (creates the pipeline in the tools account)
cdk deploy PipelineStack --profile tools-profile
```

## Configuration

Edit `app.py` to set your real account IDs:

```python
TOOLS_ACCOUNT  = "111111111111"
TOOLS_REGION   = "us-east-1"
TARGET_ACCOUNT = "222222222222"
TARGET_REGION  = "us-east-2"
```

---

## Issues & Solutions

### 1. `artifact_store` vs `artifact_stores`
**Error:**
```
Unable to deserialize value as CfnPipeline.ArtifactStoreProperty
Wired struct has type ArtifactStoreMapProperty, which does not match expected type
```
**Cause:** `artifact_store` (singular) expects `ArtifactStoreProperty` directly.
`ArtifactStoreMapProperty` is only valid inside `artifact_stores` (plural).

**Solution:** Use `artifact_store` with `ArtifactStoreProperty` for single-region pipelines.
For cross-region pipelines use `artifact_stores` with `ArtifactStoreMapProperty` per region.

---

### 2. Pipeline requires `artifact_stores` for cross-region actions
**Error:**
```
Your pipeline contains actions in more than one region. Use 'pipeline.artifactStores'
instead of 'pipeline.artifactStore'
```
**Cause:** The Deploy action had `region=target_region` which differs from the pipeline region,
making it a cross-region pipeline. CodePipeline requires `artifact_stores` in this case.

**Solution:** Switched to `artifact_stores` with one `ArtifactStoreMapProperty` entry per region.

---

### 3. Duplicate construct ID `ArtifactBucket`
**Error:**
```
RuntimeError: There is already a Construct with name 'ArtifactBucket' in Stack [PipelineStack]
```
**Cause:** CDK internally generates a child construct named `ArtifactBucket` when attaching
encryption keys or grants, clashing with the explicit `"ArtifactBucket"` construct ID.

**Solution:** Renamed the bucket construct ID to `"PipelineArtifactBucket"`.

---

### 4. S3 409 Conflict on bucket creation
**Error:**
```
A conflicting conditional operation is currently in progress against this resource.
(Service: S3, Status Code: 409)
```
**Cause:** The explicit `bucket_name` was still being held by S3 from a previous
failed/deleted stack deployment.

**Solution:** Removed the explicit `bucket_name` so CDK generates a unique name,
avoiding conflicts with previously deleted buckets.

---

### 5. GitHub webhook 401 Bad Credentials
**Error:**
```
Webhook could not be registered with GitHub.
Error cause: Invalid credentials [StatusCode: 401]
```
**Cause:** `register_with_third_party=True` requires a GitHub classic Personal Access Token
with `repo` and `admin:repo_hook` scopes. Fine-grained tokens are not supported by the
CodePipeline GitHub v1 provider.

**Solution:** Set `register_with_third_party=False` and manually registered the webhook
in GitHub repo → Settings → Webhooks using the webhook URL from the CloudFormation output.

---

### 6. CodeBuild AccessDenied on artifact bucket
**Error:**
```
error while downloading key .../SourceOutp/Za3opGh.zip
AccessDenied: not authorized to perform s3:GetObject
```
**Cause:** The CodeBuild execution role had no permissions on the artifact bucket.

**Solution:** Added `artifact_bucket.grant_read_write(build_project.role)` after the
`build_project` definition.

---

### 7. `UnboundLocalError` on `build_project.role`
**Error:**
```
UnboundLocalError: cannot access local variable 'build_project'
where it is not associated with a value
```
**Cause:** `artifact_bucket.grant_read_write(build_project.role)` was placed before
`build_project` was defined.

**Solution:** Moved the grant line to after the `build_project` definition.

---

### 8. Deploy stage AccessDenied on artifact bucket
**Error:**
```
Unable to access the artifact with Amazon S3 object key '.../BuildOutpu/gqv9N3C'
The provided role does not have sufficient permissions.
```
**Cause:** The `CrossAccountRole` in the target account did not have permissions to read
from the source account's S3 artifact bucket or use the KMS key to decrypt artifacts.

**Solution:**
- Added `kms:GenerateDataKey` to the `CrossAccountRole` KMS policy statement
- Added `kms:GenerateDataKey` to the KMS key resource policy for the target account principal

---

### 9. Unnecessary `DeployActionRole`
**Issue:** A `DeployActionRole` in the source account was used as an intermediary to assume
the `CrossAccountRole` in the target account. This added unnecessary complexity.

**Solution:** Removed `DeployActionRole` entirely and set `role_arn=cross_account_role_arn`
directly on the Deploy action, which is the correct pattern per the AWS cross-account
pipeline documentation.

---

### 10. `target_pipeline_version` type error
**Error:**
```
TypeError: type of argument target_pipeline_version must be one of (int, float); got str instead
```
**Cause:** `cfn_pipeline.attr_version` returns a string token, but `target_pipeline_version`
expects an `int`.

**Solution:** Replaced `cfn_pipeline.attr_version` with the integer literal `1`.
