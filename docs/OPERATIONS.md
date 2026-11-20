# Operations

Running PQ after it's installed: routine tasks, troubleshooting, limitations and cost. Installation problems are covered in the troubleshooting table at the end of [INSTALLATION.md](INSTALLATION.md#troubleshooting).

In the commands below, `<you>` is your Docker Hub user. `$KEY` and `localhost:8080` come from a port-forward to the internal API, in a second terminal:

```bash
kubectl -n pq port-forward svc/pq 8080:80
```

```bash
KEY=$(kubectl -n pq get secret pq-secrets -o jsonpath='{.data.AGENT_API_KEY}' | base64 -d)
```

Local port 8080 goes to the Service's port 80, which is the internal API (port 8000 in the pod). The endpoints are described in [USAGE.md](USAGE.md).

---

## Day to day

| Task | How |
|---|---|
| **Pick up security updates** | Rebuild the image even without code changes (GitHub: **Actions → CI → Run workflow** on `main`), then `kubectl -n pq rollout restart deploy/pq`. Do this regularly, e.g. monthly, and after any scan finding. |
| **Roll out a new image** (after CI pushed a new `latest`) | `kubectl -n pq rollout restart deploy/pq` |
| **Pin or roll back a version** (recommended in production) | `kubectl -n pq set image deployment/pq agent=docker.io/<you>/pq:sha-xxxxxxx`, and put the same tag in `deployment.yaml` so the next `apply` doesn't undo it. With `:latest`, any restart runs whatever was last pushed. The CI run's summary shows the exact command for the image it pushed. |
| **Change a setting** | Edit the ConfigMap in `deploy/deployment.yaml`, then `kubectl apply -f deploy/deployment.yaml && kubectl -n pq rollout restart deploy/pq`. All settings: [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md). |
| **Change a secret** (AI key, Workflows URL, Teams token) | `kubectl -n pq patch secret pq-secrets --type merge -p '{"stringData":{"LLM_API_KEY":"<new>"}}'` (any key name), then `kubectl -n pq rollout restart deploy/pq` |
| **Switch AI provider** | Put the new key in `LLM_API_KEY` (above), set `LLM_PROVIDER` (and `LLM_MODEL` / `LLM_BASE_URL` if needed) in the ConfigMap, apply, restart. Check `llm` in `/status`. See [AI_PROVIDERS.md](AI_PROVIDERS.md). |
| **Make explanations longer or shorter** | `ANSWER_MAX_WORDS` in the ConfigMap (default `1200`; `0` = no limit), apply, restart. If longer answers get cut off, raise `LLM_MAX_OUTPUT_TOKENS` too. |
| **Let another person ask** | Add them to the team that holds the outgoing webhook. If `TEAMS_ALLOWED_USER_IDS` is set, add their Entra object ID to it, apply, restart. |
| **Update cloudflared** | Change the image tag in `deploy/cloudflare-tunnel.yaml` to a newer release (Docker Hub `cloudflare/cloudflared` tags), then `kubectl apply -f deploy/cloudflare-tunnel.yaml`. The two pods are replaced one at a time, so questions keep working. |
| **Rotate the tunnel token** (e.g. it leaked) | Delete the tunnel in Cloudflare and redo INSTALLATION.md Steps 5.1, 5.2 and 5.4 with a new one; recreate Secret `pq-tunnel` and `kubectl -n pq rollout restart deploy/pq-tunnel` |
| **Rotate the Teams outgoing-webhook token** | Delete and recreate the outgoing webhook in Teams (INSTALLATION.md, Step 2.2), then update `TEAMS_OUTGOING_WEBHOOK_SECRET` and restart |
| **Rotate a Workflows URL** | Delete the workflow in the Workflows app, create it again (INSTALLATION.md, Step 2.1 or 2.3), then update `TEAMS_REPLY_WEBHOOK_URL` or `TEAMS_SECURITY_WEBHOOK_URLS` and restart |
| **Rotate the internal API key** | `kubectl -n pq patch secret pq-secrets --type merge -p "{\"stringData\":{\"AGENT_API_KEY\":\"$(openssl rand -hex 16)\"}}"`, then restart |
| **See who asked about PQ itself** | The card in the administrators' channel, `curl -s localhost:8080/security/restricted -H "x-api-key: $KEY"` (since the pod started, at most 200), or `kubectl -n pq logs deploy/pq \| grep "Refused a restricted"` |
| **Send those notices to a private channel** | Create a Workflows webhook in a private channel for administrators (INSTALLATION.md, Step 2.3), then `kubectl -n pq patch secret pq-secrets --type merge -p '{"stringData":{"TEAMS_SECURITY_WEBHOOK_URLS":"security=<URL>"}}'` and restart |
| **Stop answering for a while** | `kubectl -n pq scale deploy/pq --replicas=0`; back with `--replicas=1`. While it is down, Teams shows an error after `@PQ`. Never run more than one replica. |
| **Ask without Teams** | `POST /chat` through the port-forward, or `python cli.py ask "…"` / `python cli.py chat` in `agent/` with `LLM_PROVIDER` and the key in the environment ([USAGE.md](USAGE.md)). The boundaries don't apply there. |
| **See what it is doing** | `/status` (port-forward) or `kubectl -n pq logs deploy/pq -f` |
| **Uninstall** | `kubectl delete -f deploy/cloudflare-tunnel.yaml -f deploy/deployment.yaml` (also deletes the namespace and the Secrets). Then delete the tunnel in Cloudflare (its DNS record goes with it) and the two firewall rules, and the outgoing webhook and workflows in Teams. Nothing cluster-wide is left behind: PQ created no roles, bindings or webhook configurations. |

