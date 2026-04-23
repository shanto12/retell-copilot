#!/usr/bin/env python3
"""Create the Retell Copilot agent, attach it to the 940 phone number (replacing NetDebt Alex)."""
import json, os, sys, urllib.request, urllib.error, pathlib

API = "https://api.retellai.com"
KEY = os.environ["RETELL_API_KEY"]
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

art = pathlib.Path(__file__).parent.parent / "artifacts"
LLM_ID = (art / "copilot_llm_id.txt").read_text().strip()

WEBHOOK = os.environ.get("COPILOT_WEBHOOK", "https://gwakqpaovb.execute-api.us-east-1.amazonaws.com/prod/copilot-webhook")
PHONE_NUMBER = "+19403530737"


def api(method, path, body=None):
    url = f"{API}/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 1. Create the Copilot agent
agent_body = {
    "response_engine": {"type": "retell-llm", "llm_id": LLM_ID},
    "agent_name": "Retell Copilot",
    "voice_id": "11labs-Adrian",  # warm British-ish male, memorable
    "language": "en-US",
    "webhook_url": WEBHOOK,
    "end_call_after_silence_ms": 20000,
    "interruption_sensitivity": 0.55,
    "responsiveness": 1,
    "enable_backchannel": True,
    "backchannel_frequency": 0.6,
    "voice_speed": 1.0,
    "voice_temperature": 1.0,
    "post_call_analysis_data": [
        {"name": "caller_built_an_agent", "type": "boolean", "description": "True if provision_agent was called successfully during the call."},
        {"name": "agent_purpose", "type": "string", "description": "What the caller asked their agent to do, or empty."},
        {"name": "call_summary", "type": "string", "description": "1-2 sentence summary."}
    ]
}

status, resp = api("POST", "create-agent", agent_body)
if status not in (200, 201):
    print(f"create-agent failed: {status}\n{resp}", file=sys.stderr)
    sys.exit(1)
AGENT_ID = resp["agent_id"]
(art / "copilot_agent_id.txt").write_text(AGENT_ID)
print(f"✅ Agent created: {AGENT_ID}")

# 2. Reassign +19403530737 from NetDebt Alex to Copilot
status, resp = api("PATCH", f"update-phone-number/{PHONE_NUMBER}", {
    "inbound_agent_id": AGENT_ID,
    "outbound_agent_id": AGENT_ID,
    "nickname": "Retell Copilot (was NetDebt Alex)"
})
if status not in (200, 201):
    print(f"phone reassign failed: {status}\n{resp}", file=sys.stderr)
    sys.exit(1)
print(f"✅ Phone {PHONE_NUMBER} now routes to Copilot")

# 3. Nuke the old NetDebt Alex agent + LLM (per Shanto's instruction)
OLD_AGENT = "agent_20cce4114703d2f870cf04a81c"
OLD_LLM = "llm_64002678ded35cfe80c0345f5783"
for path in (f"delete-agent/{OLD_AGENT}", f"delete-retell-llm/{OLD_LLM}"):
    status, resp = api("DELETE", path)
    print(f"🗑  DELETE {path}: {status}")

print("\n✅ Copilot live on +1 (940) 353-0737")
