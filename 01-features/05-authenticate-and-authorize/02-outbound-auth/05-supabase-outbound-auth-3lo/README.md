# Outbound Auth with Supabase Inbound Auth + Google OAuth2 3LO

| Information         | Details                                                                                          |
|:--------------------|:-------------------------------------------------------------------------------------------------|
| Tutorial type       | Conversational                                                                                   |
| Agent type          | Single                                                                                           |
| Agentic Framework   | Strands Agents                                                                                   |
| LLM model           | Anthropic Claude Haiku 4.5                                                                       |
| Tutorial components | AgentCore runtime, Supabase JWT inbound auth, Outbound Auth, GoogleOauth2 Credential Provider   |
| Example complexity  | Medium                                                                                           |

## Overview

This tutorial combines two authentication concerns in one agent:

- **Inbound auth (who can call the agent)**: Supabase JWT — callers must supply a Supabase access
  token as a Bearer token. AgentCore validates the token against Supabase's OIDC discovery URL.
- **Outbound auth (what the agent can access)**: Google Calendar via 3-Legged OAuth (3LO /
  USER_FEDERATION) — the agent accesses Google Calendar on behalf of the authenticated user.

On the first invocation the agent returns a Google authorization URL. The user grants consent once;
AgentCore Identity then stores and automatically refreshes the Google access token on every
subsequent call.

## Architecture

```
User (Supabase JWT) ──► AgentCore runtime ──► Strands Agent
                               │                    │
            customJWTAuthorizer│                    │ @requires_access_token
            (Supabase OIDC)    │                    │  auth_flow="USER_FEDERATION"
                               ▼                    ▼
                         Token validated      AgentCore Identity
                                              GetResourceOauth2Token
                                                     │
                               ┌─────────────────────┴─────────────────────┐
                               │ (first call)                               │ (subsequent calls)
                               ▼                                            ▼
                      Returns auth URL                         Returns cached access token
                               │                                            │
                               ▼                                            ▼
                   User opens URL → Google OAuth2             Agent calls Google Calendar API
                   grants consent
                               │
                               ▼
                   oauth2_callback_server.py :9090
                   CompleteResourceTokenAuth
                               │
                               ▼
                   AgentCore Identity stores token
```

## Files

| File | Description |
|:-----|:------------|
| `deploy.py` | One-shot deployment script — creates the Google credential provider, builds and deploys the container, registers the callback URL, attaches IAM policies |
| `strands_claude_google_3lo.py` | Agent code deployed to AgentCore runtime |
| `oauth2_callback_server.py` | Local FastAPI server (port 9090) for OAuth2 session binding |
| `chatbot_app_supabase.py` | Streamlit chat UI with Supabase sign-in |
| `supabase_auth_utils.py` | Supabase auth helpers (`setup_supabase_auth`, `reauthenticate_supabase_user`) |
| `runtime_with_strands_and_supabase_3lo.ipynb` | Jupyter notebook walkthrough |
| `requirements.txt` | Python dependencies |

## Prerequisites

### AWS

- AWS CLI configured with credentials
- Required AWS permissions:
  - `bedrock-agentcore:*`
  - `iam:PutRolePolicy` (to attach the outbound-auth inline policy)
  - `bedrock-agentcore:GetResourceOauth2Token`
  - `secretsmanager:GetSecretValue` on `bedrock-agentcore*`
- Docker running (needed for the container build)

### Supabase

