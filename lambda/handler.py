"""
Retell Copilot — single-file Lambda serving landing page, test pages, and provisioning API.

Routes:
  GET  /                       -> landing page
  GET  /test/{agent_id}        -> test-your-agent page (Retell Web Call)
  GET  /t/{code}               -> short-code redirect to /test/{agent_id}
  POST /provision-agent        -> create a Retell LLM+Agent from spec (used by web form AND voice tool)
  POST /create-web-call        -> mint a Retell web-call access token for an agent
  POST /copilot-webhook        -> Retell call event callback (no-op ack)
  GET  /healthz                -> health check
"""
import os
import json
import string
import secrets
import urllib.request
import urllib.error
from datetime import datetime, timezone

import boto3

RETELL_API_KEY = os.environ["RETELL_API_KEY"]
RETELL_API = "https://api.retellai.com"
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
CODES_TABLE = os.environ.get("CODES_TABLE", "retell-copilot-codes")

_ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_codes = _ddb.Table(CODES_TABLE)

# 23 unambiguous lowercase letters (no i, l, o — hard to read/hear)
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz"


VOICE_MAP = {
    "warm_female": "11labs-Marissa",
    "warm_male": "11labs-Adrian",
    "professional_female": "11labs-Kate",
    "professional_male": "11labs-Brian",
    "energetic_female": "11labs-Anna",
    "british_male": "11labs-Chloe",
}


def _headers():
    return {"Authorization": f"Bearer {RETELL_API_KEY}", "Content-Type": "application/json"}


def _retell(method, path, body=None):
    url = f"{RETELL_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": "retell_http_error"}


def _resp(status, body, content_type="application/json", extra_headers=None):
    if content_type == "application/json" and not isinstance(body, str):
        body_str = json.dumps(body)
    else:
        body_str = body
    headers = {
        "Content-Type": content_type,
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-store",
    }
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": status, "headers": headers, "body": body_str}


def _base_url(event):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    ctx = event.get("requestContext") or {}
    domain = ctx.get("domainName") or event.get("headers", {}).get("host", "")
    return f"https://{domain}"


def _new_short_code():
    """Generate a 5-char short code, avoiding collisions (unlikely but handle it)."""
    for _ in range(5):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(5))
        existing = _codes.get_item(Key={"code": code}).get("Item")
        if not existing:
            return code
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def _store_code(code, agent_id, meta=None):
    item = {"code": code, "agent_id": agent_id, "created_at": datetime.now(timezone.utc).isoformat()}
    if meta:
        item["meta"] = meta
    _codes.put_item(Item=item)


def _lookup_code(code):
    item = _codes.get_item(Key={"code": code}).get("Item")
    return item.get("agent_id") if item else None


# --- Provisioning -----------------------------------------------------------

BUILT_AGENT_PROMPT_TEMPLATE = """You are {agent_name}. Your job: {agent_purpose}.

## Style
- Warm, conversational, short sentences. Speak like a real person, not a form.
- ONE question at a time. Never dump a list.
- If partial info, ask follow-ups. If you have everything, confirm and wrap up.

## Greeting (say this FIRST on every call, verbatim)
"{greeting}"

## Info to capture each call
{numbered_questions}

## Close
Summarize: "So to confirm — [key facts]. Sound right?" Thank them by name. Hang up when done.

## Rules
- NO medical, legal, or financial advice.
- If caller gets upset or asks for a human, apologize and offer escalation.
- Stay in character as {agent_name}. Never mention you are AI unless directly asked.
"""


def _build_agent_prompt(spec):
    qs = spec.get("key_questions") or []
    if isinstance(qs, str):
        qs = [q.strip() for q in qs.replace(";", ",").split(",") if q.strip()]
    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(qs)) or "1. Caller's name and why they called"
    return BUILT_AGENT_PROMPT_TEMPLATE.format(
        agent_name=spec.get("agent_name", "Assistant"),
        agent_purpose=spec.get("agent_purpose", "help the caller"),
        greeting=spec.get("greeting", "Hi, how can I help you today?"),
        numbered_questions=numbered,
    )


def _provision_agent(spec):
    voice_id = VOICE_MAP.get(spec.get("voice_style", "warm_female"), "11labs-Marissa")
    prompt = _build_agent_prompt(spec)

    llm_body = {
        "model": "gpt-4.1-mini",
        "model_temperature": 0.5,
        "general_prompt": prompt,
        "begin_message": spec.get("greeting") or "Hi, how can I help you today?",
    }
    s, llm = _retell("POST", "/create-retell-llm", llm_body)
    if s not in (200, 201):
        return s, {"error": "llm_create_failed", "detail": llm}

    agent_body = {
        "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
        "agent_name": f"Copilot-built • {spec.get('agent_name', 'Agent')}",
        "voice_id": voice_id,
        "language": spec.get("language") or "en-US",
        "end_call_after_silence_ms": 20000,
        "interruption_sensitivity": 0.6,
        "enable_backchannel": True,
        "backchannel_frequency": 0.5,
        "voice_speed": 1.0,
    }
    s, agent = _retell("POST", "/create-agent", agent_body)
    if s not in (200, 201):
        return s, {"error": "agent_create_failed", "detail": agent}
    return 200, {"llm_id": llm["llm_id"], "agent_id": agent["agent_id"], "voice_id": voice_id}


