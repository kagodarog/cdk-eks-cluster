#!/usr/bin/env python3
import os

import aws_cdk as cdk

from eks.eks_stack import EksStack
from eks.myapp import MyappStack




app = cdk.App()
EksStack(app, "EksStack")
MyappStack(app, "myapps-docker", env=cdk.Environment(account=os.getenv('CDK_DEFAULT_ACCOUNT'), region=os.getenv('CDK_DEFAULT_REGION')))
app.synth()