A restart (any rollout) empties PQ's memory. Conversations start again: a follow-up such as `continue` in an old thread is then a new question without context. The list behind `/security/restricted` is cleared; the log of the old pod is gone with it, so keep the cards or ship the logs if you need a record.

### Tests and CI

```bash
cd agent && pip install -r requirements-dev.txt && pytest tests -q
```

`requirements-dev.txt` adds `pytest`, `flake8` and `pyyaml` to the runtime packages; `pyyaml` is needed by `tests/test_manifests.py`, which checks `deploy/*.yaml`. The tests need no AI key, no cluster and no network.

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs `flake8` and the same tests on every pull request and push that changes `agent/`, `deploy/` or the workflow. On `main`, when they pass, it builds the image for `linux/amd64` and `linux/arm64` and pushes it to Docker Hub as `docker.io/<you>/pq:sha-<7 characters>` and `:latest`.

---

## When a vulnerability scan reports something

The image is built to scan clean ([SECURITY.md, section 2](SECURITY.md#2-the-image)). When a scanner such as Trivy reports a new finding, look at the **package** column:

| Package in the report | Where it comes from | Fix |
|---|---|---|
| A Debian package with **no fixed version** listed | The Python base image | Nothing to do yet: Debian hasn't released a fix. It disappears with a rebuild once it has. |
| A Debian package (`zlib1g`, `libssl3`, `libc6`, …) with a fixed version | The Python base image | Rebuild the image: the build runs `apt-get upgrade`. In GitHub: **Actions → CI → Run workflow**. Then restart PQ. |
| A Python package (`fastapi`, `requests`, `anthropic`, …) | [`agent/requirements.txt`](../agent/requirements.txt) | Rebuilding installs the newest version inside the allowed range. If the fix is outside the range, raise the upper bound, run the tests, rebuild. |
| `pip` | Shouldn't appear: the image has no pip | Check the pod runs a current image. |
| `undici` | The Cursor SDK's bundled helper | Set `UNDICI_VERSION` and `UNDICI_INTEGRITY` in [`agent/docker/patch_cursor_bridge.py`](../agent/docker/patch_cursor_bridge.py) to the fixed release (the integrity value is `dist.integrity` at `https://registry.npmjs.org/undici/<version>`), then rebuild. Stay on the 6.x line unless the fix exists only in a later one. |
| Another Node package under `cursor_sdk/_vendor` | The Cursor SDK | Check for a newer `cursor-sdk` release ([`requirements-cursor.txt`](../agent/requirements-cursor.txt)). If you don't use Cursor, build without it: `--build-arg WITH_CURSOR=false`. |
| `cloudflared` image findings | [`deploy/cloudflare-tunnel.yaml`](../deploy/cloudflare-tunnel.yaml) | Move the pinned tag to a newer release (see *Update cloudflared* above). |

After a rebuild, restart the pod and let the scanner rescan; with the Trivy Operator the new report appears by itself a few minutes after the new pod starts.

---

## Health at a glance

```bash
kubectl -n pq get pods
```

One `pq-…` pod and, with the tunnel, two `pq-tunnel-…` pods, all `Running` and Ready.

```bash
curl -s localhost:8080/status -H "x-api-key: $KEY"
```

| In `/status` | Healthy when |
|---|---|
| `llm.configured` | `true`, with the `provider` and `model` you chose. Otherwise `llm.status` says why. |
| `teams.reply_webhook` | The name of the reply target (`reply-1` unless you named it). `null` means `TEAMS_REPLY_WEBHOOK_URL` is missing: no answers reach Teams. |
| `teams.outgoing_webhook` | `true`. `false` means `TEAMS_OUTGOING_WEBHOOK_SECRET` is missing: `/teams/webhook` answers 404. |
| `teams.security_targets` | The names of the administrators' targets. Empty means refusals are only logged and kept. |
| `boundaries.enabled` | `true` |
| `boundaries.protected_namespaces` | Contains the namespace PQ runs in |

`/status` shows what is configured, not whether the other side works. To test the AI provider, ask something through `/chat`. To test the path from Teams, `curl -i -X POST https://<host>/teams/webhook` should give **401**, and `@PQ help` in the channel should be answered within seconds.

`POST /teams/test` posts a test card to the reply webhook and returns the HTTP status per target. `"ok": true` means the workflow accepted the request. The test card carries no thread; if the workflow replies in a thread (INSTALLATION.md, Step 2.1) the card may not appear, and the workflow's run history shows why.

**Probes.** `/ready` answers 200 as soon as the API is up. `/healthz` answers 503 only when the Teams server (port 8080) died; the liveness probe then restarts the pod, and the log says why (`The teams server stopped: …`). Neither probe calls the AI provider or Teams, so a pod can be Ready while the provider key is wrong: `/status` and the log show that.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Teams says "Sorry, there was a problem…" after `@PQ` | Wrong security token (log: `Rejected /teams/webhook call…`), Cloudflare blocked the call (**Security → Events** in Cloudflare), the tunnel is down, or the PQ pod isn't running. `curl -i -X POST https://<host>/teams/webhook` should give **401**. |
| "Working on it" but no answer card | `TEAMS_REPLY_WEBHOOK_URL` is wrong or that workflow is off (log: `Teams target '…' returned …`). If the log shows nothing, open the workflow's run history: the reply step failed (wrong Team/Channel, or the Message ID expression mistyped). |
| Answers stopped arriving after a while | Workflows run under the account of the person who created them. If that account was disabled or its connection expired, recreate the workflow and update the URL. |
| The answer card says "Sorry, that failed (…)" | The AI provider failed: wrong key, model not available, rate or daily limit, timeout (`LLM_TIMEOUT_SECONDS`), or outbound HTTPS to the provider blocked. The person sees only the error type; the full error follows `Teams request failed` in the log. See [AI_PROVIDERS.md](AI_PROVIDERS.md). |
| "No AI model is configured" | `/status` → `llm.status` names the missing setting. See [AI_PROVIDERS.md](AI_PROVIDERS.md). |
| "I can't send my answer: no reply webhook is set up" | `TEAMS_REPLY_WEBHOOK_URL` isn't in the Secret, or the pod wasn't restarted after adding it |
| Answers arrive as a new post, not in the question's thread | The answers workflow still uses "Post card in a chat or channel". Change it to "Reply with an adaptive card in a channel" with Message ID `triggerBody()?['threadMessageId']` (INSTALLATION.md, Step 2.1). |
| The answer ends with "The answer reached the length limit. Reply `continue` for the rest." | The model's reply limit was reached. `@PQ continue` in the thread gives the rest. If it happens often, raise `LLM_MAX_OUTPUT_TOKENS` or lower `ANSWER_MAX_WORDS`. |
| The last card says the answer "was longer than Teams allows and was cut here" | The answer needed more than four cards. `@PQ continue` in the thread gives the rest. |
| Answers are too long, or too short | Length and format are rule 10 of the AI's rules ([RULES.md, section 1](RULES.md#1-the-ais-rules)). Change `ANSWER_MAX_WORDS` in the ConfigMap (default 1200; `0` = no limit), apply, restart. |
| A follow-up ("go deeper", "continue") is answered as if it were a new question | The follow-up wasn't a reply in the same thread; the pod restarted since; the thread was idle for more than 24 hours; or the earlier turns no longer fit the request size (`LLM_MAX_REQUEST_CHARS`, small on Groq's free plan). |
| PQ doesn't react to a reply in the thread | Teams only sends messages that mention `@PQ`. Mention it in every follow-up. |
| Answers take minutes | Normal on free plans: PQ waits out the per-minute token limit (log: `Model API returned 429; retrying in …`). |
| "I'm busy with other questions right now" | Two answers are written at a time and six may run or wait. With Cursor one runs at a time. Ask again in a few minutes. |
| The same question is answered with "(already answered)" | Teams sent the message twice; PQ answers each message once. Nothing to fix. |
| PQ says it "only teaches technical topics" | The question had nothing to do with technology (rule 11). |
| Someone gets "You are not authorised to ask this question" for an ordinary question | The question filter read it as a question about PQ itself ("you", "this bot" or "PQ" next to words such as "deployed", "configured" or "model"), or the AI used its rule 12. The card and the log line say which layer and why. Ask again and name the technology instead of "you" (e.g. "how is a chat bot deployed on Kubernetes?"). Examples of what is refused and what passes: [RULES.md, section 3](RULES.md#3-boundaries-what-may-not-be-asked). |
| No card when a restricted question is asked | `/status` → `teams.security_targets` is empty (`TEAMS_SECURITY_WEBHOOK_URLS` not set), that workflow is off, or more than 20 refusals came within an hour (then they are only logged). `/security/restricted` lists them all. |
| Pod never created; events say `violates PodSecurity "restricted"` for `opentelemetry-auto-instrumentation-…` | The CloudWatch Observability add-on is injecting OpenTelemetry containers. The manifests opt out with `instrumentation.opentelemetry.io/inject-*: "false"`; details in [INSTALLATION.md](INSTALLATION.md#troubleshooting). |
| Tunnel pods not Ready / Cloudflare error 1033 or 502 | See the tunnel rows in [INSTALLATION.md](INSTALLATION.md#troubleshooting). |
| The pod restarts every few minutes; events say `Liveness probe failed ... 503` | The Teams server on port 8080 stopped. `kubectl -n pq logs deploy/pq --previous` shows `The teams server stopped: …` with the reason. |
| The pod is `OOMKilled` | The memory limit is 1Gi, sized for the Cursor provider's helper process. Raise `resources.limits.memory` in `deployment.yaml`. |
| `/status` and other internal endpoints answer 403 | `AGENT_API_KEY` isn't in the Secret: the internal API is off until it is. |
| Internal endpoints answer 401 | The `x-api-key` header is missing or doesn't match `AGENT_API_KEY`. |

Logs worth knowing:

| Log line | Meaning |
|---|---|
| `AI model: Groq, model openai/gpt-oss-120b` | The provider loaded at startup |
| `… PQ cannot answer questions until an AI provider is configured.` | No AI model; the text in front says why |
| `Boundaries on: … reported to …; protected namespaces: …` | At startup: where notices about refused questions go and which namespace names are protected |
| `TEAMS_REPLY_WEBHOOK_URL is not set: answers can't be posted to Teams` | At startup: the reply webhook is missing |
| `AGENT_API_KEY is not set: the internal API (…) is off` | At startup: the internal API answers 403 |
| `Teams webhook listening on :8080 (POST /teams/webhook only)` | The Teams server started |
| `Teams question from <name> (<n> characters)` | A question arrived. The text of an ordinary question is not logged. |
| `Answering with <provider>, model <model>` | A model call starts |
| `Model API returned 429; retrying in Ns` | Waiting for a rate limit |
| `Model API said the request is too large; retrying with less history` | The provider refused the request size; PQ sends it again with less of the conversation |
| `Teams request failed` (with a traceback) | An answer failed; the person got "Sorry, that failed (…)" |
| `Teams target '<name>' returned <status>` / `failed` | A Workflows webhook refused a card or couldn't be reached (3 attempts) |
| `Rejected /teams/webhook call with a missing or wrong HMAC signature` | A call that wasn't from your Teams webhook |
| `Rejected Teams message from tenant <id>` | A message from another Microsoft 365 tenant |
| `Refused a restricted request from <name> (<user id>), stopped by the <layer>: <why> \| <question>` | Someone asked about PQ itself and was refused. The question is shown with credentials redacted. |
| `The teams server stopped: …` | Port 8080 died; `/healthz` fails and the pod is restarted |

---

## Limitations

| # | Item | Notes |
|---|---|---|
| 1 | **Questions only from a standard channel** | Outgoing webhooks don't work in chats or private channels, and only react to an `@PQ` mention. A 1:1 chat with PQ would need a registered Teams bot (Azure Bot Service). |
| 2 | **PQ knows only what the model knows** | It has no tools, no internet and no access to anyone's systems. It can't look up today's documentation, run code or read a repository. Rule 5 tells the model to say when it is unsure, but a model can still be wrong: check commands and version details before relying on them. |
| 3 | **State is in memory** | Conversations and the list of refused questions are lost when the pod restarts. Keep `replicas: 1`: a second pod would not know the first one's conversations. |
| 4 | **Teams limits** | Replies to an outgoing webhook must come within 5 s, so explanations come through a workflow. They land in the question's thread only after the one-time workflow change (INSTALLATION.md, Step 2.1). A card holds about 8,000 characters and an answer at most four cards. Workflows throttle above 4 requests per second; PQ retries 3 times. |
| 5 | **Text only** | PQ reads the text of the message. It doesn't see images, screenshots or attached files. Code and logs must be pasted as text. |
| 6 | **Conversation memory is short on small plans** | Only what fits `LLM_MAX_REQUEST_CHARS` is sent, and at most the last 20 messages. On Groq's free plan that is one or two earlier answers. |
| 7 | **Free AI plans** | Per-minute and per-day token limits make answers slow (minutes) and stop them for the day once used up; people then get "Sorry, that failed (ProviderError)". |
| 8 | **Open models follow the rules less strictly** | The structure of an explanation and the boundary rule are instructions to the model. Smaller models skip parts more often. |
| 9 | **Six questions at a time** | Two are answered at once and four more wait; the next person is asked to try again. |
| 10 | **Cloudflare is in the path of questions** | Questions depend on Cloudflare and the tunnel pods. Answers don't: they go straight from the pod to the Workflows webhook. |
| 11 | **Cursor depends on a bundled helper program** | The Cursor SDK runs its own Node.js helper inside the pod. It starts fine under the pod's restrictions (read-only filesystem, non-root), but its packages are Cursor's to update; the image build patches the one with known vulnerabilities ([SECURITY.md](SECURITY.md#2-the-image)). |
| 12 | **Boundaries recognise wording** | The question filter knows English phrasing and can refuse an ordinary question by mistake or miss an unusual one; the AI's rule covers other wording and languages but is a model's judgement. Everyone in Teams is restricted, administrators included; refusals are kept in memory only (and in the pod's log). |
| 13 | **No usage statistics** | PQ keeps no record of who asked what, apart from the log line with the name and the length of each question. |

---

## Cost

| Item | Cost |
|---|---|
| **AI provider** | Free tiers (Groq, Gemini, some OpenRouter models) cost nothing but are rate-limited. Claude, OpenAI, Azure OpenAI, Mistral and Cursor bill per use. One question is one model call (Cursor: one run). Its size is the rules (about 7,000 characters), the conversation so far (up to `LLM_MAX_REQUEST_CHARS`) and the reply (up to `LLM_MAX_OUTPUT_TOKENS`). Nothing caps the number of questions per day; limit who can ask with `TEAMS_ALLOWED_USER_IDS`, and set a spending limit at the provider. |
| **Cloudflare** | Tunnel, the WAF custom rule, one rate-limiting rule and the Universal certificate are included in the free plan. |
| **Kubernetes** | Nothing extra: PQ requests 50m CPU / 128Mi (limit 1Gi); each tunnel pod 10m / 64Mi. |
| **Docker Hub** | Free for public repositories. |
| **Teams** | Workflows webhooks and outgoing webhooks need no premium licence. |
