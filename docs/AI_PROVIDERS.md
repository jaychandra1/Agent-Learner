# AI providers

PQ uses an AI model to write every answer. **One Docker image supports every provider below.** You choose one with a single ConfigMap setting, `LLM_PROVIDER`, and put its key in the Secret as `LLM_API_KEY`. Only that provider is ever called.

**A provider is required.** Without one (`LLM_PROVIDER: "none"`, or a missing key) PQ starts, but answers every question with "No AI model is configured, so I can't answer questions yet." There is no fallback.

The code lives in one folder, [`agent/providers/`](../agent/providers); how it plugs in is shown in [ARCHITECTURE.md](ARCHITECTURE.md#4-choosing-the-ai-provider).

---

## 1. Choose a provider

| `LLM_PROVIDER` | Service | Key | Default model (`LLM_MODEL`) | Good to know |
|---|---|---|---|---|
| `groq` | [Groq](https://console.groq.com) | `gsk_…` | `openai/gpt-oss-120b` | Free plan available. Its small limits make answers slow and cut long lessons short (section 5). |
| `anthropic` (or `claude`) | [Anthropic Claude](https://console.anthropic.com) | `sk-ant-…` | `claude-opus-5-5` | The largest limits: long explanations and long conversations fit. Paid per token. |
| `cursor` | [Cursor](https://cursor.com) (Cursor SDK) | Cursor API key | `composer-2.5` | Needs a **paid** Cursor plan. One Cursor agent run per question, without any tools. |
| `gemini` (or `google`) | [Google Gemini](https://aistudio.google.com) | Gemini API key | `gemini-flash-latest` | Free tier available. |
| `openai` | [OpenAI](https://platform.openai.com) | `sk-…` | none: set `LLM_MODEL` | Paid per token. |
| `azure-openai` (or `azure`, `foundry`) | [Azure OpenAI / Microsoft Foundry](https://ai.azure.com) | resource key | none: set `LLM_MODEL` to your **deployment name** | Also set `LLM_BASE_URL`. Needs an Azure subscription. |
| `openrouter` | [OpenRouter](https://openrouter.ai) | `sk-or-…` | `openai/gpt-oss-120b` | One key for many models, some free. |
| `mistral` | [Mistral AI](https://console.mistral.ai) | Mistral key | `mistral-large-latest` | |
| `ollama` | [Ollama](https://ollama.com), self-hosted | none | none: set `LLM_MODEL` | Set `LLM_BASE_URL`, e.g. `http://ollama.ollama.svc:11434/v1`. Data never leaves your network. |
| `custom` | Any OpenAI-compatible Chat Completions API | optional | none: set `LLM_MODEL` | Set `LLM_BASE_URL`. vLLM, LiteLLM, Together, DeepSeek, Amazon Bedrock's OpenAI-compatible endpoint, … |
| `none` | – | – | – | No AI model: PQ can't answer questions. |

**Requirement for `custom` and the OpenAI-compatible ones:** a chat model behind a Chat Completions endpoint. Tool calling (functions) is **not** needed, because PQ sends no tools. Pick a model that explains well and writes correct code; the quality of the lessons is the quality of the model.

### About "Microsoft Copilot" and "GitHub Copilot"

- **Microsoft 365 Copilot** can't be used. Its Chat API works only on behalf of a signed-in user who holds a Copilot licence; a background service like PQ has no user to sign in as.
- **Microsoft's hosted models** *can* be used: choose `azure-openai` (Azure OpenAI / Microsoft Foundry).
- **GitHub Models** was retired in July 2026.
- **GitHub Copilot** has an SDK (GA in June 2026) that could be added later as an agent provider, the same way Cursor is.

---

## 2. Configure it

Three changes, then a restart:

**1. The key**, in the Secret:

```bash
kubectl -n pq patch secret pq-secrets --type merge -p '{"stringData":{"LLM_API_KEY":"<your key>"}}'
```

The provider's own variable name works too (e.g. `GROQ_API_KEY`, `CURSOR_API_KEY`, `GEMINI_API_KEY`); if both are set, the provider's own one wins.

**2. The provider**, in the ConfigMap in [`deploy/deployment.yaml`](../deploy/deployment.yaml):

```yaml
  LLM_PROVIDER: "gemini"
  LLM_MODEL: ""          # "" = the provider's default model
  LLM_BASE_URL: ""       # "" = the provider's default endpoint
```

**3. Apply and restart:**

```bash
kubectl apply -f deploy/deployment.yaml && kubectl -n pq rollout restart deploy/pq
```

**Check** (with the port-forward and `$KEY` from [USAGE.md, section 4](USAGE.md#4-internal-api-administrators)):

```bash
curl -s localhost:8080/status -H "x-api-key: $KEY"
```

Look for `"llm": {"configured": true, "provider": "gemini", "model": "gemini-flash-latest", …}`. When `configured` is `false`, `status` says exactly what is missing, for example `Azure OpenAI (Microsoft Foundry) needs LLM_MODEL and LLM_BASE_URL`. The pod's log says the same at startup: a line starting with `AI model:` when a provider was loaded, a warning ending in `PQ cannot answer questions until an AI provider is configured.` when not.

### Examples per provider

| Provider | Secret | ConfigMap |
|---|---|---|
| Groq | `LLM_API_KEY=gsk_…` | `LLM_PROVIDER: "groq"` |
| Claude | `LLM_API_KEY=sk-ant-…` | `LLM_PROVIDER: "anthropic"` |
| Cursor | `LLM_API_KEY=<Cursor key>` | `LLM_PROVIDER: "cursor"` |
| Gemini | `LLM_API_KEY=<Gemini key>` | `LLM_PROVIDER: "gemini"` |
| OpenAI | `LLM_API_KEY=sk-…` | `LLM_PROVIDER: "openai"`, `LLM_MODEL: "<model name>"` |
| Azure OpenAI | `LLM_API_KEY=<resource key>` | `LLM_PROVIDER: "azure-openai"`, `LLM_BASE_URL: "https://<resource>.openai.azure.com/openai/v1"`, `LLM_MODEL: "<deployment name>"` |
| OpenRouter | `LLM_API_KEY=sk-or-…` | `LLM_PROVIDER: "openrouter"` (optionally `LLM_MODEL: "<vendor>/<model>"`) |
| Ollama in the cluster | – | `LLM_PROVIDER: "ollama"`, `LLM_BASE_URL: "http://ollama.ollama.svc:11434/v1"`, `LLM_MODEL: "<model>"` |
| Any OpenAI-compatible API | `LLM_API_KEY=<key, if needed>` | `LLM_PROVIDER: "custom"`, `LLM_BASE_URL: "https://…/v1"`, `LLM_MODEL: "<model>"` |

---

## 3. Getting a key

| Provider | Steps |
|---|---|
| **Groq** | console.groq.com → **API Keys** → **Create API Key** → copy (starts with `gsk_`, shown once). Free plan limits: https://console.groq.com/docs/rate-limits |
| **Anthropic** | console.anthropic.com → **API Keys** → **Create Key**. Needs billing set up. |
| **Cursor** | Cursor Dashboard → **API Keys** → create a key. A *personal* key bills your own plan. For a team, use a **service account** key from the team settings (Teams/Enterprise plans). Cursor doesn't allow giving third parties usage under your personal account. |
| **Gemini** | aistudio.google.com → **Get API key** → create a key in a Google Cloud project. |
| **OpenAI** | platform.openai.com → **API keys** → **Create new secret key**. |
| **Azure OpenAI / Foundry** | In the Azure portal (or ai.azure.com), create an Azure OpenAI or Foundry resource, **deploy a model** (its deployment name is `LLM_MODEL`), then copy **Keys and Endpoint**. `LLM_BASE_URL` is the endpoint plus `/openai/v1`. |
| **OpenRouter** | openrouter.ai → **Keys** → **Create Key**. |
| **Mistral** | console.mistral.ai → **API Keys**. |

---

## 4. How each kind works

**One call per question.** An answer is exactly one call to the provider: the rules as the system prompt ([RULES.md, section 1](RULES.md#1-the-ais-rules)), the conversation so far as the history, and no tools. There is no loop.

PQ supports two provider shapes ([`providers/base.py`](../agent/providers/base.py)):

| Shape | Providers | What one question is |
|---|---|---|
| **Chat provider** | Claude, and every OpenAI-compatible API (Groq, Gemini, OpenAI, Azure OpenAI, OpenRouter, Mistral, Ollama, custom) | One model call: `create(system, messages, [])`. The history is the thread's conversation, trimmed to 20 messages and to the request size the provider takes. |
| **Agent provider** | Cursor | One agent run: a session with no custom tools, and one message that holds the recent transcript (the last 6 messages) plus the new question. |

**PQ has no tools.** The provider classes can still carry tool definitions and tool calls: that code and its tests were kept, so tools could be added back through the same interface. PQ passes none. The model can only answer in text.

### Claude ([`claude.py`](../agent/providers/claude.py))
- Anthropic Messages API. The `tools` parameter is not sent, because there are none.
- `LLM_EFFORT` (default `medium`; also `low`, `high`, `xhigh`, `max`; `none` = don't send) controls how much the model thinks. Thinking happens before the first word appears, so a lower value answers sooner and a higher one reasons more.
- `LLM_FALLBACKS: "default"` re-runs a refused request on Anthropic's fallback model within the same call; `"off"` disables it.
- The full reply, thinking blocks included, goes into the conversation history as it is. When PQ trims a history, it drops the thinking blocks from what is kept.

### OpenAI-compatible APIs ([`openai_compat.py`](../agent/providers/openai_compat.py))
- PQ converts its conversation to Chat Completions messages on every call, and the reply back.
- **Rate limits:** on "too many requests" (429) or a server error it waits for the `retry-after` time and retries (up to 5 attempts). When the wait would be longer than a minute (a daily limit), it gives up at once and the person gets an error.
- **Request size:** `agent.py` cuts the conversation to `LLM_MAX_REQUEST_CHARS` before the call by dropping the oldest turns. A request the server still calls too large (HTTP 413) is retried, but the provider class can only shorten tool results there and PQ has none, so the fix is a lower `LLM_MAX_REQUEST_CHARS`.
- **Compatibility:** if a server rejects an optional field with HTTP 400 (`reasoning_effort`, `max_completion_tokens`), the request is resent once without it (`max_tokens` instead).
- **Azure OpenAI** sends the key as an `api-key` header; all others use `Authorization: Bearer`.
- `reasoning_effort` is sent for Groq's `openai/gpt-oss*` models by default, and for any model when you set `LLM_EFFORT` to `low`, `medium` or `high`.
- A reply that stops at the length limit (`finish_reason: length`) is marked as cut, and PQ adds the "reply `continue`" line. A reply stopped by the provider's content filter is shown as "The model declined this request."

### Cursor ([`cursor.py`](../agent/providers/cursor.py))
- Uses the official Cursor SDK (`cursor-sdk`, included in the image). On Cursor's forum, a Cursor team answer says using the SDK as a backend AI service inside your product is a supported use, billed under the plan of the key's account.
- Each question creates a short-lived Cursor agent with:
  - `tools=[]`: no tool group is switched on, so the model has no tools at all and can only answer in text. No shell, no file access, no web; `shell` is also explicitly disallowed;
  - no custom tools and no MCP server;
  - an empty working folder (`pq-cursor` under the temp directory, `/tmp/pq-cursor` in the pod), no project or user settings loaded.
- The rules go out with the message, followed by a note that the model has no tools here.
- Each question is a new agent, so Cursor keeps no conversation itself. PQ puts the recent transcript into the message ("Conversation so far: … New question: …").
- One Cursor run at a time, for the whole pod. `LLM_TIMEOUT_SECONDS` (default 600) cancels a run that takes too long.
- The SDK starts a helper process (its "bridge") on first use. `HOME` is `/tmp` in the image, the only writable place in the pod. The pod's memory limit of 1Gi leaves room for it.
- The SDK's helper bundles its own Node.js packages. One of them, `undici`, is an unmaintained version with known vulnerabilities, so the image build replaces it with a fixed, checksum-verified release and checks that the helper still loads ([SECURITY.md](SECURITY.md)).
- Building the image without Cursor: `docker build --build-arg WITH_CURSOR=false agent` (the SDK has a proprietary licence; check Cursor's terms before publishing an image that includes it).
- What tests can't cover is a full run against Cursor's service, which needs a real key.

---

## 5. Limits per provider

Defaults from [`providers/catalog.py`](../agent/providers/catalog.py). Every value can be overridden in the ConfigMap.

| Setting | Groq (free tier) | Claude | Cursor | All others |
|---|---|---|---|---|
| `LLM_MAX_OUTPUT_TOKENS`: the longest reply | 2,500 | 16,000 | – | 4,000 |
| `LLM_MAX_REQUEST_CHARS`: one request (the rules + the conversation) | 14,000 | 200,000 | – | 60,000 |
| `LLM_TIMEOUT_SECONDS` | 120 per call | 300 per call | 600 per run | 120 per call |
| `LLM_EFFORT` | `low` (gpt-oss models) | `medium` | – | not sent |
| `LLM_MAX_STEPS` (no effect) | 8 | 14 | 2 | 12 |
| `LLM_TOOL_OUTPUT_CHARS` (no effect) | 2,500 | 15,000 | 15,000 | 8,000 |

**`LLM_MAX_OUTPUT_TOKENS` bounds the length of an explanation.** A full lesson of about 1,200 words plus a code example can need more than 2,500 tokens (a reasoning model's thinking counts too), so on the free tier default long lessons can get cut. The person then sees *"The answer reached the length limit. Reply `continue` for the rest."* Two ways out:
- raise `LLM_MAX_OUTPUT_TOKENS` in the ConfigMap (for example `"4000"`), if your plan's per-minute token limit allows a reply of that size;
- or lower `ANSWER_MAX_WORDS` so the model plans a shorter lesson that fits.

**`LLM_MAX_REQUEST_CHARS` bounds how much conversation is sent.** The rules take about 6,500 characters of every request (about 7,500 for requests from Teams, which carry rule 12). What is left is for the conversation; the oldest turns are dropped first and the newest question always stays. With the free tier's 14,000 characters that leaves about 6,000 to 7,000 characters, which is less than one full lesson. So after a long answer, a follow-up such as `go deeper` or `continue` may reach the model without the answer before it. On a **paid Groq plan**, raise `LLM_MAX_REQUEST_CHARS` and `LLM_MAX_OUTPUT_TOKENS`. Cursor has no such setting: it always gets the last 6 messages as text.

**`LLM_MAX_STEPS` and `LLM_TOOL_OUTPUT_CHARS` have no effect** while PQ has no tools. They are still read, and they belong to the tool support the provider classes kept. They are not in the ConfigMap of `deploy/deployment.yaml`.

There is no overall deadline besides `LLM_TIMEOUT_SECONDS` and the retries. On a free plan, waiting out the per-minute limit can make one answer take minutes.

---

## 6. Data and cost

- **What is sent:** the rules, the question, any text the person pasted into it (code, logs, configuration), and the recent conversation of that thread. Nothing else: no cluster data, no files, no Teams history beyond the thread's own questions and PQ's answers. PQ has nothing else to send, because it reads no system. Everything goes over HTTPS to the provider you chose, and to no other.
- **Pasted secrets go to the provider.** PQ does not redact questions on the way to the model. The rules tell the model not to repeat a pasted secret and to tell the person to rotate it. Tell people not to paste credentials. (Redaction is applied to refused questions before they are logged or sent to the administrators.)
- **Self-hosted option:** with `ollama` (or `custom` pointing inside your network), nothing leaves your network.
- **Cost:** free tiers (Groq, Gemini, some OpenRouter models) cost nothing but are rate-limited. Paid providers bill per token (Cursor: per your plan's usage pricing). One question is one call. Its cost is the rules plus the conversation sent, plus the answer. There is no budget setting: cost is bounded by `LLM_MAX_OUTPUT_TOKENS`, `LLM_MAX_REQUEST_CHARS`, the queue (2 answers at a time, at most 6 running or waiting) and, if you set it, `TEAMS_ALLOWED_USER_IDS`.

---

## 7. Adding a provider

- **OpenAI-compatible API:** add one `Spec(...)` line to `CATALOG` in [`providers/catalog.py`](../agent/providers/catalog.py) with its name, endpoint, key variable and default model, then a line for it in the `READY` table of [`tests/test_provider_catalog.py`](../agent/tests/test_provider_catalog.py).
- **Anything else:** write a class in `providers/` that implements `ChatProvider.create()` or `AgentProvider.session()` ([`base.py`](../agent/providers/base.py)), add a `kind` for it in `catalog._build()`, and add tests with a fake of its SDK, as [`tests/test_provider_cursor.py`](../agent/tests/test_provider_cursor.py) does.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `"configured": false` in `GET /status` | Read `llm.status`: it names the missing setting (`LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`) or the unknown provider name. |
| `unknown LLM_PROVIDER '…'` | Typo; the message lists the valid names. |
| PQ answers "No AI model is configured, …" | `LLM_PROVIDER` is empty or `none`, or the provider could not be loaded. Check `GET /status` and the startup lines of the pod's log. |
| Teams shows "Sorry, that failed (ProviderError). …" | The provider's API returned an error. The reason is in the pod's log (`Teams request failed`): wrong key (HTTP 401/403), model name not available to your account (HTTP 404), or a daily limit used up (`rate limit, retry after …`). |
| Answers end with "The answer reached the length limit" | The reply hit `LLM_MAX_OUTPUT_TOKENS`. Raise it, or lower `ANSWER_MAX_WORDS` (section 5). |
| A follow-up doesn't remember the earlier answer | The conversation didn't fit `LLM_MAX_REQUEST_CHARS` (free tier), the follow-up was a new post instead of a reply in the thread, the thread was idle for 24 hours, or the pod restarted. |
| Answers take minutes | Normal on free plans: PQ waits out the per-minute limit (log: `Model API returned 429; retrying in …`). |
| "The model declined this request." | The provider's own safety filter stopped the reply. Rephrase the question. |
| `LLM_MAX_OUTPUT_TOKENS must be a number, not '…'` in `llm.status` | A limit setting holds something that is not a number. |
| Cursor: `the cursor-sdk package is not in this image` | The image was built with `WITH_CURSOR=false`. |
| Cursor: `ConfigurationError` / `AuthenticationError` in the log | Wrong or missing `CURSOR_API_KEY` / `LLM_API_KEY`; a repository-scoped Cursor key (use a user or service-account key); or the model name isn't available on your plan (try another `LLM_MODEL`). |
| Cursor: `Cursor run took longer than …` | Raise `LLM_TIMEOUT_SECONDS`. |
| Cursor fails right after start with a file-system error | The helper writes only under `/tmp` (`HOME=/tmp`). Check the pod still mounts the `tmp` volume and that it isn't full (`sizeLimit` in `deployment.yaml`). |
| Azure OpenAI: HTTP 404 | `LLM_MODEL` must be the **deployment name**, and `LLM_BASE_URL` must end in `/openai/v1`. |
| Outbound traffic is restricted | Allow HTTPS to the provider's host: `api.groq.com`, `api.anthropic.com`, `generativelanguage.googleapis.com`, `api.openai.com`, `<resource>.openai.azure.com`, `openrouter.ai`, `api.mistral.ai`. For Cursor, allow the hosts in Cursor's network documentation. |
