#!/usr/bin/env python3
"""Test Qwen3-Coder availability across Vertex AI regions."""
import urllib.request
import json
import subprocess
import sys

token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
project = "project-26227097-98fa-4016-a54"
regions = ["us-south1", "us-central1", "us-east1", "us-east4", "europe-west1", "global"]

for region in regions:
    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{region}/endpoints/openapi/chat/completions"
    )
    body = json.dumps({
        "model": "qwen/qwen3-coder-480b-a35b-instruct-maas",
        "messages": [{"role": "user", "content": "say hi"}],
        "max_tokens": 5,
    }).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        d = json.load(r)
        c = d["choices"][0]["message"]["content"]
        print(f"{region}: OK - {c[:30]}")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:150]
        print(f"{region}: {e.code} - {err}")
    except Exception as e:
        print(f"{region}: ERR - {e}")

print("No region worked. Model may need Model Garden acceptance.")
sys.exit(1)
