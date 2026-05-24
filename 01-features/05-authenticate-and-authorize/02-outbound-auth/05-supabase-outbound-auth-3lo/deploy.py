#!/usr/bin/env python3
"""
deploy.py — Deploy AgentCore agent with Supabase inbound auth + Google Calendar 3LO

Steps:
  1. Validate configuration from .env files
  2. Create / update the Google OAuth2 credential provider in AgentCore Identity
  3. Build and deploy the AgentCore agent container with Supabase JWT authorizer
  4. Register the Node.js client callback URL with the agent's workload identity
  5. Attach required IAM policies to the execution role
  6. Wait for the agent endpoint to reach READY status
  7. Print a summary with the agent ARN to paste into client/index.js

Usage:
  python deploy.py

Prerequisites:
  - AWS credentials configured (aws configure or environment variables)
  - Docker running (required for container build)
  - .env (this directory) populated with SUPABASE_URL, SUPABASE_ANON_KEY,
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SUPABASE_CLIENT_ID, CALLBACK_URL
  - pip install -r requirements.txt
"""

import os
import sys
import time
import json

import boto3
import dotenv
import botocore.exceptions
from boto3.session import Session

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Agent source directory is the same as this script's directory
AGENT_SOURCE_DIR = SCRIPT_DIR

# Agent .env (same directory as this script)
AGENT_ENV = os.path.join(SCRIPT_DIR, ".env")


# ── Deployment constants ──────────────────────────────────────────────────────

AGENT_NAME    = "strands_agent_supabase_3lo"
ENTRYPOINT    = "strands_claude_google_3lo.py"
REQUIREMENTS  = "requirements.txt"
PROVIDER_NAME = "google-cal-provider"


# ── Helpers ───────────────────────────────────────────────────────────────────

def step(n, total, msg):
    print(f"\n[{n}/{total}] {msg}")


def require_env(name, env_path):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: {name} is not set in {env_path}")
        sys.exit(1)
    return v


def provider_exists(client, name):
    try:
        client.get_oauth2_credential_provider(name=name)
        return True
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "404"):
            return False
        raise


# ── Load and validate configuration ──────────────────────────────────────────

print("Loading configuration...")

# Load agent .env first (Supabase URL, Google credentials)
dotenv.load_dotenv(AGENT_ENV, override=True)

SUPABASE_URL         = require_env("SUPABASE_URL", AGENT_ENV)
SUPABASE_ANON_KEY    = require_env("SUPABASE_ANON_KEY", AGENT_ENV)
GOOGLE_CLIENT_ID     = require_env("GOOGLE_CLIENT_ID", AGENT_ENV)
GOOGLE_CLIENT_SECRET = require_env("GOOGLE_CLIENT_SECRET", AGENT_ENV)

SUPABASE_CLIENT_ID = os.environ.get("SUPABASE_CLIENT_ID", "").strip()
if SUPABASE_CLIENT_ID:
    print(f"  Supabase OAuth client ID: {SUPABASE_CLIENT_ID}")
else:
    print("  WARNING: SUPABASE_CLIENT_ID not set — only 'authenticated' audience will be allowed")

NODE_CALLBACK_URL = require_env("CALLBACK_URL", AGENT_ENV)

# ── AWS session ───────────────────────────────────────────────────────────────

boto_session      = Session()
region            = boto_session.region_name
account           = boto_session.client("sts").get_caller_identity()["Account"]
identity_client   = boto_session.client("bedrock-agentcore-control")
agentcore_control = boto3.client("bedrock-agentcore-control", region_name=region)
iam_client        = boto3.client("iam", region_name=region)

print(f"  AWS account: {account}")
print(f"  AWS region:  {region}")

TOTAL_STEPS = 5

# ── Step 1: Google OAuth2 credential provider ─────────────────────────────────

step(1, TOTAL_STEPS, "Google OAuth2 credential provider")

provider_config = dict(
    name=PROVIDER_NAME,
    credentialProviderVendor="GoogleOauth2",
    oauth2ProviderConfigInput={
        "googleOauth2ProviderConfig": {
            "clientId": GOOGLE_CLIENT_ID,
            "clientSecret": GOOGLE_CLIENT_SECRET,
        }
    },
)

if provider_exists(identity_client, PROVIDER_NAME):
    print(f"  Provider '{PROVIDER_NAME}' exists — updating credentials...")
    provider = identity_client.update_oauth2_credential_provider(**provider_config)
else:
    print(f"  Creating provider '{PROVIDER_NAME}'...")
    provider = identity_client.create_oauth2_credential_provider(**provider_config)

agentcore_callback_url = provider.get("callbackUrl", "")
print(f"\n  AgentCore callback URL (Google redirect URI):")
print(f"  {agentcore_callback_url}")
print()
print("  *** ACTION REQUIRED ***")
print("  Add the URL above to your Google OAuth2 app:")
print("  Google Console → APIs & Services → Credentials →")
print("  [your OAuth 2.0 Client] → Authorised redirect URIs → Add URI")
print()
input("  Press Enter once you have saved it in Google Console...")