1. Create a project at [supabase.com](https://supabase.com)
2. **Change the JWT signing algorithm to RS256 or ES256** — the default HS256 is symmetric and
   cannot be validated by AgentCore's `customJWTAuthorizer`, which fetches public keys from the
   JWKS endpoint:
   > Project Settings → API → JWT Settings → JWT Algorithm → RS256 (or ES256)
3. Create at least one Auth user:
   > Authentication → Users → Add user (or use the sign-up flow and confirm the email)
4. Note your **Project URL** and **anon public key** from:
   > Project Settings → API

### Google

1. Go to [Google Developer Console](https://console.developers.google.com/)
2. Create a project and enable the **Google Calendar API**
3. Configure the OAuth consent screen (External audience; add your email as a test user)
4. Create **OAuth 2.0 credentials** → Web application
5. Note the **Client ID** and **Client Secret** — you will add the AgentCore redirect URI in the
   next step

## Setup

```bash
cd 01-features/05-authenticate-and-authorize/02-outbound-auth/05-supabase-outbound-auth-3lo/

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

## Configuration

Create a `.env` file in this directory:

```bash
cat > .env << 'EOF'
GOOGLE_CLIENT_ID="your-google-client-id"
GOOGLE_CLIENT_SECRET="your-google-client-secret"

SUPABASE_URL="https://<ref>.supabase.co"
SUPABASE_ANON_KEY="your-supabase-anon-key"

# Optional — only needed if you use Supabase OAuth server-flow (social login).
# Found in: Authentication → URL Configuration → client_id claim.
# Leave blank if you only use email/password (signInWithPassword).
SUPABASE_CLIENT_ID=""

# The URL your browser can reach for the local OAuth2 callback server.
# Local default: http://localhost:9090/oauth2/callback
# SageMaker Studio: the proxy URL is detected automatically.
CALLBACK_URL="http://localhost:9090/oauth2/callback"
EOF
```

## Deploying

`deploy.py` performs all deployment steps in one run:

1. Creates (or updates) the `google-cal-provider` OAuth2 credential provider
2. Prints the AgentCore redirect URI — **you must add this to Google Console before continuing**
3. Builds the agent Docker image and deploys it to AgentCore runtime with a Supabase JWT authorizer
4. Registers the callback URL with the agent's workload identity
5. Attaches the required IAM inline policy to the execution role
6. Waits for the agent endpoint to reach `READY` status

```bash
python deploy.py
```

During step 1 the script will pause and print a URL similar to:

```
AgentCore callback URL (Google redirect URI):
  https://bedrock-agentcore.<region>.amazonaws.com/identities/oauth2/callback/...

*** ACTION REQUIRED ***
Add the URL above to your Google OAuth2 app:
Google Console → APIs & Services → Credentials →
[your OAuth 2.0 Client] → Authorised redirect URIs → Add URI

Press Enter once you have saved it in Google Console...
```

Add the URL, save in Google Console, then press Enter.

## Running the Chat App

Open two terminals:

```bash
# Terminal 1 — OAuth2 callback server (handles Google consent redirect)
python oauth2_callback_server.py --region us-west-2

# Terminal 2 — Streamlit UI
streamlit run chatbot_app_supabase.py
```

Open `http://localhost:8501` in your browser.

1. Sign in with the email and password of a Supabase Auth user
2. Ask: **"What is in my agenda for today?"**
3. On first use the agent returns a Google authorization URL — click it to grant calendar access
4. On subsequent messages the agent retrieves your events directly

## What to Expect

```
Loading configuration...
  Supabase OAuth client ID: ...
  AWS account: 123456789012
  AWS region:  us-west-2

[1/5] Google OAuth2 credential provider
  Creating provider 'google-cal-provider'...

  AgentCore callback URL (Google redirect URI):
  https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/callback/...

  *** ACTION REQUIRED ***
  ...
  Press Enter once you have saved it in Google Console...

[2/5] Building and deploying AgentCore agent container
  Launching 'strands_agent_supabase_3lo' (builds + pushes Docker image — takes several minutes)...
  Agent ID:  strands_agent_supabase_3lo-xxxxxxxx
  Agent ARN: arn:aws:bedrock-agentcore:us-west-2:...

[3/5] Registering Node.js callback URL with workload identity
  Registered: http://localhost:9090/oauth2/callback

[4/5] Attaching IAM policies to execution role
  Policies attached to role: AmazonBedrockAgentCoreSDKRuntime-...

[5/5] Waiting for agent endpoint to reach READY status
  CREATING
  READY

────────────────────────────────────────────────────────────────
  Deployment complete
────────────────────────────────────────────────────────────────
  Agent ARN : arn:aws:bedrock-agentcore:us-west-2:...:runtime/...
  ...
```

## Key Concepts

- **Supabase customJWTAuthorizer**: AgentCore fetches the public JWKS from
  `<SUPABASE_URL>/auth/v1/.well-known/openid-configuration` to validate incoming JWTs. The JWT
  algorithm **must** be RS256 or ES256 — HS256 (Supabase default) will be rejected.
- **allowedClients**: Set to `["authenticated"]` for email/password sign-in tokens, plus the
  `SUPABASE_CLIENT_ID` if you also support social login via Supabase OAuth server flow.
- **GoogleOauth2 credential provider**: Pre-configured Google OAuth2 endpoints. You supply only
  your app's `clientId` and `clientSecret`.
- **USER_FEDERATION (3LO)**: On first access the agent returns an authorization URL. The user
  grants consent once; AgentCore Identity stores and refreshes the token automatically.
- **Session binding**: `oauth2_callback_server.py` calls `CompleteResourceTokenAuth` after the
  user grants consent, binding the Google token to the Supabase user identity.

## Troubleshooting

### `redirect_uri_mismatch` error from Google

The AgentCore callback URL was not added to **Authorized redirect URIs** in Google Console, or was
added with a typo. Copy the URL printed by `deploy.py` exactly and add it to the OAuth 2.0
client's redirect URI list.

### Agent returns authorization URL on every call

The OAuth2 session binding did not complete. Ensure `oauth2_callback_server.py` is running on
port 9090 before invoking the agent, and that the `CALLBACK_URL` in `.env` matches the URL
registered in the workload identity.

### Supabase JWT rejected (401 Unauthorized)

Verify the JWT algorithm is RS256 or ES256 in Supabase project settings. The default HS256 cannot
be verified by AgentCore's public-key-based authorizer.

### Token expired during testing

Supabase access tokens expire after ~1 hour. Sign out and sign back in in the Streamlit app to
obtain a fresh token.

### Port 9090 already in use

Stop the conflicting process, or update `CALLBACK_URL` in `.env` and rerun `deploy.py` to
re-register the new port with the workload identity.

## Clean Up

```bash
python -c "
import boto3, yaml

# Delete the Google credential provider
control = boto3.client('bedrock-agentcore-control')
control.delete_oauth2_credential_provider(name='google-cal-provider')
print('Google credential provider deleted')

# Delete the agent runtime
config = yaml.safe_load(open('.bedrock_agentcore.yaml'))
agent_id = config['agents']['strands_agent_supabase_3lo']['bedrock_agentcore']['agent_id']
control.delete_agent_runtime(agentRuntimeId=agent_id)
print(f'Agent runtime {agent_id} deleted')
"
```
