# Retell Copilot

**A Retell voice agent that builds Retell voice agents.** Describe it on a phone call or a web form, and in about sixty seconds you get back a 5-character URL you can open in any browser to talk to your fresh agent.

> Built by **[Shanto Mathew](https://www.linkedin.com/in/shantomathew/)** as a working demo for the **Retell AI Forward-Deployed Engineer** interview. One engineer, one day, the full stack.

---

## 🎯 Try it

| Path | How |
| --- | --- |
| 📞 **Phone** | Call **+1 (940) 353-0737** |
| 🌐 **Web** | [retell-copilot-demo.netlify.app](https://retell-copilot-demo.netlify.app/) |

The Copilot agent asks what kind of voice agent you want, provisions it live, and reads back a short URL like `/t/abxjm` over the phone (spelled in NATO phonetic so you can write it down). Visit the URL, hit the call button, talk to a brand-new agent in your browser. Zero signup, zero phone-number purchase.

---

## 🧱 Architecture

```
Caller on phone ── Retell (Copilot agent)            Web-form user
       │                     │                              │
       │              tool: provision_agent                 │
       │                     │                              │
       └─────────────▶ POST /provision-agent ◀──────────────┘
                               │
                               ▼
                  AWS Lambda  retell-copilot-api
                 (behind HTTP API Gateway)
                               │
                               ▼
                    Retell REST API
                    ├─ create-retell-llm
                    └─ create-agent
                               │
                               ▼
                  DynamoDB short-code mapping
                     (5 chars · 6.4M keyspace)
                               │
                               ▼
             { agent_id, short_url, test_url }
                               │
                               ▼
    Phone caller hears short URL   Web user redirected to /test/{agent_id}
                                            │
                                            ▼
                          Browser test page using Retell Web SDK
                          starts a live WebRTC call to the new agent
```

Everything in the pipeline is a thing a Retell FDE would touch in their first month.

## 📦 Stack

- **Voice:** Retell AI + ElevenLabs voices (Marissa · Adrian · Kate · Brian · Anna)
- **LLM:** GPT-4.1 on Retell's managed LLM service
- **Backend:** single-file Python 3.12 AWS Lambda, behind HTTP API Gateway
- **State:** DynamoDB (short-code mapping, on-demand billing)
- **Edge:** Netlify (static landing + proxy rewrites to the Lambda API)
- **Infra as code:** idempotent shell deploy script — `./lambda/deploy.sh`

## 🗂 Repo layout

```
lambda/
  handler.py          — single-file Lambda; serves landing HTML, test page,
                        POST endpoints (/provision-agent, /create-web-call,
                        /copilot-webhook), /healthz
  deploy.sh           — idempotent deploy script (creates role, zips code,
                        updates env, bumps function)
retell/
  copilot_prompt.txt  — voice-Copilot system prompt
  create_llm.py       — one-shot LLM creation (prompt + provision_agent tool)
  create_agent.py     — one-shot agent + phone-binding
netlify/
  build.sh            — extracts landing HTML from handler.py, writes dist/
                        with _redirects + netlify.toml
  netlify.toml        — security headers + publish config
```

## 🔌 Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`  | `/` | Landing page |
| `POST` | `/provision-agent` | Create a new Retell LLM + agent from a spec. Returns `{agent_id, short_url, test_url}` |
| `POST` | `/create-web-call` | Mint a Retell Web SDK access token for a given agent |
| `POST` | `/copilot-webhook` | Retell custom-tool webhook target for the Copilot's `provision_agent` tool |
| `GET`  | `/t/{code}` | 302-redirect a 5-char short code to the agent's test page |
| `GET`  | `/test/{agent_id}` | In-browser WebRTC test page |
| `GET`  | `/healthz` | liveness |

## 🚀 Deploy

Prereqs: an AWS account you own, a Retell AI API key, `aws` CLI, `zip`.

```bash
export RETELL_API_KEY="key_..."
export AWS_DEFAULT_REGION="us-east-1"
./lambda/deploy.sh
```

Then build and push the Netlify mirror:

```bash
export NETLIFY_AUTH_TOKEN="..."
cd netlify && ./build.sh
netlify deploy --dir=dist --prod --site <your-site-id>
```

Set `PUBLIC_BASE_URL` on the Lambda to your Netlify domain (e.g. `https://retell-copilot-demo.netlify.app`) so generated short URLs point at the edge, not the raw API Gateway.

## 🧪 What it does well

- **Voice-first UX.** The Copilot NATO-spells the short code so a caller can write it down without a keyboard.
- **Dual delivery.** Same backend drives a phone call and a WebRTC browser test harness.
- **Production-lean.** ~300 lines of `handler.py`, zero servers, cold start well under a second.
- **Idempotent deploys.** `deploy.sh` can be run any number of times.
- **No-leak routing.** The `/t/{code}` redirect is proxied through Netlify so the user never sees the raw Lambda URL in the address bar.

## 📝 License

MIT — see `LICENSE`.

## 📬 Contact

Shanto Mathew · [shanto12@gmail.com](mailto:shanto12@gmail.com) · [linkedin.com/in/shantomathew](https://www.linkedin.com/in/shantomathew/)
