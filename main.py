"""
Connect to GCP using Workload Identity Federation (WIF) from a local PC.
No service account JSON key is used.

=== HOW IT WORKS ===
Workload Identity Federation lets an external identity (e.g. an OIDC token, AWS
credentials, Azure managed identity, or a locally-generated token) impersonate a
GCP service account without ever downloading a long-lived key file.

=== ONE-TIME GCP SETUP ===
1. Create a Workload Identity Pool & Provider in the GCP Console:
     IAM & Admin → Workload Identity Federation → Create Pool
     Add a Provider (OIDC / AWS / Azure / Executable-sourced / File-sourced)

2. Grant the pool access to a service account:
     IAM & Admin → Service Accounts → <your SA> → Permissions
     Add the principal:  principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/
                         locations/global/workloadIdentityPools/<POOL_ID>/*
     Role: roles/iam.workloadIdentityUser  (and any other roles the SA needs)

3. Download the credential configuration file:
     Workload Identity Pool → Connected Service Accounts → Download config
     This file is NOT a secret — it contains no private key.

=== LOCAL SETUP ===
   pip install -r requirements.txt

   # Point ADC at the WIF config file (no key, just metadata):
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credential-config.json
   export GCP_PROJECT_ID=your-gcp-project-id

   python main.py
"""

import json
import os

import google.auth
from google.auth import external_account
from google.auth.transport.requests import Request
from google.cloud import storage


# ---------------------------------------------------------------------------
# Method 1 – Application Default Credentials (ADC)
# ---------------------------------------------------------------------------
# If GOOGLE_APPLICATION_CREDENTIALS points to the WIF config file, google-auth
# picks it up automatically via ADC.  This is the recommended approach.
# ---------------------------------------------------------------------------

def authenticate_with_adc(scopes: list[str] | None = None):
    """Return (credentials, project_id) via Application Default Credentials."""
    if scopes is None:
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    credentials, project_id = google.auth.default(scopes=scopes)
    credentials.refresh(Request())

    print("[ADC] Authenticated successfully.")
    print(f"[ADC] Access token (first 20 chars): {credentials.token[:20]}...")
    return credentials, project_id


# ---------------------------------------------------------------------------
# Method 2 – Explicitly load a WIF credential configuration file
# ---------------------------------------------------------------------------
# Use this when you want to specify the config file path in code rather than
# via the GOOGLE_APPLICATION_CREDENTIALS environment variable.
# ---------------------------------------------------------------------------

def authenticate_with_wif_config(
    config_file_path: str,
    scopes: list[str] | None = None,
):
    """Return WIF credentials loaded from *config_file_path*."""
    if scopes is None:
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]

    if not os.path.exists(config_file_path):
        raise FileNotFoundError(
            f"WIF credential config file not found: {config_file_path}\n"
            "Download it from: GCP Console → Workload Identity Federation → "
            "Connected Service Accounts → Download config"
        )

    with open(config_file_path, encoding="utf-8") as fh:
        config = json.load(fh)

    credentials = external_account.Credentials.from_info(config, scopes=scopes)
    credentials.refresh(Request())

    print(f"[WIF] Credentials loaded from '{config_file_path}'.")
    print(f"[WIF] Access token (first 20 chars): {credentials.token[:20]}...")
    return credentials


# ---------------------------------------------------------------------------
# Sample GCP operation – list Cloud Storage buckets
# ---------------------------------------------------------------------------

def list_gcs_buckets(credentials, project_id: str):
    """Print all GCS buckets in *project_id* and return them as a list."""
    client = storage.Client(project=project_id, credentials=credentials)
    buckets = list(client.list_buckets())

    print(f"\nCloud Storage buckets in project '{project_id}':")
    if buckets:
        for bucket in buckets:
            print(f"  • {bucket.name}")
    else:
        print("  (no buckets found)")

    return buckets


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Read configuration from environment variables with sensible defaults.
    config_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credential-config.json")
    project_id = os.environ.get("GCP_PROJECT_ID", "")

    print("=" * 60)
    print("  GCP Workload Identity Federation – local authentication")
    print("=" * 60)

    # --- Method 1: ADC (recommended) ---
    print("\n[1] Trying Application Default Credentials (ADC) …")
    try:
        creds, detected_project = authenticate_with_adc()
        effective_project = project_id or detected_project or ""
        if effective_project:
            list_gcs_buckets(creds, effective_project)
        else:
            print(
                "[ADC] GCP project ID not detected automatically.\n"
                "      Set the GCP_PROJECT_ID environment variable and re-run."
            )
        return  # Success – no need to fall through to Method 2
    except Exception as exc:
        print(f"[ADC] Failed: {exc}")

    # --- Method 2: Explicit WIF config file ---
    print(f"\n[2] Trying explicit WIF config file: '{config_path}' …")
    try:
        creds = authenticate_with_wif_config(config_path)
        effective_project = project_id
        if effective_project:
            list_gcs_buckets(creds, effective_project)
        else:
            print(
                "[WIF] Credentials obtained, but GCP_PROJECT_ID is not set.\n"
                "      Set it to list buckets or call other GCP services."
            )
    except Exception as exc:
        print(f"[WIF] Failed: {exc}")
        print(
            "\nSetup checklist:\n"
            "  1. Create a Workload Identity Pool & Provider in GCP Console.\n"
            "  2. Download the credential config file (no private key inside).\n"
            "  3. export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credential-config.json\n"
            "  4. export GCP_PROJECT_ID=your-project-id\n"
            "  5. pip install -r requirements.txt\n"
            "  6. python main.py"
        )


if __name__ == "__main__":
    main()
