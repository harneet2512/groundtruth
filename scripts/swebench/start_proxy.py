"""Start litellm proxy with DeepSeek V4 Flash thinking disabled."""
import os
import subprocess
import sys
import time
import yaml

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not KEY:
    print("ERROR: DEEPSEEK_API_KEY not set")
    sys.exit(1)

cfg = {
    "model_list": [{
        "model_name": "deepseek-v4-flash-nothink",
        "litellm_params": {
            "model": "deepseek/deepseek-v4-flash",
            "api_key": KEY,
            "api_base": "https://api.deepseek.com",
            "thinking": {"type": "disabled"},
        }
    }],
    "general_settings": {"master_key": "sk-gt-local"},
    "litellm_settings": {"drop_params": True, "request_timeout": 300},
}

with open("/tmp/litellm_v4.yaml", "w") as f:
    yaml.dump(cfg, f)

print("Config written to /tmp/litellm_v4.yaml")
print("Starting proxy on port 4000...")

subprocess.Popen(
    ["litellm", "--config", "/tmp/litellm_v4.yaml", "--port", "4000"],
    stdout=open("/tmp/litellm_proxy.log", "w"),
    stderr=subprocess.STDOUT,
)

# Wait for proxy to be ready
import urllib.request
for i in range(30):
    time.sleep(1)
    try:
        urllib.request.urlopen("http://localhost:4000/health")
        print(f"Proxy ready after {i+1}s")
        sys.exit(0)
    except Exception:
        pass

print("ERROR: Proxy didn't start in 30s")
print(open("/tmp/litellm_proxy.log").read()[-500:])
sys.exit(1)
