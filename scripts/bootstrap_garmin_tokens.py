#!/usr/bin/env python3
"""One-time interactive Garmin token bootstrap for Mickey Marathon.

Garmin blocks headless credential logins (Cloudflare TLS fingerprinting,
March 2026), so the deployed agent NEVER logs in with credentials. Instead:

  1. You run this script ON A WORKSTATION. It logs in interactively
     (handling MFA), using garminconnect's curl_cffi-based client which
     impersonates a real browser's TLS fingerprint.
  2. The resulting token bundle (long-lived OAuth1 ~1 year + short-lived
     OAuth2) is verified with a real API call and stored in Secret Manager.
  3. The deployed agent loads the bundle and runs token-only, refreshing
     OAuth2 in memory. When the OAuth1 token finally expires (~a year),
     Mickey will start telling you to re-run this script.

Usage:
    # from the Marathon repo root, in the shared venv
    /home/jonathan/projects/.my_venv/bin/python3 scripts/bootstrap_garmin_tokens.py

    # or import tokens you already have (e.g. ~/.garminconnect)
    ... scripts/bootstrap_garmin_tokens.py --tokenstore ~/.garminconnect

Re-running rotates the stored bundle (adds a new secret version; the agent
always reads `latest`).
"""
import argparse
import getpass
import sys

from garminconnect import Garmin
from google.cloud import secretmanager

DEFAULT_PROJECT = "mickey-marathon"
DEFAULT_SECRET = "mickey-marathon-garmin-tokens"


def store_secret(project: str, secret: str, payload: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}/secrets/{secret}"
    version = client.add_secret_version(
        request={"parent": parent, "payload": {"data": payload.encode("utf-8")}}
    )
    return version.name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Agent GCP project")
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="Secret Manager secret id")
    parser.add_argument(
        "--tokenstore",
        default=None,
        help="Optional: path to an existing token dir/file to import instead of logging in",
    )
    args = parser.parse_args()

    if args.tokenstore:
        print(f"Importing tokens from {args.tokenstore}...")
        garmin = Garmin()
        garmin.login(tokenstore=args.tokenstore)
    else:
        print("Garmin Connect interactive login (credentials are NOT stored anywhere).")
        email = input("  Garmin email: ").strip()
        password = getpass.getpass("  Garmin password: ")
        garmin = Garmin(
            email=email,
            password=password,
            prompt_mfa=lambda: input("  MFA code: ").strip(),
        )
        garmin.login()

    # Prove the tokens actually work before storing them.
    name = garmin.get_full_name()
    print(f"  Verified: logged in as {name}")

    bundle = garmin.client.dumps()
    version = store_secret(args.project, args.secret, bundle)
    print(f"  Token bundle stored: {version}")
    print("Done. The deployed agent reads the `latest` version of this secret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