def _create_web_call(agent_id):
    s, call = _retell("POST", "/v2/create-web-call", {"agent_id": agent_id})
    if s not in (200, 201):
        return s, {"error": "web_call_create_failed", "detail": call}
    return 200, {"access_token": call.get("access_token"), "call_id": call.get("call_id")}


# --- HTML -------------------------------------------------------------------

LANDING_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="description" content="A Retell voice agent that builds Retell voice agents. Describe it, it ships, you call it — in 60 seconds."/>
<meta property="og:title" content="Retell Copilot — build a voice agent in 60 seconds"/>
<meta property="og:description" content="Built by Shanto Mathew as a working Retell FDE demo. A voice agent that builds voice agents — call or type."/>
<title>Retell Copilot · 60-second voice agent builder</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#06070a;
    --bg-2:#0e0f13;
    --card:#13151b;
    --card-2:#171922;
    --ink:#f5f5f7;
    --muted:#a0a0ab;
    --dim:#6a6a75;
    --accent:#a78bfa;
    --accent-2:#67e8f9;
    --grad:linear-gradient(120deg,#a78bfa 0%,#67e8f9 100%);
    --border:rgba(255,255,255,.08);
    --glow:0 0 0 1px rgba(167,139,250,.25),0 10px 40px -12px rgba(103,232,249,.18);
    --danger:#f87171;
    --ok:#34d399;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:'Inter',-apple-system,system-ui,sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased;}
  a{color:var(--accent-2);text-decoration:none;}
  code,.mono{font-family:'JetBrains Mono',ui-monospace,SFMono-Regular,monospace;font-size:.92em;}
  .bg-orb{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden;}
  .bg-orb::before,.bg-orb::after{content:"";position:absolute;width:700px;height:700px;border-radius:50%;filter:blur(120px);opacity:.35;}
  .bg-orb::before{top:-200px;left:-200px;background:radial-gradient(circle,#a78bfa 0%,transparent 70%);}
  .bg-orb::after{bottom:-200px;right:-200px;background:radial-gradient(circle,#67e8f9 0%,transparent 70%);}
  .wrap{position:relative;z-index:1;max-width:1120px;margin:0 auto;padding:40px 24px 80px;}
  nav{display:flex;justify-content:space-between;align-items:center;padding:8px 0 32px;}
  .brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.01em;}
  .brand .logo{width:28px;height:28px;border-radius:8px;background:var(--grad);display:grid;place-items:center;color:#06070a;font-weight:800;font-size:14px;}
  .nav-links{display:flex;gap:20px;color:var(--muted);font-size:14px;}
  .hero{padding:56px 0 36px;}
  .eyebrow{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;background:rgba(167,139,250,.08);border:1px solid rgba(167,139,250,.2);color:var(--accent);font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:20px;}
  .eyebrow .pulse{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:p 1.6s infinite;}
  @keyframes p{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.85)}}
  h1{font-size:clamp(42px,6vw,72px);line-height:1.02;letter-spacing:-.03em;margin:0 0 20px;font-weight:800;}
  h1 .grad{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;}
  .sub{color:var(--muted);font-size:clamp(17px,1.3vw,20px);max-width:680px;margin:0 0 40px;line-height:1.5;}
  .metrics{display:flex;gap:28px;flex-wrap:wrap;margin-top:28px;margin-bottom:16px;}
  .metric .n{font-size:28px;font-weight:800;letter-spacing:-.01em;background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;}
  .metric .l{color:var(--dim);font-size:13px;}
  .cards{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:40px;}
  @media (max-width:820px){.cards{grid-template-columns:1fr;}}
  .card{background:var(--card);border:1px solid var(--border);border-radius:24px;padding:32px;position:relative;transition:transform .15s,box-shadow .15s;}
  .card:hover{transform:translateY(-2px);box-shadow:var(--glow);}
  .card h2{font-size:22px;margin:0 0 8px;letter-spacing:-.01em;font-weight:700;}
  .card .chip{position:absolute;top:20px;right:20px;font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);}
  .card p{color:var(--muted);margin:0 0 22px;font-size:15px;}
  .phone-box{margin:20px 0 8px;}
  .phone-box .num{font-family:'JetBrains Mono',monospace;font-size:34px;font-weight:700;letter-spacing:-.01em;}
  .phone-box .num a{color:var(--ink);}
  .hint{color:var(--dim);font-size:12px;margin-top:6px;}
  form{display:flex;flex-direction:column;gap:14px;}
  label{font-size:12px;color:var(--dim);letter-spacing:.02em;font-weight:500;}
  input,textarea,select{width:100%;background:var(--bg-2);border:1px solid var(--border);color:var(--ink);border-radius:12px;padding:12px 14px;font:inherit;font-size:15px;font-family:inherit;transition:border-color .15s;}
  input:focus,textarea:focus,select:focus{outline:none;border-color:rgba(167,139,250,.5);}
  textarea{min-height:72px;resize:vertical;font-family:inherit;}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
  @media (max-width:560px){.row{grid-template-columns:1fr;}}
  button.primary{background:var(--grad);color:#06070a;border:0;border-radius:14px;padding:14px 22px;font-weight:700;font-size:16px;cursor:pointer;transition:transform .08s,filter .15s;font-family:inherit;}
  button.primary:hover{transform:translateY(-1px);filter:brightness(1.05);}
  button.primary:disabled{opacity:.5;cursor:wait;transform:none;}
  .err{color:var(--danger);font-size:13px;margin-top:6px;}
  .ok-msg{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.25);border-radius:12px;padding:12px;color:var(--ok);font-size:14px;display:none;}
  .ok-msg.show{display:block;}

  .section{margin-top:88px;}
  .section h3{font-size:14px;color:var(--accent-2);letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px;}
  .section h2{font-size:clamp(28px,3.2vw,40px);letter-spacing:-.02em;margin:0 0 32px;font-weight:700;}

  .steps{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;}
  @media (max-width:720px){.steps{grid-template-columns:1fr;}}
  .step{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:24px;}
  .step .n{width:32px;height:32px;border-radius:10px;background:rgba(167,139,250,.12);color:var(--accent);font-family:'JetBrains Mono',monospace;font-weight:700;display:grid;place-items:center;margin-bottom:14px;font-size:14px;}
  .step h4{margin:0 0 8px;font-size:17px;font-weight:600;letter-spacing:-.01em;}
  .step p{color:var(--muted);margin:0;font-size:14px;}

  .examples{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;}
  @media (max-width:720px){.examples{grid-template-columns:1fr;}}
  .example{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:20px;cursor:pointer;transition:all .15s;}
  .example:hover{border-color:rgba(167,139,250,.35);transform:translateY(-1px);}
  .example .role{font-size:12px;color:var(--dim);letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px;}
  .example .title{font-weight:600;font-size:16px;margin-bottom:4px;letter-spacing:-.01em;}
  .example .teaser{color:var(--muted);font-size:13px;line-height:1.4;}
  .example .cta{display:inline-block;margin-top:10px;color:var(--accent-2);font-size:13px;font-weight:600;}

  .stack{display:flex;gap:16px;flex-wrap:wrap;margin-top:16px;}
  .chip2{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:10px;background:var(--card);border:1px solid var(--border);font-size:13px;color:var(--muted);}

  footer{margin-top:88px;padding-top:32px;border-top:1px solid var(--border);display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;color:var(--muted);font-size:13px;}
  footer .c{color:var(--ink);font-weight:600;}
  footer a{color:var(--accent-2);}
  footer a:focus-visible,.example:focus-visible,.card a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px;}

  /* ── Attribution ribbon: pinned to the top so reviewers see it instantly ── */
  .ribbon{
    background:linear-gradient(90deg,rgba(167,139,250,.14),rgba(103,232,249,.10));
    border-bottom:1px solid var(--border);
    padding:10px 20px;font-size:13px;color:var(--ink);
    display:flex;justify-content:center;align-items:center;gap:16px;flex-wrap:wrap;
  }
  .ribbon strong{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;font-weight:700;}
  .ribbon .sep{color:var(--dim);}
  .ribbon a{color:var(--accent-2);font-weight:600;}

  /* ── Built-by callout under hero (reinforces ribbon) ── */
  .built-by{
    margin:28px 0 0;
    padding:16px 20px;
    background:var(--card-2);
    border:1px dashed rgba(167,139,250,.35);
    border-radius:14px;
    display:flex;gap:16px;flex-wrap:wrap;align-items:center;justify-content:space-between;
    color:var(--muted);font-size:13.5px;line-height:1.5;
  }
  .built-by strong{color:var(--ink);}
  .built-by .links{display:flex;gap:14px;flex-wrap:wrap;}
  .built-by .links a{color:var(--accent-2);font-weight:600;}

  /* ── Success state (after build) ── */
  .ok-msg .short-code{
    display:inline-block;font-family:'JetBrains Mono',monospace;font-weight:700;
    font-size:16px;padding:4px 10px;background:rgba(52,211,153,.12);border-radius:6px;margin:0 4px;
  }
  .ok-msg .row-actions{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap;}
  .ok-msg button{background:transparent;border:1px solid var(--border);color:var(--ink);
    border-radius:10px;padding:8px 14px;font-size:13px;cursor:pointer;font-family:inherit;}
  .ok-msg button:hover{border-color:rgba(167,139,250,.55);}
</style>
</head>
<body>
<div class="bg-orb"></div>
<div class="ribbon" role="note" aria-label="Attribution">
  <span>🎯 <strong>A live demo built by Shanto Mathew for the Retell AI Forward-Deployed Engineer interview.</strong></span>
  <span class="sep">·</span>
  <a href="https://github.com/shanto12/retell-copilot" target="_blank" rel="noopener noreferrer">Source on GitHub</a>
  <span class="sep">·</span>
  <a href="https://www.linkedin.com/in/shantomathew/" target="_blank" rel="noopener noreferrer">LinkedIn</a>
  <span class="sep">·</span>
  <a href="mailto:shanto12@gmail.com">Email</a>
</div>
<div class="wrap">
  <nav>
    <div class="brand">
      <div class="logo">⌁</div>
      <span>Retell Copilot</span>
    </div>
    <div class="nav-links">
      <a href="#how">How it works</a>
      <a href="#examples">Examples</a>
      <a href="#stack">Stack</a>
      <a href="#about">About this demo</a>
    </div>
  </nav>

  <section class="hero">
    <span class="eyebrow"><span class="pulse"></span>Copilot is live · +1 (940) 353-0737</span>
    <h1>Build a production-ready<br/><span class="grad">voice agent in 60 seconds</span>.</h1>
    <p class="sub">Call Copilot. Tell it what you want. A real Retell agent — with a real voice, a real phone number, real tools — gets provisioned live on the call. Or skip the phone and fill out the form. Same backend.</p>

    <div class="metrics">
      <div class="metric"><div class="n">~6s</div><div class="l">provisioning latency</div></div>
      <div class="metric"><div class="n">1 call</div><div class="l">from spec to live agent</div></div>
      <div class="metric"><div class="n">Retell-native</div><div class="l">LLM + Agent + Voice</div></div>
      <div class="metric"><div class="n">Built in 6h</div><div class="l">by one engineer</div></div>
    </div>

    <div class="built-by" id="built-by">
      <div>
        👋 This whole thing — the voice Copilot, the web form, the short-code system, the Retell + AWS backend, the Netlify edge —
        is <strong>a hand-built interview demo for the Retell AI Forward-Deployed Engineer role</strong>.
        Shipped end-to-end in under a day by <strong>Shanto Mathew</strong>.
      </div>
      <div class="links">
        <a href="https://github.com/shanto12/retell-copilot" target="_blank" rel="noopener noreferrer">📦 Source</a>
        <a href="#about">About the builder</a>
      </div>
    </div>
  </section>

  <section class="cards">
    <div class="card">
      <div class="chip">Path A</div>
      <h2>📞 Call the Copilot</h2>
      <p>Pick up your phone. Tell Copilot what your agent should do, what voice, what to ask callers. It reads back a short URL while you're still on the line.</p>
      <div class="phone-box">
        <div class="num"><a href="tel:+19403530737">+1 (940) 353-0737</a></div>
      </div>
      <p class="hint">Answers instantly. Talk like you'd talk to a person. It's fast.</p>
    </div>

    <div class="card">
      <div class="chip">Path B</div>
      <h2>💻 Build it here</h2>
      <p>Same backend, same latency, no phone needed. You get a "Test my agent" URL the moment it's live.</p>
      <form id="spec-form" autocomplete="off" style="margin-top:14px;">
        <div>
          <label for="purpose">What does your agent do?</label>
          <input id="purpose" name="agent_purpose" required placeholder="e.g. books cleanings for Dr. Singh's dental office"/>
        </div>
        <div class="row">
          <div>
            <label for="name">Agent's name</label>
            <input id="name" name="agent_name" required placeholder="Riley" value="Riley"/>
          </div>
          <div>
            <label for="voice">Voice</label>
            <select id="voice" name="voice_style">
              <option value="warm_female">Warm female (Marissa)</option>
              <option value="warm_male">Warm male (Adrian)</option>
              <option value="professional_female">Professional female (Kate)</option>
              <option value="professional_male">Professional male (Brian)</option>
              <option value="energetic_female">Energetic female (Anna)</option>
            </select>
          </div>
        </div>
        <div>
          <label for="greeting">First thing it says</label>
          <input id="greeting" name="greeting" required placeholder="Hi, thanks for calling Dr. Singh's office — how can I help?"/>
        </div>
        <div>
          <label for="questions">What it should ask every caller (comma-separated)</label>
          <textarea id="questions" name="key_questions" required placeholder="caller name, phone number, reason for visit, preferred day and time"></textarea>
        </div>
        <button class="primary" type="submit" id="submit-btn">⚡ Build my agent</button>
        <div id="err" class="err"></div>
        <div id="success" class="ok-msg"></div>
      </form>
    </div>
  </section>

  <section id="how" class="section">
    <h3>How it works</h3>
    <h2>From "describe it" to "call it" in three steps.</h2>
    <div class="steps">
      <div class="step">
        <div class="n">1</div>
        <h4>Describe the agent</h4>
        <p>By voice or by form, tell Copilot the agent's job, name, voice, greeting, and the questions it should ask.</p>
      </div>
      <div class="step">
        <div class="n">2</div>
        <h4>Copilot provisions</h4>
        <p>A Lambda behind the Copilot tool hits Retell's REST API, creates a real LLM + Agent tuned to your spec, and mints a web-call access token.</p>
      </div>
      <div class="step">
        <div class="n">3</div>
        <h4>You test it — instantly</h4>
        <p>Copilot reads back a short URL like <code>/t/abxjm</code>. Visit it, hit the call button, talk to your brand-new agent in-browser. No signup, no phone number purchase.</p>
      </div>
    </div>
  </section>

  <section id="examples" class="section">
    <h3>Try these pre-built examples</h3>
    <h2>Click, talk, see what a 60-second build looks like.</h2>
    <div class="examples" id="example-grid">
      <!-- populated by JS -->
    </div>
  </section>

  <section id="stack" class="section">
    <h3>Under the hood</h3>
    <h2>One engineer, one day, the full stack.</h2>
    <div class="stack">
      <span class="chip2">📞 Retell Voice SDK</span>
      <span class="chip2">🎤 ElevenLabs voices (Marissa · Adrian · Kate · Brian)</span>
      <span class="chip2">🛠 GPT-4.1-mini on Retell LLM</span>
      <span class="chip2">☁️ AWS Lambda + HTTP API Gateway</span>
      <span class="chip2">🗂 DynamoDB (short-code store)</span>
      <span class="chip2">🌐 Retell Web Call SDK (for in-browser test)</span>
    </div>
    <p style="margin-top:20px;color:var(--muted);font-size:14px;max-width:760px;">
      The Copilot itself is a Retell agent with a custom <code>provision_agent</code> tool. When you describe the agent you want, the tool calls the Lambda, which calls Retell's <code>/create-retell-llm</code> and <code>/create-agent</code>, stores a short-code mapping in DynamoDB, and returns a memorable URL the Copilot reads back to you. Same pipeline serves the web form. Every piece is something a Retell FDE would touch in their first month.
    </p>
  </section>

  <section id="about" class="section">
    <h3>About this demo</h3>
    <h2>Why I built it.</h2>
    <div style="display:grid;grid-template-columns:1.3fr 1fr;gap:24px;max-width:960px;">
      <div style="color:var(--muted);font-size:15px;line-height:1.6;">
        <p style="margin:0 0 14px 0;">
          I'm <strong style="color:var(--ink);">Shanto Mathew</strong> — 12+ years of production Python, and for the last 2 years I've been shipping
          real voice + agentic-AI systems (Retell, OpenAI Realtime, LangGraph, function calling, sub-250&nbsp;ms latency).
        </p>
        <p style="margin:0 0 14px 0;">
          When I saw the Retell AI Forward-Deployed Engineer role I didn't want to send a resume and hope.
          I wanted to ship the kind of thing an FDE actually ships in the field — a working, production-lean
          Retell deployment that a customer could start using the moment I left the call.
        </p>
        <p style="margin:0;">
          So I built <strong style="color:var(--ink);">Retell Copilot</strong>: a voice agent (on Retell) whose one
          tool creates new voice agents (on Retell) on demand. Same developer experience I'd want a Retell
          customer to have. The whole system — voice prompt, Lambda, API Gateway, DynamoDB short-codes, the
          web companion, the Netlify edge — is in the public repo below. That's my cover letter.
        </p>
      </div>
      <div style="background:var(--card);border:1px solid var(--border);border-radius:16px;padding:22px;font-size:14px;">
        <div style="font-size:11px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase;font-weight:700;margin-bottom:12px;">Get in touch</div>
        <div style="margin-bottom:8px;">📧 <a href="mailto:shanto12@gmail.com">shanto12@gmail.com</a></div>
        <div style="margin-bottom:8px;">📱 +1 (213) 431-9641</div>
        <div style="margin-bottom:8px;">💼 <a href="https://www.linkedin.com/in/shantomathew/" target="_blank" rel="noopener noreferrer">linkedin.com/in/shantomathew</a></div>
        <div style="margin-bottom:8px;">📦 <a href="https://github.com/shanto12/retell-copilot" target="_blank" rel="noopener noreferrer">github.com/shanto12/retell-copilot</a></div>
        <div style="margin-top:14px;color:var(--dim);font-size:12px;">Dallas, TX · Work authorized (E-3D, no sponsorship required)</div>
      </div>
    </div>
  </section>

  <footer>
    <div>
      <div class="c">Built by Shanto Mathew</div>
      <div>shanto12@gmail.com · <a href="https://www.linkedin.com/in/shantomathew/" target="_blank" rel="noopener noreferrer">LinkedIn</a> · <a href="https://github.com/shanto12/retell-copilot" target="_blank" rel="noopener noreferrer">GitHub</a></div>
      <div style="margin-top:6px;color:var(--dim);">A working demo for the Retell AI Forward-Deployed Engineer role.</div>
    </div>
    <div style="text-align:right;">
      <div>🏗 Deployed 2026-04-23</div>
      <div>Source: <a href="https://github.com/shanto12/retell-copilot" target="_blank" rel="noopener noreferrer">github.com/shanto12/retell-copilot</a></div>
      <div style="margin-top:6px;color:var(--dim);">This site uses no cookies and no tracking.</div>
    </div>
  </footer>
</div>

<script>
// Form submission
const form = document.getElementById('spec-form');
const btn = document.getElementById('submit-btn');
const err = document.getElementById('err');
const ok = document.getElementById('success');

// Very light session rate-limit: protects the Retell account from a runaway form.
// Generous enough that real demo users never hit it.
const RL_KEY = 'rc_rate', RL_MAX = 6, RL_WINDOW_MS = 60 * 60 * 1000;
function rateAllow() {
  try {
    const now = Date.now();
    const arr = JSON.parse(localStorage.getItem(RL_KEY) || '[]').filter(t => now - t < RL_WINDOW_MS);
    if (arr.length >= RL_MAX) return false;
    arr.push(now); localStorage.setItem(RL_KEY, JSON.stringify(arr));
    return true;
  } catch(_) { return true; }
}

function resetForm() {
  btn.disabled = false; btn.textContent = '⚡ Build my agent';
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  err.textContent = ''; ok.classList.remove('show'); ok.innerHTML = '';

  // Client-side validation
  const purpose = (document.getElementById('purpose').value || '').trim();
  if (purpose.length < 8) {
    err.textContent = '⚠️  Describe what your agent should do in a full sentence (at least ~8 characters).';
    document.getElementById('purpose').focus();
    return;
  }
  if (!rateAllow()) {
    err.textContent = '⏱  You have hit this session\u2019s build limit (6 in the last hour). Try again later — this keeps the demo friendly for everyone.';
    return;
  }

  btn.disabled = true; btn.textContent = '⏳ Building your agent…';
  const fd = new FormData(form);
  const spec = {
    agent_purpose: purpose,
    agent_name: (fd.get('agent_name') || '').trim(),
    voice_style: fd.get('voice_style'),
    greeting: (fd.get('greeting') || '').trim(),
    key_questions: (fd.get('key_questions') || '').split(',').map(s => s.trim()).filter(Boolean),
  };
  try {
    const r = await fetch('/provision-agent', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(spec),
    });
    let j = {};
    try { j = await r.json(); } catch(_) {}
    if (!r.ok) throw new Error(j.hint || j.error || ('Request failed (' + r.status + ')'));

    ok.innerHTML = '✅ <strong>' + (j.agent_name || 'Your agent') + '</strong> is live. '
      + 'Short URL: <span class="short-code">' + (j.short_url || '') + '</span>'
      + '<div class="row-actions">'
      + '  <button type="button" id="copy-short">📋 Copy URL</button>'
      + '  <button type="button" id="go-test">🎧 Talk to it now →</button>'
      + '</div>';
    ok.classList.add('show');

    const copyBtn = document.getElementById('copy-short');
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(j.short_url || '');
        copyBtn.textContent = '✅ Copied';
        setTimeout(() => { copyBtn.textContent = '📋 Copy URL'; }, 1800);
      } catch(_) {
        copyBtn.textContent = '⚠ Copy failed';
      }
    });
    document.getElementById('go-test').addEventListener('click', () => {
      window.location.href = j.test_url;
    });
    resetForm();
  } catch (x) {
    err.textContent = '❌ ' + (x && x.message ? x.message : 'Something went wrong. Please try again.');
    resetForm();
  }
});

