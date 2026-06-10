#!/usr/bin/env python3
"""Test DeepSeek V3.2 availability on Vertex AI."""
import urllib.request, json, subprocess, sys

token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
project = "project-26227097-98fa-4016-a54"

models = [
    ("us-east5", "deepseek-ai/deepseek-v3-0324"),
    ("us-south1", "deepseek-ai/deepseek-v3-0324"),
    ("us-central1", "deepseek-ai/deepseek-v3-0324"),
    ("us-east5", "deepseek-ai/DeepSeek-V3"),
    ("us-south1", "deepseek-ai/DeepSeek-V3"),
]

for region, model in models:
    url = (
        f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{region}/endpoints/openapi/chat/completions"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "say hi"}],
        "max_tokens": 5,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        r = urllib.request.urlopen(req, timeout=30)
        d = json.load(r)
        c = d["choices"][0]["message"]["content"]
        print(f"{region}/{model}: OK - {c[:30]}")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:100]
        print(f"{region}/{model}: {e.code} - {err}")
    except Exception as e:
        print(f"{region}/{model}: ERR - {e}")

print("No DeepSeek endpoint worked.")
sys.exit(1)
