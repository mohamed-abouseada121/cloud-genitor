"""
utils/credentials.py
────────────────────
Unified credential loading and validation for all five cloud providers.
Secrets are NEVER stored here — only profile names / config file paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from models.resource import ProviderName


# ── Credential containers ─────────────────────────────────────────────────────

@dataclass
class AWSCredentials:
    profile_name: str = "default"
    region: str = "us-east-1"
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    # Optional role assumption
    role_arn: Optional[str] = None
    external_id: Optional[str] = None


@dataclass
class AzureCredentials:
    subscription_id: str = ""
    tenant_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


@dataclass
class AlibabaCredentials:
    access_key_id: str = ""
    access_key_secret: str = ""
    region_id: str = "me-central-1"


@dataclass
class GCPCredentials:
    project_id: str = ""
    # Path to service-account JSON key file (empty = use ADC)
    key_file: str = ""


@dataclass
class OracleCredentials:
    config_file: str = "~/.oci/config"
    profile_name: str = "DEFAULT"


# ── Loader ────────────────────────────────────────────────────────────────────

class CredentialManager:
    """
    Loads and validates credentials for a given provider.

    Typical usage
    -------------
    mgr = CredentialManager()
    creds = mgr.load(ProviderName.AWS, {"profile_name": "prod"})
    session = mgr.get_aws_session(creds)
    """

    # ── AWS ──────────────────────────────────────────────────────────────────

    def load_aws(self, profile_name: str = "default", region: str = "us-east-1",
                 role_arn: Optional[str] = None) -> AWSCredentials:
        return AWSCredentials(profile_name=profile_name, region=region, role_arn=role_arn)

    def get_aws_session(self, creds: AWSCredentials) -> Any:
        import boto3

        if creds.access_key_id and creds.secret_access_key:
            session = boto3.Session(
                aws_access_key_id=creds.access_key_id,
                aws_secret_access_key=creds.secret_access_key,
                region_name=creds.region,
            )
        else:
            session = boto3.Session(
                profile_name=creds.profile_name,
                region_name=creds.region,
            )
        if creds.role_arn:
            sts = session.client("sts")
            kwargs: dict[str, Any] = {
                "RoleArn": creds.role_arn,
                "RoleSessionName": "cloud-janitor",
            }
            if creds.external_id:
                kwargs["ExternalId"] = creds.external_id
            assumed = sts.assume_role(**kwargs)
            creds_info = assumed["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds_info["AccessKeyId"],
                aws_secret_access_key=creds_info["SecretAccessKey"],
                aws_session_token=creds_info["SessionToken"],
                region_name=creds.region,
            )
        return session

    # ── Azure ─────────────────────────────────────────────────────────────────

    def load_azure(self, subscription_id: str) -> AzureCredentials:
        return AzureCredentials(subscription_id=subscription_id)

    def get_azure_credential(self, creds: AzureCredentials) -> Any:
        from azure.identity import DefaultAzureCredential, ClientSecretCredential  # type: ignore[import]
        if creds.tenant_id and creds.client_id and creds.client_secret:
            return ClientSecretCredential(
                tenant_id=creds.tenant_id,
                client_id=creds.client_id,
                client_secret=creds.client_secret
            )
        return DefaultAzureCredential()

    # ── Alibaba ───────────────────────────────────────────────────────────────

    def load_alibaba(self, access_key_id: str, access_key_secret: str,
                     region_id: str = "me-central-1") -> AlibabaCredentials:
        return AlibabaCredentials(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            region_id=region_id,
        )

    def get_alibaba_config(self, creds: AlibabaCredentials) -> Any:
        from alibabacloud_tea_openapi import models as open_api_models  # type: ignore[import]
        config = open_api_models.Config(
            access_key_id=creds.access_key_id,
            access_key_secret=creds.access_key_secret,
            region_id=creds.region_id,
        )
        return config

    # ── GCP ───────────────────────────────────────────────────────────────────

    def load_gcp(self, project_id: str, key_file: str = "") -> GCPCredentials:
        return GCPCredentials(project_id=project_id, key_file=key_file)

    def get_gcp_credentials(self, creds: GCPCredentials) -> Any:
        import google.auth  # type: ignore[import]
        import google.oauth2.service_account as sa  # type: ignore[import]

        if creds.key_file and os.path.isfile(creds.key_file):
            return sa.Credentials.from_service_account_file(
                creds.key_file,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        google_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return google_creds

    # ── Oracle ────────────────────────────────────────────────────────────────

    def load_oracle(self, config_file: str = "~/.oci/config",
                    profile: str = "DEFAULT") -> OracleCredentials:
        return OracleCredentials(config_file=config_file, profile_name=profile)

    def get_oci_config(self, creds: OracleCredentials) -> dict:
        import oci  # type: ignore[import]
        return oci.config.from_file(
            file_location=os.path.expanduser(creds.config_file),
            profile_name=creds.profile_name,
        )
