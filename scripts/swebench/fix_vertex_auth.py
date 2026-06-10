#!/usr/bin/env python3
"""Fix Vertex AI auth by using the service account token from metadata server.

The issue: gcloud auth prints the USER token (narrow scopes), but we need
the SERVICE ACCOUNT token (cloud-platform scope). This script:
1. Gets the SA token from the metadata server
2. Tests Vertex AI directly with that token
3. Sets up Application Default Credentials to use the SA
"""
import urllib.request
import json
import subprocess
import os
import sys

PROJECT = "project-26227097-98fa-4016-a54"
REGION = "us-south1"
MODEL = "qwen/qwen3-coder-480b-a35b-instruct-maas"

# Step 1: Get SA token from metadata server
print("--- Getting SA token from metadata ---")
req = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={"Metadata-Flavor": "Google"},
)
r = urllib.request.urlopen(req, timeout=5)
token_data = json.load(r)
sa_token = token_data["access_token"]
print(f"SA token: {sa_token[:20]}... expires_in={token_data.get('expires_in')}")

# Step 2: Test Vertex AI with SA token
print(f"\n--- Testing Vertex AI ({REGION}) ---")
url = (
    f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT}"
    f"/locations/{REGION}/endpoints/openapi/chat/completions"
)
body = json.dumps({
    "model": MODEL,
    "messages": [{"role": "user", "content": "say hi"}],
    "max_tokens": 5,
}).encode()
req = urllib.request.Request(
    url,
    data=body,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {sa_token}"},
)
try:
    r = urllib.request.urlopen(req, timeout=60)
    d = json.load(r)
    content = d["choices"][0]["message"]["content"]
    print(f"OK: {content[:50]}")
except urllib.error.HTTPError as e:
    err = e.read().decode()[:300]
    print(f"FAIL ({e.code}): {err}")
    print("\nThis means Vertex AI MaaS Qwen model is not available on this project.")
    print("You need to accept the model terms in Vertex AI Model Garden:")
    print(f"  https://console.cloud.google.com/vertex-ai/publishers/qwen/model-garden/qwen3-coder-480b-a35b-instruct-maas?project={PROJECT}")
    sys.exit(1)

# Step 3: Configure ADC to use SA instead of user credentials
print("\n--- Configuring Application Default Credentials ---")
subprocess.run(["gcloud", "auth", "application-default", "login", "--no-launch-browser", "--quiet"],
               capture_output=True)
# Alternative: just unset user credentials so google-auth falls back to SA
adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
if os.path.exists(adc_path):
    os.rename(adc_path, adc_path + ".bak")
    print(f"Moved user ADC to {adc_path}.bak — will use SA token now")
else:
    print("No user ADC found — SA token will be used by default")

print("\nDone. Restart litellm proxy to pick up SA credentials.")
