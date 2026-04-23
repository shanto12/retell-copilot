#!/usr/bin/env python3
"""Create the Retell Copilot LLM (prompt + provision_agent tool)."""
import json, os, sys, urllib.request, urllib.error, pathlib

API = "https://api.retellai.com"
KEY = os.environ["RETELL_API_KEY"]
API_BASE = os.environ.get("COPILOT_API_BASE", "https://gwakqpaovb.execute-api.us-east-1.amazonaws.com/prod")

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

prompt_path = pathlib.Path(__file__).parent / "copilot_prompt.txt"
prompt = prompt_path.read_text()

body = {
    "model": "gpt-4.1",
    "model_temperature": 0.4,
    "general_prompt": prompt,
    "begin_message": "Hey! This is Copilot. Tell me what kind of voice agent you want, and I'll build it and have it live on a test URL before we hang up. What do you have in mind?",
    "general_tools": [
        {
            "type": "custom",
            "name": "provision_agent",
            "description": "Create a real Retell voice agent from the caller's spec. Call this ONCE you have agent_purpose, agent_name, voice_style, greeting, and key_questions confirmed. Returns a test URL the caller can visit to try their new agent.",
            "url": f"{API_BASE}/provision-agent",
            "speak_during_execution": True,
            "speak_after_execution": False,
            "execution_message_description": "A short one-liner like 'building your agent now' to say while the tool runs — keep it under 6 words.",
            "timeout_ms": 15000,
            "parameters": {
                "type": "object",
                "required": ["agent_purpose", "agent_name", "voice_style", "greeting", "key_questions"],
                "properties": {
                    "agent_purpose": {"type": "string", "description": "One-sentence description of what the agent does."},
                    "agent_name": {"type": "string", "description": "Persona name of the agent."},
                    "voice_style": {"type": "string", "enum": ["warm_female","warm_male","professional_female","professional_male","energetic_female","british_male"]},
                    "greeting": {"type": "string", "description": "Exact words the agent says first when a caller connects."},
                    "key_questions": {"type": "array", "items": {"type": "string"}, "description": "3-5 things the agent must ask every caller."},
                    "language": {"type": "string", "description": "BCP-47 tag, default en-US."}
                }
            }
        },
        {
            "type": "end_call",
            "name": "end_call",
            "description": "End the call when the caller says goodbye or is clearly done.",
        }
    ]
}

req = urllib.request.Request(f"{API}/create-retell-llm", data=json.dumps(body).encode(), headers=HEADERS, method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
except urllib.error.HTTPError as e:
    print("ERROR:", e.code, e.read().decode(), file=sys.stderr)
    sys.exit(1)

print(f"✅ LLM created: {resp['llm_id']}")
# Save llm_id
out = pathlib.Path(__file__).parent.parent / "artifacts" / "copilot_llm_id.txt"
out.parent.mkdir(exist_ok=True)
out.write_text(resp["llm_id"])
print(f"   saved -> {out}")