// Examples — click to auto-fill form
const EXAMPLES = [
  {
    role: "Dental Practice",
    title: "Dental receptionist — Riley",
    teaser: "Books cleanings, collects name + phone, confirms appointment day/time.",
    spec: {
      agent_purpose: "books cleanings for Dr. Singh's family dental practice",
      agent_name: "Riley",
      voice_style: "warm_female",
      greeting: "Hi, thanks for calling Dr. Singh's office — how can I help?",
      key_questions: "caller's full name, phone number, reason for visit, preferred appointment day and time",
    }
  },
  {
    role: "Mortgage",
    title: "Refinance lead qualifier — Marco",
    teaser: "Qualifies mortgage refi leads — property type, loan balance, credit range.",
    spec: {
      agent_purpose: "qualifies mortgage refinance leads for a direct lender",
      agent_name: "Marco",
      voice_style: "professional_male",
      greeting: "Hi, thanks for calling about refinancing — got a minute for a few quick questions?",
      key_questions: "caller's name, zip code, current mortgage balance, current rate, credit score range, goal of refi",
    }
  },
  {
    role: "Restaurant",
    title: "Italian bistro reservations — Luca",
    teaser: "Takes reservations — party size, date/time, caller name.",
    spec: {
      agent_purpose: "takes reservations for Trattoria Milano, a 40-seat Italian bistro",
      agent_name: "Luca",
      voice_style: "warm_male",
      greeting: "Ciao! Thanks for calling Trattoria Milano — how can I help?",
      key_questions: "party size, date and time, caller name, phone number, any dietary restrictions",
    }
  },
  {
    role: "Solar Sales",
    title: "Solar lead qualifier — Jordan",
    teaser: "Qualifies solar-install leads — home ownership, roof condition, bill size.",
    spec: {
      agent_purpose: "qualifies residential solar installation leads for SunBright Energy",
      agent_name: "Jordan",
      voice_style: "energetic_female",
      greeting: "Hey, thanks for calling SunBright — excited to help you look at solar. Got five minutes?",
      key_questions: "caller's name, home address, home ownership status, roof age, current electric bill, motivation",
    }
  }
];

