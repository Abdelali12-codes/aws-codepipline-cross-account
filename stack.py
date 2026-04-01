import aws_cdk as cdk
from aws_cdk import (
    aws_codebuild as codebuild,
    aws_codedeploy as codedeploy,
    aws_codepipeline as codepipeline,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
from constructs import Construct


# ── Stack 1: deployed in the TARGET account ─────────────────────────────────
# Creates the cross-account role that the pipeline (tools account) assumes
# to perform deployments, plus the CodeDeploy resources being deployed to.
class TargetAccountStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str,
                 tools_account_id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Role assumed by CodePipeline (running in tools account) to deploy here.
        # The trust policy allows the tools account's pipeline execution role
        # to assume this role cross-account.
        self.cross_account_role = iam.Role(
            self, "CrossAccountRole",
            role_name="CodePipelineCrossAccountRole",
            assumed_by=iam.AccountPrincipal(tools_account_id),
        )

        # Allow CodeDeploy actions in this account
        self.cross_account_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "codedeploy:CreateDeployment",
                    "codedeploy:GetDeployment",
                    "codedeploy:GetDeploymentConfig",
                    "codedeploy:GetApplicationRevision",
                    "codedeploy:RegisterApplicationRevision",
                ],
                resources=["*"],
            )
        )

        # Allow reading artifacts from the pipeline's cross-region S3 bucket
        # (the pipeline will grant explicit bucket access separately)
        self.cross_account_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject*", "s3:GetBucket*", "s3:List*"],
                resources=["*"],
            )
        )

        # Allow decrypting artifacts encrypted with the pipeline's KMS key
        self.cross_account_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:DescribeKey"],
                resources=["*"],
            )
        )

        # ── VPC (default-like, 2 AZs) ─────────────────────────────────
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=0)

        # ── Security group — allow SSH + HTTP ────────────────────────
        sg = ec2.SecurityGroup(self, "InstanceSG", vpc=vpc)
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(22))
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80))

        # ── IAM role for EC2 (CodeDeploy agent needs S3 + SSM) ────────
        ec2_role = iam.Role(
            self, "EC2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonS3ReadOnlyAccess"),
            ],
        )

        # ── User data — install CodeDeploy agent on Ubuntu ────────────
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "apt-get update -y",
            "apt-get install -y ruby-full wget",
            f"wget https://aws-codedeploy-{self.region}.s3.{self.region}.amazonaws.com/latest/install",
            "chmod +x ./install",
            "./install auto",
            "systemctl enable codedeploy-agent",
            "systemctl start codedeploy-agent",
        )

        # ── Ubuntu 22.04 LTS EC2 instance ─────────────────────────────
        instance = ec2.Instance(
            self, "AppInstance",
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3, ec2.InstanceSize.MICRO
            ),
            machine_image=ec2.MachineImage.from_ssm_parameter(
                "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_group=sg,
            role=ec2_role,
            user_data=user_data,
        )
        # Tag matches the CodeDeploy deployment group filter
        cdk.Tags.of(instance).add("App", "python")

        # ── CodeDeploy application + deployment group in target account ──
        deploy_app = codedeploy.ServerApplication(
            self, "DeployApp",
            application_name="cross-account-app",
        )

        codedeploy.ServerDeploymentGroup(
            self, "DeploymentGroup",
            application=deploy_app,
            deployment_group_name="cross-account-dg",
            ec2_instance_tags=codedeploy.InstanceTagSet({"App": ["python"]}),
            deployment_config=codedeploy.ServerDeploymentConfig.ONE_AT_A_TIME,
            auto_rollback=codedeploy.AutoRollbackConfig(failed_deployment=True),
        )

        cdk.CfnOutput(self, "CrossAccountRoleArn", value=self.cross_account_role.role_arn)
        cdk.CfnOutput(self, "InstancePublicIp", value=instance.instance_public_ip)


