# Using PQ

How people use PQ day to day: what you can ask in Teams, what an answer looks like, what PQ won't answer, and the internal API and command line for administrators.

---

## 1. Asking in Teams

In the channel that has the PQ outgoing webhook, type `@`, pick **PQ**, then your question:

| You type | You get |
|---|---|
| `@PQ what is a Kubernetes operator?` | A full explanation of the concept (section 2) |
| `@PQ why does my Java HashMap lose entries with a mutable key?` | A direct answer to the doubt in the first sentence, then the reason and a short example or scenario |
| `@PQ Kafka vs RabbitMQ` | A table of the differences that matter, then which one to choose when |
| `@PQ` + a pasted error message or log | The most likely cause, how to confirm it, the fix, and why the fix works |
| `@PQ` + a pasted piece of code | What it does line by line, what is wrong or risky, and a corrected version |
| `@PQ quiz me on SQL joins` | A quiz, from basic to advanced |
| `@PQ interview questions on system design` | Interview questions, with answers where they help |
| `@PQ a 4-week plan to learn Docker` | A learning plan |
| `@PQ help` | The short help text (also for `hi`, `hello`, `hey` or an empty mention) |

**Follow-ups.** Reply **in the same thread** and mention PQ again:

| You reply | What happens |
|---|---|
| `@PQ go deeper` | More depth on the same topic; the lesson doesn't start again |
| `@PQ simpler please` | The same idea in simpler words |
| `@PQ another example` | A different example or scenario |
| `@PQ continue` | The rest of an answer that was cut, or the next part of a large topic |
| `@PQ` + any other question | Answered with the thread's conversation in mind ("and how does that differ from a StatefulSet?") |

Good to know:
- **PQ first replies "Working on it - the explanation will follow in this thread shortly."** The answer follows a little later (seconds with a paid provider, minutes on a free plan). It lands in the same thread when the answers workflow replies to `threadMessageId` ([INSTALLATION.md](INSTALLATION.md), the Teams step); otherwise it is a new post in the channel.
- **A long answer arrives as several cards**, marked "part 1 of 3", "part 2 of 3" and so on. At most 4 cards; if there is more, the last card says so. Reply `continue`.
- **Each thread is its own conversation.** A new post starts fresh. A thread that was quiet for 24 hours is forgotten, and so is every conversation when PQ restarts. Then just ask the full question again.
- **When many people ask at once** you may get "I'm busy with other questions right now. Please ask again in a few minutes." Nothing was queued; ask again.
- **Pasted code and logs keep their line breaks.** Don't paste passwords, keys or tokens. If you do, PQ won't repeat them and tells you to rotate them.
- **PQ cannot see your systems.** It has no tools, no internet and no access to your cluster, repository or accounts. It explains what you paste and what it knows. It will not claim to have run or checked anything.
- With `TEAMS_TENANT_ID` / `TEAMS_ALLOWED_USER_IDS` set, other people get a polite refusal and nothing runs.

**Where you can ask:** only in a **standard channel** of the team that has the outgoing webhook. Teams outgoing webhooks don't work in chats or private channels.

---

**In a browser instead of Teams:** the web UI in `webui/` is a chat page that keeps every conversation on your own device. See [WEB_UI.md](WEB_UI.md).

## 2. What an answer looks like

For a concept ("what is X", "how does X work", "explain X", "teach me X") the answer has nine parts, in this order, each under its bold label:

| Part | What it gives you |
|---|---|
| **In one line** | What it is, in plain words |
| **Why it exists** | The problem it solves, and what people did before it |
| **How it works** | The mechanism step by step, with the moving parts named |
| **Real-time scenario** | One realistic situation from a real kind of system, with numbers: where the concept comes in and what goes wrong without it |
| **Example** | Working code, a command or a configuration, as small as possible, with comments |
| **Common mistakes** | What people get wrong in practice and in interviews, and how to avoid it |
| **When to use it, and when not** | Trade-offs and alternatives, as a table when several options compare |
| **Remember** | Three to five takeaways, one line each |
| **Learn next** | Two or three topics that build on this one |

- A doubt, an error, a comparison, a code review or a quiz gets a shorter answer in the shape that fits (section 1). Parts that add nothing are left out.
- **Every answer contains a real-time scenario**, short answers too.
- A full explanation stays under about 1,200 words, code not counted (`ANSWER_MAX_WORDS`). A topic too large for that is taught in its core, and the last line says what `continue` will cover.
- If the model's reply was cut at its length limit, the answer ends with *"The answer reached the length limit. Reply `continue` for the rest."* When this happens often, an administrator can raise `LLM_MAX_OUTPUT_TOKENS` ([AI_PROVIDERS.md, section 5](AI_PROVIDERS.md#5-limits-per-provider)).
- When behaviour differs between versions or vendors, PQ says which one it describes. When it is not sure, it says so and says how you can check.