// Plain-text escape so a pathological EXAMPLES entry can't inject HTML.
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function applyExample(ex) {
  document.getElementById('purpose').value = ex.spec.agent_purpose;
  document.getElementById('name').value = ex.spec.agent_name;
  document.getElementById('voice').value = ex.spec.voice_style;
  document.getElementById('greeting').value = ex.spec.greeting;
  document.getElementById('questions').value = ex.spec.key_questions;
  document.getElementById('spec-form').scrollIntoView({behavior:'smooth',block:'center'});
  document.getElementById('purpose').focus();
}

const grid = document.getElementById('example-grid');
EXAMPLES.forEach((ex) => {
  const div = document.createElement('div');
  div.className = 'example';
  div.setAttribute('role', 'button');
  div.setAttribute('tabindex', '0');
  div.setAttribute('aria-label', 'Use ' + ex.title + ' as a starting point');
  div.innerHTML = `
    <div class="role">${esc(ex.role)}</div>
    <div class="title">${esc(ex.title)}</div>
    <div class="teaser">${esc(ex.teaser)}</div>
    <div class="cta">⚡ Build this →</div>
  `;
  div.addEventListener('click', () => applyExample(ex));
  div.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault(); applyExample(ex);
    }
  });
  grid.appendChild(div);
});
</script>
</body>
</html>
"""

TEST_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Test your agent · Retell Copilot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  :root{--bg:#06070a;--card:#13151b;--ink:#f5f5f7;--muted:#a0a0ab;--dim:#6a6a75;--accent:#a78bfa;--accent-2:#67e8f9;--grad:linear-gradient(120deg,#a78bfa 0%,#67e8f9 100%);--border:rgba(255,255,255,.08);--ok:#34d399;--danger:#f87171;}
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:'Inter',-apple-system,system-ui,sans-serif;min-height:100vh;}
  a{color:var(--accent-2);}
  .bg-orb{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden;}
  .bg-orb::before{content:"";position:absolute;top:-200px;left:50%;transform:translateX(-50%);width:900px;height:600px;border-radius:50%;filter:blur(140px);background:radial-gradient(circle,#a78bfa40 0%,transparent 70%);}
  .wrap{position:relative;z-index:1;max-width:680px;margin:0 auto;padding:56px 24px;}
  .back{color:var(--muted);font-size:14px;text-decoration:none;display:inline-flex;align-items:center;gap:6px;margin-bottom:24px;}
  .back:hover{color:var(--ink);}
  h1{font-size:40px;letter-spacing:-.025em;margin:0 0 8px;font-weight:800;}
  h1 .grad{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;}
  .sub{color:var(--muted);margin:0 0 24px;font-size:16px;}
  .card{background:var(--card);border:1px solid var(--border);border-radius:24px;padding:36px;margin:24px 0;}
  .call-btn{background:var(--grad);color:#06070a;border:0;border-radius:999px;padding:22px 42px;font-weight:800;font-size:18px;cursor:pointer;transition:transform .08s,filter .15s;font-family:inherit;display:inline-flex;align-items:center;gap:10px;}
  .call-btn:hover{transform:translateY(-1px);filter:brightness(1.05);}
  .call-btn:disabled{opacity:.5;cursor:wait;}
  .end-btn{background:transparent;color:var(--ink);border:1px solid var(--border);border-radius:999px;padding:12px 24px;font-weight:600;cursor:pointer;margin-left:12px;font-family:inherit;}
  .end-btn:hover{border-color:var(--danger);color:var(--danger);}
  #status{color:var(--muted);margin-top:16px;font-size:14px;min-height:22px;display:flex;align-items:center;justify-content:center;gap:8px;}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;}
  .on{background:var(--ok);animation:pulse 1.4s infinite;}
  .off{background:#444;}
  .err{background:var(--danger);}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
  .transcript{background:rgba(0,0,0,.25);border:1px solid var(--border);border-radius:16px;padding:16px;margin-top:18px;min-height:120px;max-height:260px;overflow-y:auto;display:none;text-align:left;font-family:'JetBrains Mono',monospace;font-size:13px;line-height:1.6;}
  .transcript.show{display:block;}
  .line{margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.05);}
  .line:last-child{border-bottom:0;}
  .line .who{color:var(--accent-2);font-weight:600;margin-right:6px;}
  .line.agent .who{color:var(--accent);}
  .line.user .who{color:var(--ok);}
  .meta-box{text-align:left;margin-top:24px;padding:18px;background:rgba(0,0,0,.2);border:1px solid var(--border);border-radius:16px;font-size:13px;color:var(--muted);}
  .meta-box .k{color:var(--dim);display:inline-block;width:110px;}
  .meta-box .v{color:var(--ink);font-family:'JetBrains Mono',monospace;font-size:12px;}
  .share{margin-top:18px;font-size:13px;color:var(--muted);}
  .share button{background:transparent;color:var(--accent-2);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer;margin-left:6px;font-family:inherit;}
  .share button:hover{border-color:rgba(167,139,250,.4);}
</style>
</head>
<body>
<div class="bg-orb"></div>
<div class="wrap">
  <a class="back" href="/">← Back to Copilot</a>
  <h1>🎤 <span class="grad">Test your agent</span></h1>
  <p class="sub">Your brand-new Retell agent is live. Click below and talk to it in the browser.</p>

  <div class="card" style="text-align:center;">
    <button class="call-btn" id="startBtn">🎤 Call my agent</button>
    <button class="end-btn" id="endBtn" style="display:none;">⏹ End call</button>
    <div id="status"><span class="dot off"></span>Idle — click the button to start</div>
    <div class="transcript" id="transcript"></div>
  </div>

  <div class="meta-box" id="meta">
    <div><span class="k">Agent ID</span><span class="v">__AGENT_ID__</span></div>
    <div style="margin-top:6px;"><span class="k">Short URL</span><span class="v">__SHORT_URL__</span></div>
    <div class="share">
      Share this test: <span class="v" id="full-url"></span>
      <button id="copy-btn">📋 Copy</button>
    </div>
  </div>

  <p style="color:var(--dim);font-size:13px;margin-top:20px;text-align:center;">
    Want another agent? <a href="/">Back to Copilot →</a>
  </p>
</div>

<script type="module">
import { RetellWebClient } from 'https://esm.sh/retell-client-js-sdk@latest';

const AGENT_ID = '__AGENT_ID__';
const SHORT_URL = '__SHORT_URL__';

const startBtn = document.getElementById('startBtn');
const endBtn = document.getElementById('endBtn');
const statusEl = document.getElementById('status');
const transcriptEl = document.getElementById('transcript');
const fullUrlEl = document.getElementById('full-url');
const copyBtn = document.getElementById('copy-btn');

fullUrlEl.textContent = window.location.origin + '/t/' + SHORT_URL;
copyBtn.addEventListener('click', () => {
  navigator.clipboard.writeText(fullUrlEl.textContent);
  copyBtn.textContent = '✓ Copied';
  setTimeout(() => copyBtn.textContent = '📋 Copy', 1500);
});

function setStatus(dotClass, text) {
  statusEl.innerHTML = `<span class="dot ${dotClass}"></span>${text}`;
}

const client = new RetellWebClient();
let lines = [];
function render() {
  transcriptEl.classList.add('show');
  transcriptEl.innerHTML = lines.map(l => `<div class="line ${l.role}"><span class="who">${l.role === 'agent' ? '🤖' : '👤'}</span>${l.text}</div>`).join('');
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

client.on('call_started', () => {
  setStatus('on', 'Connected — talk!');
  endBtn.style.display = 'inline-flex';
  startBtn.style.display = 'none';
});
client.on('call_ended', () => {
  setStatus('off', 'Call ended. Click to call again.');
  endBtn.style.display = 'none';
  startBtn.style.display = 'inline-flex';
  startBtn.disabled = false;
  startBtn.textContent = '🎤 Call again';
});
client.on('error', (e) => {
  setStatus('err', 'Error: ' + (e.message || e));
  startBtn.disabled = false;
  startBtn.textContent = '🎤 Call my agent';
});
client.on('update', (u) => {
  // transcript updates from Retell: { transcript: [{role, content}, ...] }
  const t = u.transcript || [];
  if (Array.isArray(t) && t.length) {
    lines = t.map(x => ({ role: (x.role === 'agent' || x.role === 'assistant') ? 'agent' : 'user', text: x.content || '' }));
    render();
  }
});

startBtn.addEventListener('click', async () => {
  startBtn.disabled = true;
  startBtn.textContent = '⏳ Connecting…';
  setStatus('on', 'Minting web-call token…');
  try {
    const r = await fetch('/create-web-call', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({agent_id: AGENT_ID})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'token failed');
    setStatus('on', 'Starting call — grant mic permission if prompted…');
    await client.startCall({ accessToken: j.access_token });
  } catch (x) {
    setStatus('err', 'Error: ' + x.message);
    startBtn.disabled = false;
    startBtn.textContent = '🎤 Call my agent';
  }
});
endBtn.addEventListener('click', () => { client.stopCall(); });
</script>
</body>
</html>
"""


