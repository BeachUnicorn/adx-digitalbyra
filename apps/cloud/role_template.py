"""
Läsrollen som skapas i KUNDENS konto. Två vägar till samma roll: en
CloudFormation-mall (ladda upp i konsolen) och ett AWS CLI-kommando.

Rollen litar på byråns konto och kräver kontots ExternalId. Rättigheterna är
en uttrycklig lista med läsoperationer - inte AWS färdiga ReadOnlyAccess,
som också ger läsning av innehåll (S3-objekt, databassnapshots, hemligheter
i Parameter Store). Vi ska se ATT saker finns, inte vad som står i dem.
"""

import json

from django.conf import settings

READ_ACTIONS = [
    "invoicing:ListInvoiceSummaries",
    "invoicing:GetInvoicePDF",
    "ce:GetCostAndUsage",
    "ce:GetCostForecast",
    "ec2:DescribeInstances",
    "ec2:DescribeSnapshots",
    "ec2:DescribeSecurityGroups",
    "rds:DescribeDBInstances",
    "s3:ListAllMyBuckets",
    "route53domains:ListDomains",
    "iam:GetAccountSummary",
    "iam:ListUsers",
    "iam:ListAccessKeys",
]


def trust_policy(account):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{settings.ADX_AWS_ACCOUNT_ID}:root"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": account.external_id}},
            }
        ],
    }


def permissions_policy():
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": READ_ACTIONS, "Resource": "*"}],
    }


def cloudformation(account):
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "ADX lasroll: fakturor, kostnad och resurslistor. Ingen skrivatkomst.",
        "Resources": {
            "AdxReadOnlyRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": account.role_name,
                    "MaxSessionDuration": 3600,
                    "AssumeRolePolicyDocument": trust_policy(account),
                    "Policies": [
                        {"PolicyName": "adx-read", "PolicyDocument": permissions_policy()}
                    ],
                },
            }
        },
        "Outputs": {"RoleArn": {"Value": {"Fn::GetAtt": ["AdxReadOnlyRole", "Arn"]}}},
    }
    return json.dumps(template, indent=2)


def cli_commands(account, profile="<kundens-profil>"):
    trust = json.dumps(trust_policy(account), separators=(",", ":"))
    perms = json.dumps(permissions_policy(), separators=(",", ":"))
    return (
        f"aws iam create-role --profile {profile} --role-name {account.role_name} \\\n"
        f"  --assume-role-policy-document '{trust}'\n"
        f"aws iam put-role-policy --profile {profile} --role-name {account.role_name} \\\n"
        f"  --policy-name adx-read --policy-document '{perms}'"
    )
