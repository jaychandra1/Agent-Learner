# Configuration reference

Every setting PQ reads. Plain settings live in the ConfigMap `pq-config` in [`deploy/deployment.yaml`](../deploy/deployment.yaml); secrets live in the Secret `pq-secrets`, created with `kubectl` ([INSTALLATION.md, Step 3](INSTALLATION.md)). Both reach the pod as environment variables with the same names. PQ reads them once, when the pod starts.

After a change:

```bash
kubectl apply -f deploy/deployment.yaml && kubectl -n pq rollout restart deploy/pq
```

For a Secret value, see *Change a secret* in [OPERATIONS.md](OPERATIONS.md#day-to-day).

---

## Secret `pq-secrets`

| Key | Default | Meaning |
|---|---|---|
| `LLM_API_KEY` | – | API key of the AI provider chosen with `LLM_PROVIDER`. The provider's own name works too (`GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `CURSOR_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `OLLAMA_API_KEY`) and wins when both are set. Required for every provider except `ollama` and `custom`. Without it PQ can't answer. |
| `AGENT_API_KEY` | – | Required `x-api-key` header for the internal API (`/chat`, `/status`, `/teams/test`, `/security/restricted`). Without it the internal API is **off** (403). The health checks `/healthz` and `/ready` need no key. |
| `TEAMS_REPLY_WEBHOOK_URL` | – | Workflows webhook of the questions channel. Every answer to a Teams question is posted here as one or more Adaptive Cards. Each card carries `threadMessageId` and `channelId`, so the workflow can reply in the question's thread. May be written as `name=URL`; the name (default `reply-1`) appears in logs and `/status` instead of the URL. Only the first URL is used. Without it a question in Teams gets "no reply webhook is set up" and no answer. |
| `TEAMS_OUTGOING_WEBHOOK_SECRET` | – | The outgoing webhook's security token. PQ checks the HMAC signature of every Teams message with it. Without it `/teams/webhook` is off (404). |
| `TEAMS_SECURITY_WEBHOOK_URLS` | – | Where the "someone asked about PQ itself" notices go: Workflows webhook URL(s) of a chat or channel that only administrators can read. Comma-separated; each entry optionally `name=URL` (default names `security-1`, `security-2`, …). Without it refusals are only written to the log and kept in `GET /security/restricted`. |

The Secret `pq-tunnel` holds `TUNNEL_TOKEN` for the Cloudflare Tunnel pods ([INSTALLATION.md, Step 5](INSTALLATION.md)).

Webhook URLs and the tunnel token work like passwords: anyone who has one can post to that channel or connect to that tunnel.

---

## ConfigMap `pq-config`

### General

| Setting | Default | Meaning |
|---|---|---|
| `TIMEZONE` | `UTC` | Time zone for the times on the administrators' cards: an IANA name such as `Asia/Kolkata` or `Europe/London`. An unknown name falls back to UTC (with a warning in the log). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Health-check requests are never logged. |

### AI provider ([AI_PROVIDERS.md](AI_PROVIDERS.md))

An AI provider is required: PQ answers every question with one call to the model. Without one, PQ replies that no AI model is configured.

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | not set in code (no AI model); `cursor` in `deployment.yaml` | `groq`, `anthropic`, `cursor`, `gemini`, `openai`, `azure-openai`, `openrouter`, `mistral`, `ollama` or `custom`. Only this provider is used. `none` (or empty) = no AI model. Also accepted: `claude` (= `anthropic`), `azure` and `foundry` (= `azure-openai`), `google` (= `gemini`). |
| `LLM_MODEL` | provider default | Model name; Azure OpenAI: the deployment name. Required for `openai`, `azure-openai`, `ollama`, `custom`. |
| `LLM_BASE_URL` | provider default | API endpoint. Required for `azure-openai`, `ollama`, `custom`. |
| `LLM_EFFORT` | provider default | Reasoning effort: `low`, `medium`, `high` (Claude also `xhigh`, `max`); `none` = not sent. Not used by Cursor. |
| `LLM_MAX_REQUEST_CHARS` | provider default | Size of one request in characters: the rules plus the conversation. This also decides how much of a conversation the model sees: when a conversation gets longer than this, the oldest turns are dropped until it fits. The newest question is always sent. Raise it on a paid plan for a longer memory. Not used by Cursor. |
| `LLM_MAX_OUTPUT_TOKENS` | provider default | Longest reply, in tokens. When a reply reaches it, PQ ends the answer with *"The answer reached the length limit. Reply `continue` for the rest."* Raise it if explanations get cut off. Not used by Cursor. |
| `LLM_TIMEOUT_SECONDS` | provider default | Longest one model call may take (Cursor: one whole run) |
| `LLM_FALLBACKS` | `default` | Claude only: server-side refusal fallback; `off` to disable |
| `ANSWER_MAX_WORDS` | `1200` | The length a full explanation should stay under, code not counted. It is written into the AI's rules (rule 10 in [RULES.md, section 1](RULES.md#1-the-ais-rules)); the model is asked to keep to it, nothing cuts the answer. `0` = no limit in the rules. |

A number that can't be read (e.g. `LLM_TIMEOUT_SECONDS: "fast"`) switches the AI model off; `/status` → `llm.status` then names the setting.

Provider defaults, from [`agent/providers/catalog.py`](../agent/providers/catalog.py):

| Provider | Default model | Request size (chars) | Longest reply (tokens) | Timeout (s) |
|---|---|---|---|---|
| `groq` | `openai/gpt-oss-120b` | 14,000 | 2,500 | 120 |
| `anthropic` | `claude-opus-5-5` (effort `high`) | 200,000 | 16,000 | 300 |
| `cursor` | `composer-2.5` | – | – | 600 |
| `gemini` | `gemini-flash-latest` | 60,000 | 4,000 | 120 |
| `openai` | set `LLM_MODEL` | 60,000 | 4,000 | 120 |
| `azure-openai` | set `LLM_MODEL` and `LLM_BASE_URL` | 60,000 | 4,000 | 120 |
| `openrouter` | `openai/gpt-oss-120b` | 60,000 | 4,000 | 120 |
| `mistral` | `mistral-large-latest` | 60,000 | 4,000 | 120 |
| `ollama`, `custom` | set `LLM_MODEL` and `LLM_BASE_URL` | 60,000 | 4,000 | 120 |

Things that follow from these numbers:

- The rules take about 7,000 characters of every request. On Groq's default of 14,000, roughly 6,000 characters of conversation fit, so follow-ups see only the last one or two answers.
- Groq's 2,500 reply tokens are less than a 1,200-word explanation with code can need. If answers often end with the `continue` line, raise `LLM_MAX_OUTPUT_TOKENS` (paid plan) or lower `ANSWER_MAX_WORDS`.
- Apart from the size limit, a conversation is cut to its last 20 messages (10 questions and answers). Cursor gets the last 6 messages as text.

`LLM_MAX_STEPS` and `LLM_TOOL_OUTPUT_CHARS` are still read by the provider code, but they have no effect: PQ uses no tools and makes one model call per answer.

### Teams access

| Setting | Default | Meaning |
|---|---|---|
| `TEAMS_TENANT_ID` | `""` | Only answer messages from this Microsoft 365 tenant (recommended). Others get "This agent only answers its own organization." |
| `TEAMS_ALLOWED_USER_IDS` | `""` | Only answer these people: comma-separated Entra object IDs (recommended). Others get "you're not on this agent's allowed-users list". `""` = everyone who can mention the outgoing webhook. |

`deployment.yaml` ships with real IDs in both settings. Replace them with your own ([INSTALLATION.md, Step 4.2](INSTALLATION.md)).

### Web UI ([WEB_UI.md](WEB_UI.md))

| Setting | Default | Meaning |
|---|---|---|
| `WEB_UI` | `""` | Address of the web UI, e.g. `https://pq-ui.onrender.com` (comma separated for several, `*` for any). Browsers may call the chat API (`POST /api/chat` on the public port) only from these addresses. `""` = that API is off. |
| `WEB_ACCESS_CODE` (Secret) | not set | A shared code every call to the chat API must carry (`X-Access-Code` header; the UI asks for it once). Recommended whenever `WEB_UI` is set. |
| `WEB_RATE_PER_MINUTE` | `20` | Questions one address may ask per minute through the chat API. |
| `PORT` | not set | Set by hosting platforms such as Render. The public port (Teams webhook and chat API) then listens there instead of 8080. |

The UI itself has one build-time variable, `BACKEND_URL`: the backend's address.

### Boundaries

What people in Teams may not ask ([RULES.md, section 3](RULES.md#3-boundaries-what-may-not-be-asked)).

| Setting | Default | Meaning |
|---|---|---|
| `BOUNDARIES_ENABLED` | `true` | Refuse and report questions about PQ itself: its setup, AI model, rules, prompt and namespace. `false` switches both layers off, the question filter and the AI's rule 12 (not recommended). |
| `PROTECTED_NAMESPACES` | `pq` | Comma-separated namespace names nobody in Teams may ask about. A question that names one (`the pq namespace`, `pods in pq`, `pq-tunnel`) is refused, and the names are listed in the AI's rule 12. Set it to the namespace PQ is installed in. `none` protects no namespace: the rest of the filter still runs. |

PQ can't read any namespace. This setting only decides which names a question may not mention.

The boundaries apply to questions from Teams. The internal API and the command line are for the administrators and are not restricted.

---

## Fixed in the code

These are not settings. To change one, edit the code and build a new image.

| Value | Where | Meaning |
|---|---|---|
| 5 seconds | Teams | How long Teams waits for the reply to an outgoing webhook. PQ answers "Working on it" at once and sends the explanation afterwards. |
| 4 cards of 8,000 characters | `agent/cards.py` | The most one answer can take in Teams. The rest is cut, with a note to ask for it with `continue`. |
| 2 answers at a time, 6 running or waiting | `agent/teams.py` | Above that, people get "I'm busy with other questions right now." |
| 200 conversations, 24 hours idle | `agent/teams.py` | Conversations kept in memory (one per Teams thread, one per `/chat` session). The least recently used go first. All are lost when the pod restarts. |
| 3 attempts, 15-second timeout | `agent/teams.py` | Posting a card to a Workflows webhook (retried on 429, server errors and network errors) |
| 20 cards per hour | `agent/boundaries.py` | Notices about refused questions sent to the administrators. Further refusals in that hour are only logged and kept. |
| 200 refusals | `agent/boundaries.py` | Kept in memory for `GET /security/restricted` |

---

## Set in the image or manifest, not the ConfigMap

| Setting | Where | Meaning |
|---|---|---|
| Ports `8000` and `8080` | `agent/main.py`, `agent/Dockerfile`, `deploy/deployment.yaml` | 8000: internal API and health checks. 8080: `POST /teams/webhook` only, the Cloudflare Tunnel's target. |
| `HOME` | `/tmp` (Dockerfile) | The only writable place in the pod; the Cursor SDK keeps its state there |
| `WITH_CURSOR` | build argument, `true` | `false` builds the image without the Cursor SDK |
| `UNDICI_VERSION`, `UNDICI_INTEGRITY` | constants in `agent/docker/patch_cursor_bridge.py` | The fixed `undici` release the image build puts into the Cursor SDK, and its SHA-512 |
| Image, replicas, resources, probes | `deploy/deployment.yaml` | Edited directly in the Deployment. Keep `replicas: 1`: conversations are in the pod's memory. |
| `cloudflared` image tag | `deploy/cloudflare-tunnel.yaml` | Pinned to a release; updated on purpose ([INSTALLATION.md, Step 7](INSTALLATION.md)) |
| Cloudflare route and firewall rules | Cloudflare dashboard | [INSTALLATION.md, Step 5](INSTALLATION.md) |
