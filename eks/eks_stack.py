from pathlib import Path
import string
import yaml
import aws_cdk as cdk
from constructs import Construct

from aws_cdk import (
    aws_iam as iam,
    aws_eks as eks,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_sqs as sqs,
    aws_events as events,
    aws_events_targets as targets
)
from aws_cdk.lambda_layer_kubectl_v32 import KubectlV32Layer

class EksStack(cdk.Stack):
    """Create an EKS cluster that's bootstrapped with some helmcharts:
        -argocd
        -argo image updater
        -karpenter

    Args:
        cdk (_type_): _description_
    """

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create VPC with 2 public and 2 private subnets
        nat_gateway_provider = ec2.NatProvider.instance_v2(
            key_pair=None,
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE2, ec2.InstanceSize.MICRO
            )
        )
        vpc = ec2.Vpc(
            self, "MyVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.190.0.0/16"),
            max_azs=3,
            nat_gateway_provider=nat_gateway_provider,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                )
            ]
        )

        # IAM role for the EKS cluster
        cluster_admin_role = iam.Role(
            self, "ClusterAdminRole",
            assumed_by=iam.AccountRootPrincipal()
        )

        # EKS cluster
        cluster = eks.Cluster(
            self, "MyEksCluster",
            cluster_name="my-eks-cluster",
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)],
            kubectl_layer=KubectlV32Layer(self, "KubectlLayer"),
            default_capacity=0,
            version=eks.KubernetesVersion.V1_32,
            #masters_role=cluster_admin_role,
            authentication_mode=eks.AuthenticationMode.API_AND_CONFIG_MAP,
            alb_controller=eks.AlbControllerOptions(
                version=eks.AlbControllerVersion.V2_8_2
            ),
            tags={ "Project": "EKS", "Owner": "Roger", "Environment": "Test" }
        )

        access_entry1 = eks.AccessPolicy.from_access_policy_name("AmazonEKSClusterAdminPolicy", access_scope_type=eks.AccessScopeType.CLUSTER)
        access_entry2 = eks.AccessPolicy.from_access_policy_name("AmazonEKSAdminPolicy", access_scope_type=eks.AccessScopeType.NAMESPACE, namespaces=["karpenter"])

        cluster.add_nodegroup_capacity("prefix-ng-spot",
            instance_types=[
                ec2.InstanceType("m5.large"),
                ec2.InstanceType("t3.small"),
                ec2.InstanceType("t3a.small")
            ],
            min_size=2,
            ami_type=eks.NodegroupAmiType.AL2023_X86_64_STANDARD,
            labels={"role": "prefix-ng-spot"},
            capacity_type=eks.CapacityType.SPOT,
            disk_size=20,
            nodegroup_name="prefix-ng-spot",
        )

        #cluster.aws_auth.add_user_mapping(cluster_admin_role, groups=["system:masters"])
        cluster.grant_access("EKSAdminRole", cluster_admin_role.role_arn, [access_entry2])
        cluster.grant_access("clusterAdminAccess", "arn:aws:iam::146256358142:user/roger", [access_entry1])

        #install cluster addons.
        eks.Addon(self, "ebs-csi-driver", addon_name="aws-ebs-csi-driver", cluster=cluster, addon_version="v1.40.0-eksbuild.1")

        karpenter_namespace = cluster.add_manifest("karpenter-namespace", {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": "karpenter"
            }
        })

        karpenter_sa = cluster.add_service_account("karpenter", namespace="karpenter", identity_type=eks.IdentityType.POD_IDENTITY)
        
        interruption_queue = sqs.Queue(
            self, "InterruptionQueue",
            queue_name="interruption-queue",
            visibility_timeout=cdk.Duration.seconds(300),
            retention_period=cdk.Duration.minutes(5),
        )

        karpenter_event_bridge_rules = [
            events.Rule(
                self, "InterruptionRule",
                description="Rule to trigger sqs message when an instance is interrupted",
                event_pattern=events.EventPattern(
                    source=["aws.ec2"],
                    detail_type=["EC2 Spot Instance Interruption Warning"],
                )
            ),
            events.Rule(
                self, "RebalanceRule",
                description="Rule to trigger sqs message when there's a rebalance recommendation",
                event_pattern=events.EventPattern(
                    source=["aws.ec2"],
                    detail_type=["EC2 Instance Rebalance Recommendation"],
                )
            ),
            events.Rule(
                self, "ScheduledChangeRule",
                description="Rule to trigger sqs message when there's an aws health event",
                event_pattern=events.EventPattern(
                    source=["aws.ec2"],
                    detail_type=["AWS Health Event"],
                )
            ),
            events.Rule(
                self, "InstanceStateChangeRule",
                description="Rule to trigger sqs message when there's an instance state change",
                event_pattern=events.EventPattern(
                    source=["aws.ec2"],
                    detail_type=["EC2 Instance State-change Notification"],
                )
            ),
        ]
        
        for rule in karpenter_event_bridge_rules:
            rule.add_target(targets.SqsQueue(interruption_queue))
            
        k8s_io_param = cdk.CfnJson(self, "ClusterNameJson", value={ f"aws:ResourceTag/kubernetes.io/cluster/{cluster.cluster_name}": "owned"})
        eksio_param = cdk.CfnJson(self, "EksClusterNameJson", value={ f"aws:RequestTag/eks:cluster-name": {cluster.cluster_name}})
          
        karpenter_controller_iam_policy = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    sid="AllowScopedEC2InstanceAccessActions",
                    actions=["ec2:RunInstances", "ec2:CreateFleet"],
                    resources=[
                        "arn:aws:ec2:*:*:snapshot/*",
                        "arn:aws:ec2:*:*:security-group/*",
                        "arn:aws:ec2:*:*:subnet/*",
                        "arn:aws:ec2:*:*:image/*"
                    ]
                ),
                iam.PolicyStatement(
                    sid="AllowScopedEC2LaunchTemplateAccessActions",
                    actions=["ec2:RunInstances", "ec2:CreateFleet"],
                    resources=["arn:aws:ec2:*:*:launch-template/*"],
                    conditions={
                        "StringEquals":
                            k8s_io_param,
                        "StringLike": {
                            "aws:ResourceTag/karpenter.sh/nodepool": "*"
                        }
                    }
                ),
                iam.PolicyStatement(
                    sid="AllowScopedEC2InstanceActionsWithTags",
                    actions=["ec2:RunInstances", "ec2:CreateFleet", "ec2:CreateLaunchTemplate"],
                    resources=[
                        "arn:aws:ec2:*:*:instance/*",
                        "arn:aws:ec2:*:*:volume/*",
                        "arn:aws:ec2:*:*:network-interface/*",
                        "arn:aws:ec2:*:*:launch-template/*",
                        "arn:aws:ec2:*:*:spot-instances-request/*",
                        "arn:aws:ec2:*:*:fleet/*"
                    ],
                    conditions={
                        "StringEquals":
                            eksio_param,
                        "StringLike": {
                            "aws:RequestTag/karpenter.sh/nodepool": "*"
                        }
                    }
                ),
                iam.PolicyStatement(
										sid="karpenterSQSpermissions",
										actions=["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
										resources=[interruption_queue.queue_arn],
								),
                iam.PolicyStatement(
                    sid="AllowScopedResourceCreationTagging",
                    actions=["ec2:CreateTags"],
                    resources=[
                        "arn:aws:ec2:*:*:instance/*",
                        "arn:aws:ec2:*:*:volume/*",
                        "arn:aws:ec2:*:*:network-interface/*",
                        "arn:aws:ec2:*:*:launch-template/*",
                        "arn:aws:ec2:*:*:spot-instances-request/*",
                        "arn:aws:ec2:*:*:fleet/*"
                    ],
                    conditions={
                        "StringEquals": k8s_io_param,
                        "StringEquals": eksio_param,
                        "StringLike": {
                            "aws:RequestTag/karpenter.sh/nodepool": "*"
                        }
                    }
                ),
                iam.PolicyStatement(
                    sid="AllowScopedResourceTagging",
                    actions=["ec2:CreateTags"],
                    resources=["arn:aws:ec2:*:*:instance/*"],
                ),
                iam.PolicyStatement(
                    sid="AllowScopedEC2LaunchTemplateActions",
                    actions=["ec2:TerminateInstances", "ec2:DeleteLaunchTemplate"],
                    resources=[
                        "arn:aws:ec2:*:*:instance/*",
                        "arn:aws:ec2:*:*:launch-template/*"
                    ]
                ),
                iam.PolicyStatement(
                    sid="AllowRegionalReadActions",
                    actions=[
                        "ec2:DescribeImages",
                        "ec2:DescribeInstances",
                        "ec2:DescribeInstanceTypeOfferings",
                        "ec2:DescribeInstanceTypes",
                        "ec2:DescribeLaunchTemplates",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeSpotPriceHistory",
                        "ec2:DescribeSubnets",
                    ],
                    resources=["*"],
                    conditions={
                        "StringEquals": {
                            "aws:RequestedRegion": cdk.Aws.REGION
                        }
                    }
                ),
                iam.PolicyStatement(
                    sid="AllowSSMReadActions",
                    actions=["ssm:GetParameter"],
                    resources=["arn:aws:ssm:*:*:parameter/aws/service/*"],
                ),
                iam.PolicyStatement(
                    sid="AllowPricingReadActions",
                    actions=["pricing:GetProducts"],
                    resources=["*"]
                ),
                iam.PolicyStatement(
                    sid="AllowPassingInstanceRole",
                    actions=["iam:PassRole"],
                    resources=[karpenter_sa.role.role_arn],
                    conditions={
                        "StringEquals": {
                            "iam:PassedToService": "ec2.amazonaws.com"
                        }
                    }
                ),
                iam.PolicyStatement(
                    sid="AllowScopedInstanceProfileCreationActions",
                    actions=["iam:CreateInstanceProfile"],
                    resources=["arn:aws:ec2:*:*:instance-profile/*"]
                ),
                iam.PolicyStatement(
                    sid="AllowScopedInstanceProfileTagActions",
                    actions=["iam:TagInstanceProfile"],
                    resources=["arn:aws:ec2:*:*:instance-profile/*"]
                ),
            ]
        )

        karpenter_sa.role.attach_inline_policy(iam.Policy(self, "KarpenterInlinePolicy", document=karpenter_controller_iam_policy))

        # karpenter helm chart.
        karpenter_helm = cluster.add_helm_chart("karpenter",
            chart="karpenter",
            repository="https://charts.karpenter.sh",
            namespace="karpenter",
            create_namespace=False,
            values={
                "serviceAccount": {"create": False,"name": karpenter_sa.service_account_name},
                "clusterName": cluster.cluster_name,
                "clusterEndpoint": cluster.cluster_endpoint,
                "aws": {"defaultInstanceProfile": "karpenter-node-instance-profile"}
            }
        )

        karpenter_sa.node.add_dependency(karpenter_namespace)

        # helm chart values
        karpenter_provisioner_file: Path = Path(__file__).resolve().parents[1] / "helm_values/karpenter-provisioner.yaml"
        argocd_helm_values: Path = Path(__file__).resolve().parents[1] / "helm_values/argocd.yaml"
        assert argocd_helm_values.exists()
        argocd_image_updater_helm_values: Path = Path(__file__).resolve().parents[1] / "helm_values/image-updater.yaml"
        assert argocd_image_updater_helm_values.exists()
        
        # with open(str(karpenter_provisioner_file), "r") as f:
        #     yaml_content = f.read()
        # variables = {"CLUSTER_NAME": cluster.cluster_name}
        # template = string.Template(yaml_content)
        # final = template.safe_substitute(variables)
        # with open('erite', "w") as f:
				# 						f.write(final)
        # manifest_list = list(yaml.safe_load_all(yaml_content))
        # final = []
        # for x in manifest_list:
        #     cdk.Fn.sub(yaml.dump(x), {"CLUSTER_NAME": cluster.cluster_name})
        #     final.append(x)
        # Load all YAML documents
        #karpenter_prov_yamls = list(yaml.load_all(karpenter_provisioner_file.open(), Loader=yaml.FullLoader))
        # Ensure all loaded documents are valid Kubernetes manifests
        # manifest_list = [doc for doc in karpenter_prov_yamls if isinstance(doc, dict)]    
        # Create KubernetesManifest
        #eks.KubernetesManifest(self, "karpenter-provisioner", cluster=cluster, manifest=final)

        # argocd helm chart
        argocd_helm = cluster.add_helm_chart("argocd",
            chart="argo-cd",
            repository="https://argoproj.github.io/argo-helm",
            namespace="argocd",
            create_namespace=True,
            values=yaml.safe_load(argocd_helm_values.read_text())
        )

        # argocd_updater_pod_identity_configuration
        argo_image_updater_role = iam.Role(
            self, "ArgoImageUpdaterRole",
            description="argocd image updater role to access ECR",
            assumed_by=iam.ServicePrincipal("pods.eks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly")
            ]
        )

        service_account_argo_image_updater = cluster.add_service_account("my-argo-image-updater", name="argocd-image-updater", namespace="argocd", identity_type=eks.IdentityType.POD_IDENTITY)
        service_account_argo_image_updater.role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly"))

        # argocd image updater helm chart
        argocd_image_updater = cluster.add_helm_chart("argocd-image-updater",
            chart="argocd-image-updater",
            repository="https://argoproj.github.io/argo-helm",
            namespace="argocd",
            create_namespace=False,
            values=yaml.safe_load(argocd_image_updater_helm_values.read_text())
        )

        argocd_image_updater.node.add_dependency(argocd_helm)
        service_account_argo_image_updater.node.add_dependency(argocd_helm)

        cdk.CfnOutput(
            self, "cluster-name",
            value=cluster.cluster_name
        )

        # Tagging the resources               
        cdk.Tags.of(self).add("Project", "EKS")
        cdk.Tags.of(self).add("Owner", "Roger")
        cdk.Tags.of(self).add("Environment", "Test")
        