#!/usr/bin/env python3
"""Test Vertex AI Qwen3-Coder connectivity and litellm proxy."""
import os, json, urllib.request, subprocess, time

os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "/root/regal-key.json")

from google.oauth2 import service_account
creds = service_account.Credentials.from_service_account_file(
    "/root/regal-key.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
import google.auth.transport.requests
creds.refresh(google.auth.transport.requests.Request())
print(f"auth=OK project={creds.project_id}")

# Test Qwen on Vertex AI — try multiple endpoint formats and locations
PROJECT = "regal-scholar-442803-e1"
MODEL = "qwen/qwen3-coder-480b-a35b-instruct-maas"

# MaaS models use OpenAI-compatible endpoint via Vertex
endpoints = [
    # OpenAI-compatible chat completions (MaaS style)
    ("us-central1", f"https://us-central1-aiplatform.googleapis.com/v1beta1/projects/{PROJECT}/locations/us-central1/endpoints/openapi/chat/completions",
     json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 5})),
    # Global MaaS
    ("global", f"https://global-aiplatform.googleapis.com/v1beta1/projects/{PROJECT}/locations/global/endpoints/openapi/chat/completions",
     json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 5})),
    # rawPredict style
    ("us-central1-raw", f"https://us-central1-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/us-central1/publishers/qwen/models/qwen3-coder-480b-a35b-instruct-maas:rawPredict",
     json.dumps({"instances": [{"prompt": "OK"}], "parameters": {"maxOutputTokens": 5}})),
]

for name, url, data in endpoints:
    req = urllib.request.Request(url, data=data.encode(), method="POST",
        headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        body = resp.read()[:300].decode()
        print(f"qwen_{name}=OK {body}")
        break  # One success is enough
    except Exception as e:
        print(f"qwen_{name}=ERR {str(e)[:150]}")

# Start litellm with the correct config
subprocess.run(["pkill", "-f", "litellm"], capture_output=True)
time.sleep(2)

with open("/tmp/litellm_cfg.yaml", "w") as f:
    f.write(f"""model_list:
  - model_name: "qwen3-coder"
    litellm_params:
      model: "vertex_ai/{MODEL}"
      vertex_project: "{PROJECT}"
      vertex_location: "global"
      vertex_credentials: "/root/regal-key.json"
      top_k: 20
      repetition_penalty: 1.05
""")

proc = subprocess.Popen(
    ["/root/.local/bin/uv", "run", "litellm", "--config", "/tmp/litellm_cfg.yaml",
     "--port", "4000", "--host", "0.0.0.0"],
    stdout=open("/tmp/litellm.log", "w"), stderr=subprocess.STDOUT,
    cwd="/root/oh-benchmarks",
    env={**os.environ, "GOOGLE_APPLICATION_CREDENTIALS": "/root/regal-key.json"}
)
print(f"litellm_pid={proc.pid}")
time.sleep(25)

# Health check
try:
    resp = urllib.request.urlopen("http://localhost:4000/health", timeout=5)
    print(f"litellm_health={resp.read().decode()[:100]}")
except Exception as e:
    print(f"litellm_health=ERR {str(e)[:100]}")
    try:
        with open("/tmp/litellm.log") as f:
            for line in f.readlines()[-10:]:
                line = line.strip()
                if line and 'banner' not in line.lower() and '|' not in line[:3]:
                    print(f"litellm_log={line}")
    except:
        pass

# Test API via litellm
try:
    data = json.dumps({"model": "qwen3-coder", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 5}).encode()
    req = urllib.request.Request("http://localhost:4000/v1/chat/completions",
        data=data, headers={"Content-Type": "application/json"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=90)
    result = json.loads(resp.read())
    content = result.get("choices", [{}])[0].get("message", {}).get("content", "NO_CONTENT")
    print(f"litellm_api=OK response={content[:100]}")
except Exception as e:
    print(f"litellm_api=ERR {str(e)[:150]}")

print("VERTEX_TEST_DONE")