# ── Stack 2: deployed in the TOOLS account ───────────────────────────────────
class PipelineStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str,
                 target_account_id: str,
                 target_region: str,
                 **kwargs):
        super().__init__(scope, id, **kwargs)

        # ── KMS key ──────────────────────────────────────────────────────
        # Required for cross-account artifact encryption.
        # The key policy must allow the target account to use it.
        key = kms.Key(self, "ArtifactKey",
                      removal_policy=cdk.RemovalPolicy.DESTROY)
        key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.AccountPrincipal(target_account_id)],
                actions=["kms:Decrypt", "kms:DescribeKey"],
                resources=["*"],
            )
        )

        # ── Artifact bucket (tools account, tools region) ─────────────────
        artifact_bucket = s3.Bucket(
            self, "PipelineArtifactBucket",
            bucket_name=f"codepipeline-artifacts-{self.account}",
            encryption_key=key,
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=True,
        )
        artifact_bucket.grant_read(iam.AccountPrincipal(target_account_id))

        # ── CodeBuild project (runs in tools account) ─────────────────────
        build_project = codebuild.PipelineProject(
            self, "BuildProject",
            encryption_key=key,
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "build": {"commands": ["echo Building..."]},
                },
                "artifacts": {
                    "files": ["**/*"],
                },
            }),
        )

        # ── Pipeline role ─────────────────────────────────────────────────
        pipeline_role = iam.Role(
            self, "PipelineRole",
            assumed_by=iam.ServicePrincipal("codepipeline.amazonaws.com"),
            inline_policies={
                "PipelinePolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "s3:GetObject*", "s3:PutObject*",
                                "s3:GetBucket*", "s3:List*",
                            ],
                            resources=[
                                artifact_bucket.bucket_arn,
                                f"{artifact_bucket.bucket_arn}/*"
                            ],
                        ),
                        iam.PolicyStatement(
                            actions=["kms:Decrypt", "kms:GenerateDataKey"],
                            resources=[key.key_arn],
                        ),
                        iam.PolicyStatement(
                            actions=[
                                "codebuild:StartBuild",
                                "codebuild:BatchGetBuilds",
                            ],
                            resources=["*"],
                        ),
                        iam.PolicyStatement(
                            actions=["sts:AssumeRole"],
                            resources=["*"],
                        ),
                    ]
                )
            },
        )

        # ── Deploy action role ────────────────────────────────────────────
        # Assumed by the pipeline role to call CodeDeploy in the target account.
        cross_account_role_arn = f"arn:aws:iam::{target_account_id}:role/CodePipelineCrossAccountRole"

        deploy_action_role = iam.Role(
            self, "DeployActionRole",
            assumed_by=iam.ArnPrincipal(pipeline_role.role_arn),
            inline_policies={
                "AssumeTargetRole": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=["sts:AssumeRole"],
                            resources=[cross_account_role_arn],
                        ),
                    ]
                )
            },
        )

        # ── Pipeline (L1 CfnPipeline) ─────────────────────────────────────
        # Using CfnPipeline so we can set `region` and `role_arn` per action,
        # enabling both cross-account and cross-region deployments.
        codepipeline.CfnPipeline(
            self, "Pipeline",
            name="cross-account-cross-region-pipeline",
            role_arn=pipeline_role.role_arn,
            artifact_store=codepipeline.CfnPipeline.ArtifactStoreProperty(
                type="S3",
                location=artifact_bucket.bucket_name,
                encryption_key=codepipeline.CfnPipeline.EncryptionKeyProperty(
                    type="KMS",
                    id=key.key_arn,
                ),
            ),
            stages=[
                # ── STAGE 1: Source ───────────────────────────────────────
                codepipeline.CfnPipeline.StageDeclarationProperty(
                    name="Source",
                    actions=[
                        codepipeline.CfnPipeline.ActionDeclarationProperty(
                            name="GitHub_Source",
                            action_type_id=codepipeline.CfnPipeline.ActionTypeIdProperty(
                                category="Source",
                                owner="ThirdParty",
                                provider="GitHub",
                                version="1",
                            ),
                            configuration={
                                "Owner": "Abdelali12-codes",
                                "Repo": "aws-codedeploy-sample",
                                "Branch": "master",
                                "OAuthToken": cdk.SecretValue.secrets_manager("github-access-token").unsafe_unwrap(),
                                "PollForSourceChanges": False,
                            },
                            output_artifacts=[codepipeline.CfnPipeline.OutputArtifactProperty(name="SourceOutput")],
                        ),
                    ],
                ),
                # ── STAGE 2: Build (tools account, tools region) ──────────
                codepipeline.CfnPipeline.StageDeclarationProperty(
                    name="Build",
                    actions=[
                        codepipeline.CfnPipeline.ActionDeclarationProperty(
                            name="Build",
                            action_type_id=codepipeline.CfnPipeline.ActionTypeIdProperty(
                                category="Build",
                                owner="AWS",
                                provider="CodeBuild",
                                version="1",
                            ),
                            configuration={"ProjectName": build_project.project_name},
                            input_artifacts=[codepipeline.CfnPipeline.InputArtifactProperty(name="SourceOutput")],
                            output_artifacts=[codepipeline.CfnPipeline.OutputArtifactProperty(name="BuildOutput")],
                        ),
                    ],
                ),
                # ── STAGE 3: Deploy (target account, target region) ───────
                # `region`   → routes the action to target_region
                # `role_arn` → pipeline assumes deploy_action_role, which
                #              then assumes cross_account_role_arn in the
                #              target account to call CodeDeploy
                codepipeline.CfnPipeline.StageDeclarationProperty(
                    name="Deploy",
                    actions=[
                        codepipeline.CfnPipeline.ActionDeclarationProperty(
                            name="CrossAccountDeploy",
                            region=target_region,
                            role_arn=deploy_action_role.role_arn,
                            action_type_id=codepipeline.CfnPipeline.ActionTypeIdProperty(
                                category="Deploy",
                                owner="AWS",
                                provider="CodeDeploy",
                                version="1",
                            ),
                            configuration={
                                "ApplicationName": "cross-account-app",
                                "DeploymentGroupName": "cross-account-dg",
                            },
                            input_artifacts=[codepipeline.CfnPipeline.InputArtifactProperty(name="BuildOutput")],
                        ),
                    ],
                ),
            ],
        )

        cdk.CfnOutput(self, "ArtifactBucket", value=artifact_bucket.bucket_name)
        cdk.CfnOutput(self, "KmsKeyArn", value=key.key_arn)