# ── Step 2: Deploy AgentCore agent ───────────────────────────────────────────

step(2, TOTAL_STEPS, "Building and deploying AgentCore agent container")

try:
    from bedrock_agentcore_starter_toolkit import Runtime
except ImportError:
    print("  ERROR: bedrock_agentcore_starter_toolkit is not installed.")
    print(f"  Run: pip install -r {REQUIREMENTS}")
    sys.exit(1)

os.chdir(AGENT_SOURCE_DIR)

supabase_discovery_url = SUPABASE_URL.rstrip("/") + "/auth/v1/.well-known/openid-configuration"

# allowedClients covers both Supabase auth flows:
#   "authenticated" — aud claim on email/password JWTs (signInWithPassword)
#   SUPABASE_CLIENT_ID — client_id claim on OAuth-server-flow JWTs
allowed_clients = ["authenticated"]
if SUPABASE_CLIENT_ID:
    allowed_clients.append(SUPABASE_CLIENT_ID)

runtime = Runtime()
runtime.configure(
    entrypoint=ENTRYPOINT,
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file=REQUIREMENTS,
    region=region,
    memory_mode="NO_MEMORY",
    agent_name=AGENT_NAME,
    authorizer_configuration={
        "customJWTAuthorizer": {
            "discoveryUrl": supabase_discovery_url,
            "allowedClients": allowed_clients,
        }
    },
)

print(f"  Launching '{AGENT_NAME}' (builds + pushes Docker image — takes several minutes)...")

launch_result = runtime.launch(
    env_vars={"CALLBACK_URL": NODE_CALLBACK_URL},
    auto_update_on_conflict=True,
)

agent_id  = launch_result.agent_id
agent_arn = launch_result.agent_arn
print(f"  Agent ID:  {agent_id}")
print(f"  Agent ARN: {agent_arn}")

# ── Step 3: Workload identity — register Node.js callback URL ─────────────────

step(3, TOTAL_STEPS, "Registering Node.js callback URL with workload identity")

workload_identity = identity_client.get_workload_identity(name=agent_id)
existing_urls     = workload_identity.get("allowedResourceOauth2ReturnUrls") or []

if NODE_CALLBACK_URL not in existing_urls:
    identity_client.update_workload_identity(
        name=agent_id,
        allowedResourceOauth2ReturnUrls=[*existing_urls, NODE_CALLBACK_URL],
    )
    print(f"  Registered: {NODE_CALLBACK_URL}")
else:
    print(f"  Already registered: {NODE_CALLBACK_URL}")

# ── Step 4: IAM policies ──────────────────────────────────────────────────────

step(4, TOTAL_STEPS, "Attaching IAM policies to execution role")

runtime_info = agentcore_control.get_agent_runtime(agentRuntimeId=agent_id)
role_arn     = runtime_info["roleArn"]
role_name    = role_arn.split("/")[-1]

inline_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "Oauth2TokenAccess",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:GetResourceOauth2Token"],
            "Resource": "*",
        },
        {
            "Sid": "SecretsManagerAccess",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [
                f"arn:aws:secretsmanager:{region}:{account}:secret:"
                f"bedrock-agentcore-identity!default/oauth2/{PROVIDER_NAME}*"
            ],
        },
    ],
}

iam_client.put_role_policy(
    RoleName=role_name,
    PolicyName="agentcore_outbound_oauth2",
    PolicyDocument=json.dumps(inline_policy),
)
print(f"  Policies attached to role: {role_name}")

# ── Step 5: Wait for READY ────────────────────────────────────────────────────

step(5, TOTAL_STEPS, "Waiting for agent endpoint to reach READY status")

terminal    = {"READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"}
last_status = None

while True:
    status_resp = runtime.status()
    status      = status_resp.endpoint["status"]
    if status != last_status:
        print(f"  {status}")
        last_status = status
    if status in terminal:
        break
    time.sleep(15)

if status != "READY":
    print(f"\nERROR: Agent deployment ended with status '{status}'")
    sys.exit(1)

# ── Update agent .env with CALLBACK_URL (idempotent) ─────────────────────────

env_lines    = open(AGENT_ENV).readlines() if os.path.exists(AGENT_ENV) else []
has_callback = any(line.startswith("CALLBACK_URL=") for line in env_lines)
if not has_callback:
    with open(AGENT_ENV, "a") as f:
        f.write(f'\nCALLBACK_URL="{NODE_CALLBACK_URL}"\n')
    print(f"\n  Written CALLBACK_URL to {AGENT_ENV}")

# ── Summary ───────────────────────────────────────────────────────────────────

separator = "─" * 64
print(f"""
{separator}
  Deployment complete
{separator}
  Agent ARN : {agent_arn}
  Agent ID  : {agent_id}
  Region    : {region}
  Callback  : {NODE_CALLBACK_URL}
{separator}
  Update client/index.js — replace AGENTCORE_ARN with:
  "{agent_arn}"
{separator}
""")
