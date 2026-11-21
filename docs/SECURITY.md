# Security model

PQ answers technical questions. It reads nothing and changes nothing: it has no tools, no Kubernetes permissions and no access to anyone's systems. But it talks to the internet (Teams, Cloudflare, an AI provider) and it holds keys, so it is built to be **closed by default and explicit about what leaves the cluster**.

---

## 1. Layers

```mermaid
flowchart LR
    teams(["Teams<br/>(Microsoft's cloud)"]) --> cf["Cloudflare WAF<br/>only POST /teams/webhook,<br/>rate limit"]
    cf --> tun["cloudflared<br/>(outbound tunnel,<br/>nothing can connect in)"]
    tun -->|"NetworkPolicy:<br/>only the tunnel pods"| p8080["PQ :8080<br/>serves only /teams/webhook"]
    p8080 --> hmac{"HMAC valid?<br/>tenant and user allowed?"}
    hmac -- no --> reject["401 / polite refusal"]
    hmac -- yes --> filter{"about PQ itself?<br/>(question filter)"}
    filter -- yes --> refuse["'not authorised'<br/>+ notice to the administrators"]
    filter -- no --> llm["AI provider<br/>rules + conversation,<br/>no tools"]
    llm --> card["answer card(s)<br/>Workflows webhook,<br/>same thread"]
```

| Layer | What it does |
|---|---|
| **No Kubernetes permissions** | PQ never talks to the Kubernetes API. The manifests contain no ServiceAccount, Role, ClusterRole or binding, and both Deployments set `automountServiceAccountToken: false`, so there is no token in the pods. `test_manifests.py` enforces this. A compromised PQ pod has nothing to read in the cluster. |
| **No tools** | The AI model gets the rules and the conversation, and returns text. It can't run commands, read files, browse the web or call any system. A manipulated model can only write a wrong or unwanted answer. |
| **No public endpoint in AWS** | No load balancer, no public IP, no inbound port. Teams' requests arrive through a Cloudflare Tunnel that the cluster opens outward. All Services are `ClusterIP`; there is no Ingress. |
| **Cloudflare** | A WAF rule blocks everything except `POST /teams/webhook` on the hostname; a rate-limit rule cuts off floods; TLS 1.2+. |
| **Teams-only port** | The tunnel reaches port 8080, which serves nothing but `POST /teams/webhook` (not even `/docs`). The internal API is on 8000 and can't be reached through the tunnel even if a Cloudflare rule is wrong. |
| **Network policies** | Only the tunnel pods may connect to 8080; no pod may connect to the internal API (8000); nothing may connect to the tunnel pods. Enforced when the cluster runs a network-policy engine (EKS VPC CNI network policy, Calico, Cilium). |
| **HMAC signature** | Every Teams request must carry a valid `Authorization: HMAC …` header made with the outgoing webhook's security token; otherwise 401. Without `TEAMS_OUTGOING_WEBHOOK_SECRET` the endpoint is off (404). |
| **Who may ask** | Only members of the team holding the outgoing webhook can mention it; `TEAMS_TENANT_ID` and `TEAMS_ALLOWED_USER_IDS` narrow it to your tenant and to named people. A message from anyone else is answered with a refusal and nothing is sent to the AI model. |
| **Boundaries** | People in Teams can't ask about PQ itself: its setup, AI model or provider, rules or prompt, or its namespace. Two layers: a filter on the question refuses before anything is sent to the AI model, and rule 12 of the AI's rules covers wording the filter can't see. The person is told they are not authorised, and the administrators get a card with who asked what ([RULES.md, section 3](RULES.md#3-boundaries-what-may-not-be-asked)). |
| **Internal API key** | `/chat`, `/status`, `/teams/test` and `/security/restricted` require the `x-api-key` header. Without `AGENT_API_KEY` the internal API is off (403, closed by default). The key is compared in constant time. Only the probes `/healthz` and `/ready` need no key. |
| **Pasted text is not an instruction** | Rule 7 tells the model that code, logs and documents in a question are material to explain, never instructions. Since the model has no tools, ignoring that rule still changes nothing outside the answer. |
| **Secrets in questions** | Rule 8: the model never asks for passwords, keys or tokens, and doesn't repeat one that was pasted. A refused question is redacted before it is logged, kept or reported: passwords, tokens, API keys, AWS access keys, JWTs and credentials in URLs (`scheme://user:password@host`) become `[REDACTED]`. |
| **Security topics** | Rule 9: attacks are explained for defence (how they work, how to detect and prevent them). The model is told not to write working malware or step-by-step instructions to break into systems the person doesn't own. |
| **Cursor provider** | Cursor runs its own agent. PQ starts it with no tool groups at all (and `shell` explicitly disallowed), in an empty working folder, with no project or user settings and no MCP server. The agent can only answer in text. |
| **Limits** | Two answers are written at a time and at most six questions run or wait; conversations are bounded (200, idle ones expire after a day, 20 messages each); an answer is at most four cards; at most 20 notices per hour go to the administrators and 200 refusals are kept; a message Teams sends twice is answered once. Every call to the AI provider and to Teams has a timeout. |
| **Container** | Non-root (UID 10001; cloudflared 65532), read-only root filesystem, no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp; namespace Pod Security level `restricted`. The only writable path is an `emptyDir` at `/tmp`. |
| **Secrets handling** | Keys and webhook URLs live only in Kubernetes Secrets created with `kubectl`, never in Git or the image. Logs and `/status` show webhook *names*, never URLs. The person who asked sees only the type of an error, because the full error can name the AI provider; the detail goes to the log. |