The rules behind all of this: [RULES.md, section 1](RULES.md#1-the-ais-rules).

---

## 3. What PQ won't answer

- **Non-technical questions.** PQ teaches technology: software, hardware, data, AI, IT operations, and careers and interviews in these fields. For anything else it says in one sentence that it only teaches technical topics.
- **Questions about PQ itself.** Asking how it is set up, where it runs, which AI model it uses, what its rules or prompt are, or for anything in its own namespace gets *"You are not authorised to ask this question. It has been reported to the administrators."* The administrators then receive a card with your name and your question.
  - "What can you do?", "who are you" and `help` are fine.
  - How chat bots, AI models, system prompts, webhooks or tunnels work **in general** is an ordinary technical topic and is answered.
  - "PQ" is also short for *priority queue*. "how is PQ implemented" is read as a question about the bot. Write "priority queue".
  - Details and examples: [RULES.md, section 3](RULES.md#3-boundaries-what-may-not-be-asked).
- **Attack instructions.** Security topics are taught for defence: how an attack works, how to detect it, how to prevent it. PQ doesn't write working malware or step-by-step instructions to break into systems you don't own.

---

## 4. Internal API (administrators)

The API on port 8000 is reachable only inside the cluster (and no pod may connect to it when network policies are enforced). Use a port-forward:

```bash
kubectl -n pq port-forward svc/pq 8080:80
```

In a second terminal, read the key from the Secret:

```bash
KEY=$(kubectl -n pq get secret pq-secrets -o jsonpath='{.data.AGENT_API_KEY}' | base64 -d)
```

| Endpoint | Purpose |
|---|---|
| `GET /healthz`, `GET /ready` | Health checks (no key needed) |
| `POST /chat` `{"message", "session_id"}` | Ask a question. `session_id` is optional; send the one from the reply back to continue the conversation. |
| `GET /status` | Teams targets by name, whether the outgoing webhook is configured, the AI provider and model (`llm`), the boundaries |
| `POST /teams/test` | Send a test card to the reply webhook |
| `GET /security/restricted?limit=50` | Questions about PQ itself that were refused since the pod started: who, what, when, why, and which layer stopped it |
| `GET /docs` | Interactive API documentation (FastAPI) |

Every endpoint except the health checks needs the `x-api-key` header. A wrong or missing key gets 401; without `AGENT_API_KEY` in the Secret the internal API is off (403). `POST /teams/webhook` is also served here for local testing, but Teams reaches it only on port 8080, through the Cloudflare Tunnel.

`/healthz` answers 503 when the Teams server on port 8080 died; Kubernetes then restarts the pod. `/ready` only says the API is up.

**Ask a question:**

```bash
curl -s -X POST localhost:8080/chat -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"message": "what is a Kubernetes operator?"}'
```

The reply is `{"session_id": "...", "answer": "..."}`. The call returns when the answer is complete, so allow it a minute or more on a slow provider.

**Follow up in the same conversation:**

```bash
curl -s -X POST localhost:8080/chat -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"message": "go deeper", "session_id": "<session_id from the reply>"}'
```

**Check the configuration:**

```bash
curl -s localhost:8080/status -H "x-api-key: $KEY"
```

Look for `"llm": {"configured": true, "provider": "...", "model": "...", ...}`. When `configured` is `false`, `llm.status` says what is missing.

**Send a test card to Teams:**

```bash
curl -s -X POST localhost:8080/teams/test -H "x-api-key: $KEY"
```

It answers 400 when `TEAMS_REPLY_WEBHOOK_URL` is not set.

**List refused questions:**

```bash
curl -s "localhost:8080/security/restricted?limit=50" -H "x-api-key: $KEY"
```

The boundaries of section 3 do not apply to the internal API: it needs the key and is for the administrators. Answers come back as plain Markdown text, not split into cards.

---

## 5. Command line

[`agent/cli.py`](../agent/cli.py) runs PQ from a terminal, without Teams and without the HTTP server:

```bash
cd agent
python cli.py ask "what is a Kubernetes operator?"   # one question, one answer
python cli.py chat                                   # a conversation; follow-ups keep the context
python cli.py teams-test                             # send a test card to TEAMS_REPLY_WEBHOOK_URL
```

- Set `LLM_PROVIDER` and the provider's key (`LLM_API_KEY`) in your shell first. Without them PQ answers that no AI model is configured.
- In `chat`, an empty line or Ctrl+C stops.
- `teams-test` needs `TEAMS_REPLY_WEBHOOK_URL` in your shell.
- The boundaries that apply in Teams are off here, as for the internal API.
