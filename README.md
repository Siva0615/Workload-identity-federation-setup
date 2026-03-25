# Workload Identity Federation – GCP Authentication from a Local PC

Connect to Google Cloud Platform (GCP) from your local machine **without** a service account JSON key, using **Workload Identity Federation (WIF)**.

---

## Table of Contents

1. [What is Workload Identity Federation?](#1-what-is-workload-identity-federation)
2. [How It Works (Architecture)](#2-how-it-works-architecture)
3. [Prerequisites](#3-prerequisites)
4. [Step-by-Step GCP Setup Guide](#4-step-by-step-gcp-setup-guide)
   - [Step 1 – Enable required APIs](#step-1--enable-required-apis)
   - [Step 2 – Create a Service Account](#step-2--create-a-service-account)
   - [Step 3 – Create a Workload Identity Pool](#step-3--create-a-workload-identity-pool)
   - [Step 4 – Add a Provider to the Pool](#step-4--add-a-provider-to-the-pool)
   - [Step 5 – Grant the Pool access to the Service Account](#step-5--grant-the-pool-access-to-the-service-account)
   - [Step 6 – Download the Credential Configuration File](#step-6--download-the-credential-configuration-file)
5. [Local Machine Setup](#5-local-machine-setup)
6. [Running the Python Code](#6-running-the-python-code)
7. [Code Overview](#7-code-overview)
8. [Troubleshooting](#8-troubleshooting)
9. [Security Notes](#9-security-notes)

---

## 1. What is Workload Identity Federation?

Workload Identity Federation (WIF) lets external workloads — including applications running on your **local PC**, AWS, Azure, GitHub Actions, GitLab CI, or any OIDC/SAML provider — authenticate to GCP **without a service account key file**.

| Traditional approach | WIF approach |
|---|---|
| Download a JSON key file | No key file needed |
| Key stored on disk (security risk) | Short-lived access tokens only |
| Manual key rotation needed | Tokens auto-expire (1 hour) |
| Risk of key leakage | No long-lived secret to leak |

---

## 2. How It Works (Architecture)

```
Your Local PC
│
│  1. You have an identity token  ──────────────────────────────────────┐
│     (e.g. from `gcloud auth print-access-token` or an OIDC provider)  │
│                                                                        ▼
│                                               ┌─────────────────────────────────┐
│                                               │  GCP Security Token Service     │
│                                               │  (STS)                          │
│  2. Exchange token via STS ─────────────────► │  Validates the external token   │
│                                               │  against the WIF Pool/Provider  │
│                                               └──────────────┬──────────────────┘
│                                                              │ 3. Issues a
│                                                              │    federated token
│                                               ┌─────────────▼──────────────────┐
│                                               │  IAM – Service Account          │
│  4. Impersonate the SA ◄─────────────────────  │  (bound to the WIF pool)       │
│     Get a short-lived                         └─────────────────────────────────┘
│     Google access token
│
│  5. Call GCP APIs using the access token
└──► Cloud Storage / BigQuery / Pub/Sub / etc.
```

The **Credential Configuration File** (a plain JSON file with no secrets) tells the `google-auth` library how to perform steps 1–4 automatically.

---

## 3. Prerequisites

| Requirement | Notes |
|---|---|
| GCP project | [Create one for free](https://console.cloud.google.com/) |
| `gcloud` CLI installed | [Install guide](https://cloud.google.com/sdk/docs/install) |
| Python 3.10+ | [python.org](https://www.python.org/downloads/) |
| Project Owner or Editor role | Needed for initial setup only |

Install the `gcloud` CLI and log in:

```bash
# Install gcloud (Linux/macOS)
curl https://sdk.cloud.google.com | bash
exec -l $SHELL

# Initialize and log in
gcloud init
gcloud auth login
```

---

## 4. Step-by-Step GCP Setup Guide

> **Note:** Replace the placeholders below with your own values:
> - `YOUR_PROJECT_ID` – your GCP project ID (e.g. `my-project-123`)
> - `YOUR_PROJECT_NUMBER` – numeric project number (find it on the GCP Console home page)
> - `YOUR_SA_NAME` – the service account name you will create (e.g. `wif-demo-sa`)
> - `YOUR_POOL_ID` – a short ID for your WIF pool (e.g. `local-dev-pool`)
> - `YOUR_PROVIDER_ID` – a short ID for the provider (e.g. `local-oidc-provider`)

---

### Step 1 – Enable Required APIs

Open [GCP Console](https://console.cloud.google.com/) or run:

```bash
gcloud services enable iam.googleapis.com \
    iamcredentials.googleapis.com \
    sts.googleapis.com \
    cloudresourcemanager.googleapis.com \
    storage.googleapis.com \
    --project=YOUR_PROJECT_ID
```

---

### Step 2 – Create a Service Account

The Service Account represents the GCP identity your code will use.

**Via GCP Console:**
1. Go to **IAM & Admin → Service Accounts**
2. Click **Create Service Account**
3. Fill in:
   - **Service account name:** `wif-demo-sa` (or any name)
   - **Service account ID:** auto-filled
   - **Description:** `WIF demo service account`
4. Click **Create and Continue**
5. Assign roles (for the demo, add **Storage Object Viewer** or **Storage Admin**)
6. Click **Done**

**Via `gcloud`:**
```bash
# Create the service account
gcloud iam service-accounts create YOUR_SA_NAME \
    --display-name="WIF Demo Service Account" \
    --project=YOUR_PROJECT_ID

# Grant it the Storage Admin role (adjust as needed)
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_SA_NAME@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/storage.admin"
```

---

### Step 3 – Create a Workload Identity Pool

A **Pool** is a container that groups external identity providers.

**Via GCP Console:**
1. Go to **IAM & Admin → Workload Identity Federation**
2. Click **Create Pool**
3. Fill in:
   - **Name:** `local-dev-pool`
   - **Pool ID:** `local-dev-pool` (auto-filled, can be customized)
   - **Description:** `Pool for local development machines`
4. Ensure **Enabled** is toggled on
5. Click **Continue**

**Via `gcloud`:**
```bash
gcloud iam workload-identity-pools create YOUR_POOL_ID \
    --location="global" \
    --display-name="Local Dev Pool" \
    --description="Pool for local development machines" \
    --project=YOUR_PROJECT_ID
```

---

### Step 4 – Add a Provider to the Pool

A **Provider** defines *which* external tokens are trusted. For a local PC the simplest option is to use **gcloud's own identity token** as the credential source.

#### Option A – Use `gcloud` user credentials (recommended for local dev)

This uses your logged-in `gcloud` identity. The provider type is **OIDC** and the issuer is Google's own token endpoint.

**Via GCP Console:**
1. Inside the pool you just created, click **Add Provider**
2. Select **OpenID Connect (OIDC)**
3. Fill in:
   - **Provider name:** `gcloud-oidc`
   - **Provider ID:** `gcloud-oidc`
   - **Issuer (URL):** `https://accounts.google.com`
4. Click **Continue**
5. Under **Attribute Mapping**, add:
   - `google.subject` → `assertion.sub`
   - `attribute.email` → `assertion.email`
6. Under **Attribute Conditions**, add (replace with your Google email):
   ```
   attribute.email == "you@gmail.com"
   ```
   This restricts access to **only your Google account**.
7. Click **Save**

**Via `gcloud`:**
```bash
gcloud iam workload-identity-pools providers create-oidc YOUR_PROVIDER_ID \
    --workload-identity-pool=YOUR_POOL_ID \
    --location="global" \
    --issuer-uri="https://accounts.google.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.email=assertion.email" \
    --attribute-condition="attribute.email == 'you@gmail.com'" \
    --project=YOUR_PROJECT_ID
```

---

### Step 5 – Grant the Pool Access to the Service Account

This binds the WIF pool (and your identity within it) to the service account so it can be impersonated.

**Via GCP Console:**
1. Go to **IAM & Admin → Service Accounts**
2. Click your service account (`wif-demo-sa@...`)
3. Go to the **Permissions** tab
4. Click **Grant Access**
5. In the **New principals** field, paste:
   ```
   principalSet://iam.googleapis.com/projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/YOUR_POOL_ID/attribute.email/you@gmail.com
   ```
6. Assign the role: **Workload Identity User** (`roles/iam.workloadIdentityUser`)
7. Click **Save**

**Via `gcloud`:**
```bash
gcloud iam service-accounts add-iam-policy-binding \
    YOUR_SA_NAME@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --member="principalSet://iam.googleapis.com/projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/YOUR_POOL_ID/attribute.email/you@gmail.com" \
    --role="roles/iam.workloadIdentityUser" \
    --project=YOUR_PROJECT_ID
```

> **Finding your Project Number:**
> ```bash
> gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)"
> ```

---

### Step 6 – Download the Credential Configuration File

This file tells `google-auth` how to exchange your local token for a GCP access token. **It contains no secrets or private keys.**

**Via GCP Console:**
1. Go to **IAM & Admin → Workload Identity Federation**
2. Click your pool (`local-dev-pool`)
3. Click **Connected Service Accounts** tab
4. Next to your service account, click **Download config**
5. Save the file as `credential-config.json` in your project directory

**Via `gcloud`:**
```bash
gcloud iam workload-identity-pools create-cred-config \
    "projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/YOUR_POOL_ID/providers/YOUR_PROVIDER_ID" \
    --service-account="YOUR_SA_NAME@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
    --service-account-token-lifetime-seconds=3600 \
    --output-file=credential-config.json \
    --credential-source-command="gcloud auth print-access-token --audiences=https://iam.googleapis.com/projects/YOUR_PROJECT_NUMBER/locations/global/workloadIdentityPools/YOUR_POOL_ID/providers/YOUR_PROVIDER_ID" \
    --executable-timeout-millis=5000 \
    --executable-output-format=token \
    --project=YOUR_PROJECT_ID
```

> **Important:** Add `credential-config.json` to `.gitignore` to avoid accidentally committing it (even though it has no secrets, it's specific to your setup).

---

## 5. Local Machine Setup

### 1. Clone the repository

```bash
git clone https://github.com/Siva0615/Workload-identity-federation-setup.git
cd Workload-identity-federation-setup
```

### 2. Create a virtual environment

```bash
# Create
python -m venv .venv

# Activate (Linux / macOS)
source .venv/bin/activate

# Activate (Windows PowerShell)
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set environment variables

**Linux / macOS:**
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/absolute/path/to/credential-config.json"
export GCP_PROJECT_ID="YOUR_PROJECT_ID"
```

**Windows (PowerShell):**
```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\absolute\path\to\credential-config.json"
$env:GCP_PROJECT_ID = "YOUR_PROJECT_ID"
```

**Windows (Command Prompt):**
```cmd
set GOOGLE_APPLICATION_CREDENTIALS=C:\absolute\path\to\credential-config.json
set GCP_PROJECT_ID=YOUR_PROJECT_ID
```

---

## 6. Running the Python Code

```bash
python main.py
```

**Expected output:**
```
============================================================
  GCP Workload Identity Federation – local authentication
============================================================

[1] Trying Application Default Credentials (ADC) …
[ADC] Authenticated successfully.
[ADC] Access token (first 20 chars): ya29.c.c0ASRK0Gb...

Cloud Storage buckets in project 'YOUR_PROJECT_ID':
  • my-bucket-1
  • my-bucket-2
```

---

## 7. Code Overview

| Function | Description |
|---|---|
| `authenticate_with_adc()` | Uses `google.auth.default()` – reads `GOOGLE_APPLICATION_CREDENTIALS` automatically. Recommended for most cases. |
| `authenticate_with_wif_config(path)` | Explicitly loads a WIF credential config JSON file. Useful when you want to specify the path in code. |
| `list_gcs_buckets(credentials, project)` | Sample GCP API call: lists all Cloud Storage buckets to verify authentication works. |
| `main()` | Entry point. Tries ADC first, falls back to explicit config if ADC fails. |

### Credential configuration file structure

```json
{
  "type": "external_account",
  "audience": "//iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/providers/PROVIDER_ID",
  "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
  "token_url": "https://sts.googleapis.com/v1/token",
  "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/SA@PROJECT.iam.gserviceaccount.com:generateAccessToken",
  "credential_source": {
    "executable": {
      "command": "gcloud auth print-access-token --audiences=...",
      "timeout_millis": 5000,
      "output_format": { "type": "token" }
    }
  }
}
```

---

## 8. Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| `google.auth.exceptions.DefaultCredentialsError` | `GOOGLE_APPLICATION_CREDENTIALS` not set or wrong path | Set the env var to the full path of `credential-config.json` |
| `403 Permission denied` | Pool not granted `roles/iam.workloadIdentityUser` on the SA | Re-do Step 5 |
| `invalid_grant` or `Token has been expired` | `gcloud` token expired | Run `gcloud auth login` again |
| `The caller does not have permission` | SA doesn't have the right GCP role | Add the needed role to the SA (Step 2) |
| `credential-config.json: No such file` | Config file not downloaded | Re-do Step 6 |
| `executable not found` | `gcloud` not in PATH | Add `gcloud` to your system PATH |

---

## 9. Security Notes

- ✅ **No service account key file** – WIF uses short-lived tokens (1 hour max)
- ✅ **Credential config file is not a secret** – it contains no private keys
- ✅ **Attribute conditions** restrict which identities can authenticate
- ⚠️ **Add `credential-config.json` to `.gitignore`** – while it has no secrets, committing it unnecessarily exposes your project/pool IDs
- ⚠️ **Never commit `.json` key files** – if you accidentally created one, [rotate it immediately](https://cloud.google.com/iam/docs/keys-disable-enable)

```bash
# Add to .gitignore
echo "credential-config.json" >> .gitignore
echo ".venv/" >> .gitignore
```

---

## References

- [Google Cloud – Workload Identity Federation overview](https://cloud.google.com/iam/docs/workload-identity-federation)
- [google-auth Python library](https://googleapis.dev/python/google-auth/latest/index.html)
- [gcloud CLI reference – workload-identity-pools](https://cloud.google.com/sdk/gcloud/reference/iam/workload-identity-pools)
- [Best practices for WIF](https://cloud.google.com/iam/docs/workload-identity-federation-best-practices)