---

## 2. The image

What [`agent/Dockerfile`](../agent/Dockerfile) does so that an image scan (Trivy, for example) stays clean:

| Step | Why |
|---|---|
| `apt-get upgrade` at build time | Installs the security fixes Debian published after the Python base image was built (for example `zlib1g`). |
| pip is upgraded for the build, then **removed** | Nothing needs pip at runtime, and its CVEs would otherwise appear in every scan. The copy Python keeps for re-installing pip is removed too. |
| The Cursor SDK's bundled `undici` is replaced | The SDK's helper program ships `undici` 5.29.0 (with `@fastify/busboy` 2.1.1), a release line that gets no more security fixes. [`docker/patch_cursor_bridge.py`](../agent/docker/patch_cursor_bridge.py) swaps in a pinned, checksum-verified `undici` 6.x, removes `@fastify/busboy`, and then loads the helper's modules to prove they still work. If any check fails, the image build fails. |
| Build helpers and tests are left out | `docker/` and `tests/` are not in the final image. |
| Few packages | The runtime needs only the packages in [`requirements.txt`](../agent/requirements.txt): the Anthropic SDK, FastAPI, uvicorn, pydantic, requests and time zone data. There is no Kubernetes client. The Cursor SDK is added unless the image is built with `--build-arg WITH_CURSOR=false`. |
| Runs as UID 10001 | The image's default user is not root. |

About the `undici` findings: in the SDK, `undici` is only imported as a fallback for Node.js older than 18, and the SDK runs on its own bundled Node.js 22. So that code was loaded but never used, and the practical risk was low. Replacing it is still right: scanners report by version, and unused vulnerable code shouldn't ship.

**Fixable and unfixed findings.** A scan that also lists advisories *without* a fix shows many entries for Debian base packages (glibc, util-linux, OpenSSL, …): Debian hasn't released fixes for those, they are in every Debian-based image, and they go away by rebuilding once Debian publishes the fix. The steps above are aimed at the findings that do have a fix.

