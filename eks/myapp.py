import os
from pathlib import Path
import yaml
import aws_cdk as cdk
from constructs import Construct
import cdk_ecr_deployment as ecrdeploy


from aws_cdk import aws_ecr as ecr, aws_ecr_assets as ecr_assets

image_tag = os.getenv("IMAGE_TAG", "0.1.0")


class MyappStack(cdk.Stack):
    """Build docker image for my app"""

    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        payments_image = ecr_assets.DockerImageAsset(
            scope=self,
            id="payments-image",
            directory=str(Path(__file__).resolve().parents[1] / "myapp"),
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        users_image = ecr_assets.DockerImageAsset(
            scope=self,
            id="users-image",
            directory=str(Path(__file__).resolve().parents[1] / "myapp"),
            platform=ecr_assets.Platform.LINUX_AMD64,
        )

        # create ecr repo
        repository_uris = []
        ecr_repos = ["payments", "users"]
        for repo in ecr_repos:
            repository = ecr.Repository(
                self,
                repo + "Repository",
                repository_name=repo,
                removal_policy=cdk.RemovalPolicy.DESTROY,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            )
            repository_uris.append(repository.repository_uri)

        ecrdeploy.ECRDeployment(
            scope=self,
            id="paymentsdeployment",
            src=ecrdeploy.DockerImageName(payments_image.image_uri),
            dest=ecrdeploy.DockerImageName(":".join([repository_uris[0], image_tag])),
        )

        ecrdeploy.ECRDeployment(
            scope=self,
            id="usersdeployment",
            src=ecrdeploy.DockerImageName(payments_image.image_uri),
            dest=ecrdeploy.DockerImageName(":".join([repository_uris[1], image_tag])),
        )

        cdk.CfnOutput(
            scope=self,
            id="payments-image-uri",
            value=payments_image.repository.repository_uri,
        )

        cdk.CfnOutput(
            scope=self,
            id="users-image-uri",
            value=users_image.repository.repository_uri,
        )
