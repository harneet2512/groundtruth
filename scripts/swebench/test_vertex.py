#!/usr/bin/env python3
import os, json
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "/root/regal-key.json")

from google.oauth2 import service_account
creds = service_account.Credentials.from_service_account_file(
    "/root/regal-key.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
import google.auth.transport.requests
req = google.auth.transport.requests.Request()
creds.refresh(req)
print(f"auth=OK project={creds.project_id}")

# Test Vertex AI Qwen
import urllib.request
url = "https://global-aiplatform.googleapis.com/v1/projects/regal-scholar-442803-e1/locations/global/publishers/qwen/models/qwen3-coder-480b-a35b-instruct-maas:rawPredict"
data = json.dumps({"instances": [{"prompt": "Say OK"}], "parameters": {"maxOutputTokens": 5}}).encode()
headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
req2 = urllib.request.Request(url, data=data, headers=headers, method="POST")
try:
    resp = urllib.request.urlopen(req2, timeout=60)
    print(f"qwen_direct=OK response={resp.read()[:200].decode()}")
except Exception as e:
    print(f"qwen_direct=ERR {str(e)[:200]}")

# Start litellm
import subprocess, time
subprocess.run(["pkill", "-f", "litellm"], capture_output=True)
time.sleep(2)

# Write config
with open("/tmp/litellm_cfg.yaml", "w") as f:
    f.write("""model_list:
  - model_name: "qwen3-coder"
    litellm_params:
      model: "vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"
      vertex_project: "regal-scholar-442803-e1"
      vertex_location: "global"
      vertex_credentials: "/root/regal-key.json"
""")

proc = subprocess.Popen(
    ["/root/.local/bin/uv", "run", "litellm", "--config", "/tmp/litellm_cfg.yaml", "--port", "4000", "--host", "0.0.0.0"],
    stdout=open("/tmp/litellm.log", "w"), stderr=subprocess.STDOUT,
    cwd="/root/oh-benchmarks"
)
print(f"litellm_pid={proc.pid}")
time.sleep(20)

# Health check
try:
    resp = urllib.request.urlopen("http://localhost:4000/health", timeout=5)
    print(f"litellm_health=OK")
except Exception as e:
    print(f"litellm_health=ERR {str(e)[:100]}")
    with open("/tmp/litellm.log") as f:
        lines = f.readlines()
    for line in lines[-5:]:
        print(f"litellm_log={line.strip()}")

print("VERTEX_TEST_DONE")