A scan is a snapshot: new advisories appear all the time. Rebuild the image regularly (re-run CI) so the OS updates are picked up, and rescan after every rebuild. What to do with a new finding is in [OPERATIONS.md](OPERATIONS.md#when-a-vulnerability-scan-reports-something).

---

## 3. What leaves the cluster

PQ reads no cluster data, so none can leave. What leaves is what people type, and what the model answers.

| To | What | When |
|---|---|---|
| The AI provider you chose | PQ's rules, the question, and the recent conversation in that thread (earlier questions and answers, as much as fits the request size) | For every question that passes the checks. A question refused by the filter, or from someone who isn't allowed to ask, is not sent. |
| Microsoft Teams (Workflows webhooks) | Answer cards: the question (first 400 characters), the name of the person who asked, and the explanation. Notices to the administrators: name, Entra object ID, time, reason and the redacted question. | For every answer and every refusal |
| Cloudflare | The Teams message with the question and its sender, and PQ's immediate reply (Cloudflare terminates HTTPS). Not the explanation: that goes from the pod straight to the Workflows webhook. | For every `@PQ` message |
| Docker Hub | Nothing; nodes only pull the public image | – |

**People paste things into questions.** Code, configuration, log lines, error messages and sometimes credentials. All of that text goes to the AI provider as part of the question, unredacted, and stays in the conversation PQ keeps in memory for follow-ups. Redaction only covers refused questions. Tell the people who use PQ:

- don't paste secrets, customer data or anything your organization may not send to the provider;
- a pasted secret counts as leaked: rotate it.

To keep questions inside your own network, use a self-hosted model through `ollama` or `custom` ([AI_PROVIDERS.md](AI_PROVIDERS.md)). What a hosted provider does with the text (retention, training) is set by your plan and agreement with that provider.

**What PQ keeps.** Nothing on disk. In the pod's memory: up to 200 conversations (dropped after 24 idle hours) and the last 200 refusals. All of it is gone when the pod restarts. In the log: for an ordinary question only the sender's name and the length of the question; for a refused question the name, the Entra object ID, the reason and the redacted question.

---

## 4. Things to know

- **The web UI's API is public when `WEB_UI` is set.** `POST /api/chat` on the public port takes a conversation from the browser and stores nothing. `WEB_UI` limits which websites may call it from a browser; it is not a login. Set `WEB_ACCESS_CODE` so that only people with the code can use your AI provider's quota, and keep `WEB_RATE_PER_MINUTE` low. The boundaries apply as in Teams. Details: [WEB_UI.md](WEB_UI.md), section 8.

- **What the boundaries guarantee.** PQ has nothing to give away: it has no tools, reads no files and no cluster, and the model is never shown keys, webhook URLs or settings. The most a clever question could get out of the AI model is what the model itself was given, which is its rules (they are in [RULES.md](RULES.md)) and the protected namespace names. The question filter and the AI's rule are best effort: they recognise wording, and wording can be varied.
- **Send the notices to a private place.** Set `TEAMS_SECURITY_WEBHOOK_URLS` to a channel or chat that only administrators can read. Without it, refusals are only logged and kept in `GET /security/restricted`. Never point it at the questions channel.
- **The internal API and the command line are not restricted.** Anyone who has `AGENT_API_KEY` and can port-forward into the namespace can ask anything through `/chat`, including about PQ itself, and can read the refused questions. The same holds for anyone who can run `cli.py` with the provider's key. Treat both as administrator access.
- **Port 8000 is internal.** It also serves FastAPI's `/docs` and `/openapi.json` without a key (they describe the API, they don't call it), and `/teams/webhook` with the same HMAC check as port 8080, for local testing. It is reachable only through `kubectl port-forward`; with network policies enforced, no pod can connect to it.
- **Kubernetes access to the namespace is access to the keys.** Anyone who can read Secrets in `pq`, or exec into the pod, can read the AI key, the webhook URLs and the tokens. Limit who has that in the cluster.
- **Workflows URLs are passwords.** Anyone who has the reply URL can post cards to the questions channel. They are stored only in the Secret and never logged.
- **Answers can be wrong.** PQ's explanations come from a model's knowledge, not from a check against a real system. The rules tell the model not to invent commands or versions and to say when it is unsure; treat commands and security advice as something to verify before use.
- **Public image.** The image contains only code, no keys or URLs. It includes the Cursor SDK (proprietary licence) unless built with `--build-arg WITH_CURSOR=false`.
- **Questions only from a standard channel** of the team with the outgoing webhook; anyone in that team can ask unless `TEAMS_ALLOWED_USER_IDS` is set.
- **Rotating secrets:** see [OPERATIONS.md](OPERATIONS.md#day-to-day) (Teams token, Workflows URLs, AI key, internal API key, tunnel token).