def _method(event):
    ctx = event.get("requestContext") or {}
    return (ctx.get("http") or {}).get("method") or event.get("httpMethod") or "GET"


def _path(event):
    ctx = event.get("requestContext") or {}
    return (ctx.get("http") or {}).get("path") or event.get("rawPath") or "/"


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def handler(event, context):
    method = _method(event)
    path = _path(event)

    if method == "OPTIONS":
        return _resp(200, {})

    if method == "GET":
        if path == "/" or path == "":
            return _resp(200, LANDING_HTML, "text/html; charset=utf-8")
        if path.startswith("/test/"):
            agent_id = path[len("/test/"):].split("/")[0].strip()
            short = ""
            # Find short code by scanning (cheap in practice — few active codes for a demo)
            try:
                resp = _codes.scan(FilterExpression="agent_id = :a", ExpressionAttributeValues={":a": agent_id}, Limit=1)
                items = resp.get("Items") or []
                if items:
                    short = items[0].get("code", "")
            except Exception:
                pass
            html = TEST_HTML.replace("__AGENT_ID__", agent_id).replace("__SHORT_URL__", short or "n/a")
            return _resp(200, html, "text/html; charset=utf-8")
        if path.startswith("/t/"):
            code = path[len("/t/"):].split("/")[0].strip().lower()
            agent_id = _lookup_code(code)
            if not agent_id:
                return _resp(404, "<h1>Short code not found</h1><p><a href='/'>Back to Copilot</a></p>", "text/html")
            base = _base_url(event)
            return _resp(302, "", "text/html", {"Location": f"{base}/test/{agent_id}"})
        if path == "/healthz":
            return _resp(200, {"ok": True, "time": datetime.now(timezone.utc).isoformat()})
        return _resp(404, {"error": "not_found"})

    body = _body(event)

    if path == "/provision-agent":
        spec = body.get("args") or body
        required = ["agent_purpose", "agent_name", "voice_style", "greeting", "key_questions"]
        missing = [k for k in required if not spec.get(k)]
        if missing:
            return _resp(400, {"error": "missing_fields", "missing": missing})
        # Input validation — keep the form honest and the prompt bounded
        caps = {"agent_purpose": 400, "agent_name": 60, "voice_style": 60, "greeting": 400}
        for k, n in caps.items():
            v = spec.get(k)
            if isinstance(v, str) and len(v) > n:
                return _resp(400, {"error": "field_too_long", "field": k, "max": n})
        if len((spec.get("agent_purpose") or "").strip()) < 8:
            return _resp(400, {"error": "agent_purpose_too_short", "hint": "Describe what the agent should do in a full sentence."})
        kq = spec.get("key_questions")
        if isinstance(kq, list) and len(kq) > 12:
            return _resp(400, {"error": "too_many_questions", "max": 12})
        s, res = _provision_agent(spec)
        if s != 200:
            return _resp(s, res)
        code = _new_short_code()
        _store_code(code, res["agent_id"], meta={"name": spec.get("agent_name"), "purpose": spec.get("agent_purpose")})
        base = _base_url(event)
        short_url = f"{base}/t/{code}"
        test_url = f"{base}/test/{res['agent_id']}"
        return _resp(200, {
            "agent_id": res["agent_id"],
            "agent_name": spec.get("agent_name"),
            "test_url": test_url,
            "short_url": short_url,
            "short_code": code,
            "phone_number": None,
            "message": f"Your agent {spec.get('agent_name')} is live. Short URL: {short_url}"
        })

    if path == "/create-web-call":
        agent_id = (body.get("agent_id") or "").strip()
        if not agent_id:
            return _resp(400, {"error": "agent_id required"})
        s, res = _create_web_call(agent_id)
        return _resp(s, res)

    if path == "/copilot-webhook":
        return _resp(200, {"ok": True})

    return _resp(404, {"error": "not_found", "path": path, "method": method})
