# Installation and configuration: PQ in EKS, connected to Teams

**How it's installed:**

- PQ is a **public Docker image** on your Docker Hub account.
- You install it with **one file**, [`deploy/deployment.yaml`](../deploy/deployment.yaml). That file holds the namespace, the settings, the Deployment, two Services and a NetworkPolicy.
- PQ needs **no Kubernetes permissions**. It never talks to the Kubernetes API: there is no ServiceAccount, no Role and no service account token in the pod. It only calls the AI provider and the Teams webhooks.
- [`deploy/cloudflare-tunnel.yaml`](../deploy/cloudflare-tunnel.yaml) lets people ask questions from a Teams channel through a Cloudflare Tunnel. The cluster opens that connection outward, so nothing in AWS is exposed to the internet. Without the tunnel only the internal API and the command line work.
- Answers go to a Teams channel that you own, as a reply in the thread of the question.

Follow the steps in order. Each one says **what to do**, **how to check it worked**, and **what to write down**. How PQ works is in [ARCHITECTURE.md](ARCHITECTURE.md); how people use it is in [USAGE.md](USAGE.md).

> **Where to run the commands:** everything below is **bash**. On Windows, use **Git Bash** (installed with Git for Windows). Replace every `<…>` placeholder with your value.

---

## Step 0 — Fill in this worksheet first

| #   | Value                                             | Example                                | Yours                                     |
| --- | ------------------------------------------------- | -------------------------------------- | ----------------------------------------- |
| W1  | Docker Hub user or organization                   | `jaychandra1`                          | jayachandra261020                         |
| W2  | EKS cluster name                                  | `prod-eks`                             | dev-env-cluster                           |
| W3  | AWS region of the cluster                         | `ap-south-1`                           | eu-west-1                                 |
| W4  | Public hostname for questions from Teams          | `pq.example.com`                       | [app.appgpc2.xyz](http://app.appgpc2.xyz) |
| W5  | Your domain in Cloudflare (it holds W4)           | `example.com`                          | appgpc2.xyz                               |
| W6  | Microsoft 365 tenant ID (Step 4.2)                | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |                                           |
| W7  | AI provider and its API key (required; Step 3.1)  | `groq` + `gsk_…`                       |                                           |

Collected along the way (keep them safe; all of them work like passwords):

| # | Value | From step |
|---|---|---|
| D1 | Docker Hub access token | 1.3 |
| T1 | Workflows URL: answers in the questions channel | 2.1 |
| T2 | Outgoing webhook security token | 2.2 |
| T3 | Workflows URL: the administrators' channel, for notices about refused questions (optional) | 2.3 |
| C1 | Cloudflare tunnel token | 5.1 |

---

## Step 1 — Tools, the image and the cluster

### 1.1 Tools on your PC

In **PowerShell**:

```powershell
winget install -e --id Git.Git
winget install -e --id Amazon.AWSCLI
winget install -e --id Kubernetes.kubectl
```

Close and reopen your terminal afterwards so the tools are on the `PATH`. Docker Desktop is only needed if you want to build the image yourself (1.6); GitHub Actions normally builds it.

Sign in to AWS (needed to reach the EKS cluster):

```bash
aws configure            # or: aws configure sso   /   aws sso login --profile <profile>
aws sts get-caller-identity
```

✅ **Check:** shows your AWS account.

### 1.2 Create the Docker Hub repository

On **hub.docker.com** (signed in as W1): **Repositories → Create repository**.

- **Name:** `pq`
- **Visibility:** **Public**

> The image contains only PQ's code: no keys and no URLs. Those live in the Kubernetes Secret you create in Step 3. But a public image means anyone can download that code.

### 1.3 Create an access token for GitHub Actions → D1

Docker Hub → your avatar → **Account settings → Personal access tokens → Generate new token**.

- **Description:** `github-actions-pq`
- **Access permissions:** **Read & Write**

Copy the token. 📝 **D1**

### 1.4 Tell GitHub about Docker Hub

On GitHub, open your repository → **Settings → Secrets and variables → Actions**:

| Tab | Name | Value |
|---|---|---|
| **Variables** | `DOCKERHUB_USERNAME` | W1 |
| **Secrets** | `DOCKERHUB_TOKEN` | D1 |

### 1.5 Push the code; CI builds and pushes the image

Push the repository to the `main` branch on GitHub. The workflow [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) then runs two jobs:

1. **Lint & tests**: `flake8` and `pytest`, including checks of `deploy/*.yaml`.
2. **Build & push image to Docker Hub**: only on `main`, and only when the first job passed.

The workflow starts by itself when a push changes something under `agent/`, `deploy/` or the workflow file. To build without such a change: GitHub → **Actions → CI → Run workflow** on `main`.

Open GitHub → **Actions** → the latest **CI** run. Both jobs should go green.

✅ **Check:** `https://hub.docker.com/r/<W1>/pq/tags` shows `latest` and `sha-xxxxxxx`, built for `linux/amd64` and `linux/arm64`.

### 1.6 (Alternative) build and push from your PC

Only if you don't want to use GitHub Actions. This needs Docker Desktop.

```bash
docker login -u <W1>
docker buildx build --platform linux/amd64,linux/arm64 -t docker.io/<W1>/pq:latest --push agent
```

The image includes the Cursor SDK (proprietary licence). To build without it, add `--build-arg WITH_CURSOR=false`; `LLM_PROVIDER: "cursor"` then doesn't work.

### 1.7 Connect to the EKS cluster

```bash
aws eks update-kubeconfig --region <W3> --name <W2>
kubectl get nodes
kubectl auth can-i create namespaces
```

✅ **Check:** your nodes are listed as `Ready`, and the last command prints `yes`.

> **If kubectl complains about an invalid kube-config**, move the file aside and try again:
> `mv ~/.kube/config ~/.kube/config.bak && aws eks update-kubeconfig --region <W3> --name <W2>`
>
> **If you get** `Unauthorized`, ask a cluster admin for an EKS access entry for your IAM identity.

You need the right to create a namespace and the objects in it. PQ itself gets no permissions in the cluster, so you don't need the right to create roles or bindings.

### 1.8 Nodes can pull from Docker Hub

Nodes need outbound internet access (directly, or through a NAT gateway) to reach `docker.io`. Quick test:

```bash
kubectl run pull-check --image=docker.io/<W1>/pq:latest --restart=Never --command -- python -c "print('pull ok')"
kubectl logs pull-check        # → pull ok
kubectl delete pod pull-check
```

✅ **Check:** prints `pull ok`. If the pod stays in `ImagePullBackOff`, see the troubleshooting table.

### 1.9 Network policies (recommended)

`deploy/deployment.yaml` and `deploy/cloudflare-tunnel.yaml` include **NetworkPolicies**:

- only the tunnel pods may connect to PQ's Teams port (8080);
- no pod may connect to PQ's internal API (8000);
- nothing may connect to the tunnel pods.

EKS enforces NetworkPolicies only when the VPC CNI's network policy feature is on. Check:

```bash
kubectl -n kube-system get daemonset aws-node -o yaml | grep -e "--enable-network-policy"
```

✅ **Check:** `--enable-network-policy=true`.

If it says `false` (and `vpc-cni` is an EKS add-on in your cluster), turn it on:

```bash
aws eks update-addon --region <W3> --cluster-name <W2> --addon-name vpc-cni \
  --configuration-values '{"enableNetworkPolicy":"true"}'
```

- **What this changes:** only pods that a NetworkPolicy selects are affected. If other namespaces already contain NetworkPolicies that were never enforced, they start being enforced too, so check with your cluster admin first.
- **Other engines:** a cluster running Calico or Cilium enforces the policies already.
- **No engine at all:** PQ still works, but the policies are not enforced.

### 1.10 Restricted outbound traffic (only if your cluster limits it)

- Allow the PQ pod HTTPS (443) to your AI provider's API host (e.g. `api.groq.com`; see [AI_PROVIDERS.md](AI_PROVIDERS.md)) and to your Workflows URL hosts (T1/T3, e.g. `*.powerplatform.com` or `*.logic.azure.com`).
- Allow the tunnel pods outbound TCP **and** UDP port **7844** to Cloudflare (`region1.v2.argotunnel.com`, `region2.v2.argotunnel.com`).

---

## Step 2 — Microsoft Teams (you're the owner, so it's all clicks)

PQ uses Teams in two directions, without a bot registration:

- **In:** an *outgoing webhook* in a team. When someone mentions `@PQ`, Teams sends the message to PQ (2.2).
- **Out:** a *Workflows webhook*. PQ posts the explanation to it as an Adaptive Card, and the workflow puts the card under the question (2.1).

### 2.1 Answers in the questions channel → T1

1. Use a **standard channel** in a team you own, e.g. `#ask-pq`. Teams outgoing webhooks don't work in private channels or chats.

   For security, make it a **private team** with only the people who may ask PQ as members: only a team's members can @mention its outgoing webhook.
2. In that channel: **…** → **Workflows** → **"Send webhook alerts to a channel"** → select that team and channel → **Create** → **copy the URL**. 📝 **T1**
3. **Make answers appear under the question (same thread).** As created, this workflow starts a *new post* for every card. PQ sends the question's thread with each answer (the field `threadMessageId`), so change the workflow's last step once:
   1. In Teams, open the **Workflows** app, find the workflow from step 2 and choose **Edit**.
   2. Open the step **Send each adaptive card**. Inside it is the action **Post card in a chat or channel**. Delete that action.
   3. In its place, add the Microsoft Teams action **Reply with an adaptive card in a channel** and fill in:
      - **Post as:** `Flow bot`
      - **Post in:** `Channel`
      - **Team** and **Channel:** your team and the questions channel
      - **Message ID:** switch the field to **Expression** and enter `triggerBody()?['threadMessageId']`
      - **Adaptive Card:** switch to **Expression** and enter `items('Send_each_adaptive_card')?['content']`
   4. **Save**.

   The field names above are how the designer showed them when this was written; if yours differ slightly, the two expressions are what matters.

A long explanation arrives as up to four cards, one after the other, in the same thread.

### 2.2 The outgoing webhook (questions) → T2

1. Team **…** → **Manage team** → **Apps** tab → click the **Upload an app ⌄** dropdown (top left, next to **Get more apps**) → **Create an outgoing webhook**. (Older Teams versions showed it as a link at the bottom right of the Apps tab.)
   - **Not in the dropdown?** Custom apps are switched off for you:
     - First, as team owner: team **…** → **Manage team** → **Settings** → **Member permissions** → tick **Allow members to upload custom apps**.
     - If it's still missing, a Teams admin must allow custom apps: Teams admin center → **Teams apps** → **Manage apps** → **Org-wide app settings** → **Let users interact with custom apps** on. Then **Teams apps** → **Setup policies** → your policy → **Upload custom apps** on.
     - Admin changes can take a few hours to reach your Teams client.
2. Fill in:
   - **Name:** `PQ` (what people @mention)
   - **Callback URL:** `https://<W4>/teams/webhook`
   - **Description:** `PQ - technical teacher`
3. **Create**, then **copy the security token**. It's shown only once. 📝 **T2**

The callback URL doesn't have to work yet. It starts working after Step 5.

### 2.3 (Optional, recommended) The administrators' channel → T3

PQ refuses questions about itself: how it is set up, which AI model it uses, its rules, its namespace ([RULES.md, section 3](RULES.md#3-boundaries-what-may-not-be-asked)). Each refusal can be reported to the administrators as a card with who asked what. Without T3 the refusals are only written to the log and kept in the internal API (`GET /security/restricted`).

As the owner of a **private channel** that only administrators can read:

1. In that channel: **…** → **Workflows** → **"Send webhook alerts to a channel"**. Select the team and the private channel → **Create** → **copy the URL**. 📝 **T3**
2. **If the private channel isn't offered in the template:**
   1. Create the workflow for any channel.
   2. Open **make.powerautomate.com** → **My flows** → that flow → **Edit**.
   3. In the **Post card in a chat or channel** step, pick the private channel, then **Save**. The URL stays the same.
3. **If posts fail later** with *BotNotInConversationRoster* in the flow's run history, change **Post as** in that step from **Flow bot** to **User**.

Leave this workflow as it is: it keeps **Post card in a chat or channel**. A notice doesn't belong to a thread. A chat works too (template **"Send webhook alerts to a chat"**). Don't use the questions channel: the people who asked would read the notices.

---

## Step 3 — AI provider key, namespace and Secret

### 3.1 AI provider and key (W7, required)

PQ answers every question with an AI model. One image supports many providers; you pick one with `LLM_PROVIDER` (Step 4.2) and put its key in the Secret as `LLM_API_KEY`. Without a provider PQ replies *"No AI model is configured, so I can't answer questions yet."*

| Provider | `LLM_PROVIDER` | Where to get the key |
|---|---|---|
| Groq (free plan available) | `groq` | console.groq.com → API Keys |
| Anthropic Claude | `anthropic` | console.anthropic.com → API Keys |
| Cursor (paid plan) | `cursor` | Cursor Dashboard → API Keys |
| Google Gemini (free tier available) | `gemini` | aistudio.google.com → Get API key |
| OpenAI | `openai` | platform.openai.com → API keys (also set `LLM_MODEL`) |
| Azure OpenAI / Microsoft Foundry | `azure-openai` | Azure portal → your resource → Keys and Endpoint (also `LLM_BASE_URL`, `LLM_MODEL`) |
| OpenRouter, Mistral, Ollama, any OpenAI-compatible API | `openrouter`, `mistral`, `ollama`, `custom` | see [AI_PROVIDERS.md](AI_PROVIDERS.md) |

📝 **W7** = the provider name and its key. Every provider, its defaults, limits and quirks: [AI_PROVIDERS.md](AI_PROVIDERS.md).

**Example: Groq.**

1. Sign in at **https://console.groq.com**.
2. Open **API Keys** → **Create API Key**, name it `pq`, and copy the key. It starts with `gsk_` and is shown only once.
3. Optional check from your laptop. A list of models means the key works:

```bash
curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer <W7 key>" | head -c 300
```

**Groq's free plan is rate-limited** (tokens per minute and per day: https://console.groq.com/docs/rate-limits). PQ keeps each request small and waits when Groq says "too many requests", so an explanation can take a few minutes. On this plan the longest reply is 2500 tokens by default, so a long explanation can be cut off; PQ then ends it with *"Reply `continue` for the rest."*

### 3.2 Namespace and Secret

Use **single quotes**, because the Workflows URLs contain `&` and `%`. The key names are the environment variables PQ reads.

```bash
kubectl create namespace pq

kubectl -n pq create secret generic pq-secrets \
  --from-literal=LLM_API_KEY='<W7 key>' \
  --from-literal=AGENT_API_KEY="$(openssl rand -hex 16)" \
  --from-literal=TEAMS_REPLY_WEBHOOK_URL='<T1>' \
  --from-literal=TEAMS_OUTGOING_WEBHOOK_SECRET='<T2>' \
  --from-literal=TEAMS_SECURITY_WEBHOOK_URLS='security=<T3>'
```

Leave out the last line (and the `\` before it) if you skipped 2.3.

| Key | What it's for | If you leave it out |
|---|---|---|
| `LLM_API_KEY` | The API key of the AI provider chosen with `LLM_PROVIDER` (3.1). The provider's own variable name (e.g. `GROQ_API_KEY`, `CURSOR_API_KEY`) works too. | PQ can't answer: "No AI model is configured" (not needed for `ollama` and `custom` endpoints without a key) |
| `AGENT_API_KEY` | Protects the internal API (`/chat`, `/status`, `/teams/test`, `/security/restricted`) and is needed for the checks in Step 6 | Internal API off (answers 403) |
| `TEAMS_REPLY_WEBHOOK_URL` | Where the answer cards go (the questions channel). With the workflow change in 2.1, they land in the question's thread. | Questions in Teams get "no reply webhook is set up" instead of an answer |
| `TEAMS_OUTGOING_WEBHOOK_SECRET` | Verifies that questions come from your Teams webhook | `/teams/webhook` switched off (404) |
| `TEAMS_SECURITY_WEBHOOK_URLS` | Where notices about refused questions go. Comma-separated; each entry `name=URL` (the name is optional and is shown in logs and `/status` instead of the secret URL). | Refusals are only logged and kept in `GET /security/restricted` |

Switching provider later? Put the new key in `LLM_API_KEY` (Step 7, *Change a secret*) and change `LLM_PROVIDER` in the ConfigMap.

✅ **Check:** `kubectl -n pq describe secret pq-secrets` lists the keys with their sizes in bytes (the values aren't shown).

---

## Step 4 — Edit `deploy/deployment.yaml` and deploy

The edits are marked `EDIT` in the file.

### 4.1 The image

Find the line that starts with `image: docker.io/` and put your Docker Hub user (W1) in it:

```bash
grep "image: docker.io" deploy/deployment.yaml      # → image: docker.io/<W1>/pq:latest
```

With `:latest` and `imagePullPolicy: Always`, a restart picks up the newest image. That is convenient for testing. In production pin a `sha-xxxxxxx` tag that CI pushed ([OPERATIONS.md](OPERATIONS.md#day-to-day), *Pin or roll back a version*).

### 4.2 The settings (ConfigMap `pq-config`)

| Setting | Set to |
|---|---|
| `LLM_PROVIDER` | The provider from W7, e.g. `"groq"`. Only this provider is used. |
| `LLM_MODEL`, `LLM_BASE_URL` | Leave `""` for the provider's defaults. Required for `openai` (model), `azure-openai` (both), `ollama` and `custom` (both). |
| `TIMEZONE` | Your time zone for the times on the administrators' cards, e.g. `"Asia/Kolkata"` (default UTC). |
| `TEAMS_TENANT_ID` | W6. PQ then only answers messages from your Microsoft 365 tenant. |
| `TEAMS_ALLOWED_USER_IDS` | Comma-separated Entra object IDs of the people who may ask. `""` = everyone who can mention the webhook. |

`TEAMS_TENANT_ID` and `TEAMS_ALLOWED_USER_IDS` hold real IDs in the file. **Replace them with your own**, or people in your tenant get *"This agent only answers its own organization."*

Where to find the IDs:

1. **Tenant ID:** Entra admin center (entra.microsoft.com) → **Overview** → copy the **Tenant ID**. 📝 **W6**
2. **Object IDs:** Entra admin center → **Users** → open each person → copy their **Object ID**.

```yaml
  TEAMS_TENANT_ID: "<W6>"
  TEAMS_ALLOWED_USER_IDS: "<object-id-1>,<object-id-2>"
```

The other settings have sensible defaults and a comment each in the file: the AI model's effort, size limits and timeout (`LLM_*`; `""` = the provider's defaults), the length of an explanation (`ANSWER_MAX_WORDS`), and the boundaries (`BOUNDARIES_ENABLED`, `PROTECTED_NAMESPACES`). All of them are explained in [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md).

If you install PQ in a namespace with another name, put that name in `PROTECTED_NAMESPACES`.

### 4.3 Permissions: none

Nothing to edit here. The file creates no ServiceAccount, Role or binding, and the pod runs with `automountServiceAccountToken: false`. PQ can't read or change anything in the cluster.

### 4.4 Deploy

```bash
kubectl apply -f deploy/deployment.yaml
kubectl -n pq rollout status deploy/pq
```

✅ **Check:**

```bash
kubectl -n pq get pods                 # pq-…   1/1   Running
kubectl -n pq logs deploy/pq
```

The log shows, among other lines:

- `AI model: <provider>, model <model>` — the provider loaded. If it says `PQ cannot answer questions until an AI provider is configured` instead, the text in front of it names what is missing.
- `Boundaries on: questions about PQ itself are refused and reported to …; protected namespaces: pq`
- `Teams webhook listening on :8080 (POST /teams/webhook only)`

✅ **Check that the pod has no Kubernetes token:**

```bash
kubectl -n pq get deploy pq -o jsonpath='{.spec.template.spec.automountServiceAccountToken}'   # false
```

---

## Step 5 — Questions from Teams through Cloudflare Tunnel

Teams sends every `@PQ` message from Microsoft's cloud to `https://<W4>/teams/webhook`. With Cloudflare Tunnel, **nothing in AWS accepts connections from the internet**: no load balancer, no public IP, no open port. Two small `cloudflared` pods open the connection *outward* to Cloudflare, and Cloudflare passes Teams' requests down it.

Questions from Teams are the main way to use PQ. If you skip this step, only the internal API (`POST /chat`) and the command line work ([USAGE.md](USAGE.md)).

```text
Teams (Microsoft's cloud) ──HTTPS──▶ Cloudflare <W4>: firewall lets only POST /teams/webhook through
   ──▶ tunnel, opened outward by 2 cloudflared pods (namespace pq)
   ──▶ Service pq-teams ──▶ PQ pod port 8080 (serves only POST /teams/webhook)
   ──▶ HMAC signature check ──▶ your tenant and allowed people only ──▶ "Working on it"
   ──▶ the explanation follows as a card through the Workflows webhook (T1)
```

| Layer | What it stops |
|---|---|
| No load balancer, no public IP, no inbound port in AWS | Nobody on the internet can reach the cluster directly |
| Cloudflare firewall and rate-limit rules (5.3) | Anything except `POST /teams/webhook` is blocked at Cloudflare; floods are cut off |
| Port 8080 serves only `/teams/webhook` | Even if a Cloudflare setting is wrong, the internal API can't be reached through the tunnel |
| NetworkPolicies (1.9) | Inside the cluster, only the tunnel pods can reach port 8080 |
| HMAC signature (T2) | Only your Teams outgoing webhook can make PQ do anything |
| Private team, `TEAMS_TENANT_ID`, `TEAMS_ALLOWED_USER_IDS` (2.1, 4.2) | Only the people you choose can ask |

### 5.1 Create the tunnel → C1

1. Cloudflare dashboard → **Networking** → **Tunnels** → **Create a tunnel**. On older dashboards the path is **Zero Trust** → **Networks** → **Tunnels**; there, choose the **Cloudflared** type.
2. Name it `pq-<W2>` and create it.
3. On the install screen, choose **Docker**. Copy **only the token**: the long string after `--token` in the command shown. Don't run the command. 📝 **C1**

### 5.2 Route W4 to PQ

In the tunnel, open the **Routes** tab (older dashboards: **Public Hostname**) → **Add route** → **Published application**.

> Not **Private hostname** or **Private network**: those only work for devices running the Cloudflare One (WARP) client. Teams calls from Microsoft's cloud without it, so it would never reach PQ.

Fill in:

| Field | Value |
|---|---|
| Hostname | a subdomain (e.g. `pq`) and the domain W5, giving W4 |
| Path (if the form shows it) | `^/teams/webhook$` |
| Service | `http://pq-teams.pq.svc.cluster.local` |

Save the route.

- Cloudflare creates the DNS record for W4 itself: a proxied CNAME to `<tunnel-id>.cfargotunnel.com`.
- If W4 already has a DNS record, delete it first, or the route can't be saved.

### 5.3 Cloudflare firewall rules for W4

In the Cloudflare dashboard, open the domain W5. In the two expressions below, replace `pq.example.com` with W4.

**1. Only the Teams webhook gets through.** **Security** → **WAF** (newer dashboards: **Security rules**) → **Custom rules** → **Create rule**:

- Name: `pq: only POST /teams/webhook`;
- **Edit expression**, and paste:

```text
(http.host eq "pq.example.com" and not (http.request.method eq "POST" and http.request.uri.path eq "/teams/webhook"))
```

- Action: **Block** → **Deploy**.

**2. Rate limit.** Same place → **Rate limiting rules** → **Create rule**:

- Name `pq: rate limit`; match `http.host eq "pq.example.com"`;
- counting by IP, more than **20** requests per **10 seconds** → **Block** for 10 seconds.

**3. HTTPS.** **SSL/TLS** → **Edge Certificates**:

- **Always Use HTTPS**: on;
- **Minimum TLS Version**: `TLS 1.2`.

Cloudflare's free Universal certificate covers W4, so you need no certificate in AWS.

**Don't:**

- put a **Cloudflare Access** application on W4: Teams can't sign in, so every question would be blocked;
- allowlist your office or VPN IP addresses: Teams calls from Microsoft's cloud, not from your users' network.

### 5.4 Store the token and start the tunnel

```bash
kubectl -n pq create secret generic pq-tunnel --from-literal=TUNNEL_TOKEN='<C1>'
kubectl apply -f deploy/cloudflare-tunnel.yaml
kubectl -n pq rollout status deploy/pq-tunnel
```

✅ **Check:** two `pq-tunnel-…` pods are Running and Ready, and the tunnel shows **Healthy** in the Cloudflare dashboard.

From your laptop:

```bash
curl -i -X POST https://<W4>/teams/webhook     # → 401: reached PQ, refused without a Teams signature
curl -i https://<W4>/status                    # → 403: blocked by Cloudflare (5.3)
```

✅ **Check:** `401`, then `403`. Anything else: see Troubleshooting.

The outgoing webhook's **Callback URL** (Step 2.2) is exactly `https://<W4>/teams/webhook`.

---

## Step 6 — Test end to end

### 6.1 The internal API

Open a second terminal and start a port-forward. Local port 8080 goes to the Service's port 80, which is the internal API (port 8000 in the pod):

```bash
kubectl -n pq port-forward svc/pq 8080:80
```

In the first terminal:

```bash
KEY=$(kubectl -n pq get secret pq-secrets -o jsonpath='{.data.AGENT_API_KEY}' | base64 -d)
curl -s localhost:8080/status -H "x-api-key: $KEY"
curl -s -X POST localhost:8080/chat -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"message": "what is a load balancer?"}'
```

✅ **Check:**

- `/status` shows `"llm": {"configured": true, "provider": "groq", "model": "openai/gpt-oss-120b", …}` (with your provider). If `configured` is `false`, its `status` says what is missing.
- `/status` shows `"teams": {"reply_webhook": "reply-1", "outgoing_webhook": true, "security_targets": ["security"]}` (the last list is empty if you skipped 2.3).
- `/chat` returns a `session_id` and an `answer` with a full explanation. On a free plan this can take a few minutes.

`POST /teams/test` sends a test card to the reply webhook (T1): `curl -s -X POST localhost:8080/teams/test -H "x-api-key: $KEY"`. `"ok": true` means the workflow accepted the request. The test card carries no thread, so after the thread change in 2.1 it may not appear in the channel; the workflow's run history shows what happened. The real test of T1 is a question in Teams (6.2).

### 6.2 Questions in Teams

In the channel from 2.1, type `@`, pick **PQ**, then try:

1. `@PQ help`: a short description with examples appears in the thread within seconds.
2. `@PQ what is a Kubernetes operator?`: first *"Working on it - the explanation will follow in this thread shortly."*, then one or more answer cards **in the same thread**: what it is, why it exists, how it works, a real-time scenario, an example, common mistakes, when to use it, what to remember and what to learn next.
3. Reply in that thread with `@PQ another example` or `@PQ go deeper`: PQ builds on the conversation and doesn't start again. A follow-up needs the `@PQ` mention too, because Teams only sends mentions to an outgoing webhook.
4. `@PQ Kafka vs RabbitMQ`: a comparison table and when to choose which.

On a free plan (Groq, Gemini) an answer can take a few minutes.

### 6.3 Boundaries

1. `@PQ how are you set up?` is answered with *"You are not authorised to ask this question. It has been reported to the administrators."*
2. If you did 2.3, the card **"Restricted question refused"** arrives in the administrators' channel with your name, the question and the reason.
3. `curl -s localhost:8080/security/restricted -H "x-api-key: $KEY"` lists the refusal.
4. `@PQ how do webhooks work?` is answered normally: any technology can be asked about, only PQ itself can't.

✅ **Check:** someone who is not in `TEAMS_ALLOWED_USER_IDS` (if you set it) and mentions PQ gets *"you're not on this agent's allowed-users list"*, and nothing is sent to the AI model.

**Done.** PQ answers technical questions in your channel.

---

## Step 7 — Day-to-day

The tasks you'll need most often:

| Task | How |
|---|---|
| **Roll out a new image** (after CI pushed a new `latest`) | `kubectl -n pq rollout restart deploy/pq` |
| **Change a setting** | Edit the ConfigMap in `deploy/deployment.yaml`, then `kubectl apply -f deploy/deployment.yaml && kubectl -n pq rollout restart deploy/pq` |
| **Change a secret** (AI key, Workflows URL, Teams token) | `kubectl -n pq patch secret pq-secrets --type merge -p '{"stringData":{"LLM_API_KEY":"<new>"}}'` (any key name), then `kubectl -n pq rollout restart deploy/pq` |
| **Update cloudflared** | The image in `deploy/cloudflare-tunnel.yaml` is pinned to a release. Change the tag to a newer one (Docker Hub `cloudflare/cloudflared` tags), then `kubectl apply -f deploy/cloudflare-tunnel.yaml`. The two pods are replaced one at a time, so questions keep working. |

Everything else (security updates, pinning a version, switching the AI provider, rotating tokens, uninstalling) is in [OPERATIONS.md](OPERATIONS.md#day-to-day). How people use PQ is in [USAGE.md](USAGE.md).

A restart empties PQ's memory: conversations start again and the list of refused questions is cleared.

---

## Troubleshooting

| Symptom | Check / fix |
|---|---|
| CI fails at **Docker login** | `DOCKERHUB_USERNAME` must be a **variable**, `DOCKERHUB_TOKEN` a **secret**, and the token must have **Read & Write** (Step 1.4) |
| CI didn't start after a push | The push changed nothing under `agent/`, `deploy/` or the workflow file. Use **Actions → CI → Run workflow** (Step 1.5). |
| Pod `ImagePullBackOff` | The image line doesn't name your Docker Hub user (Step 4.1); the repository isn't public; the tag doesn't exist; nodes have no internet/NAT route to Docker Hub (Step 1.8); or Docker Hub's anonymous pull limit was hit (`toomanyrequests`): pin a tag and set `imagePullPolicy: IfNotPresent`. See `kubectl -n pq describe pod -l app.kubernetes.io/name=pq` |
| Pod `CrashLoopBackOff` | `kubectl -n pq logs deploy/pq --previous` |
| No pod at all; `rollout status` times out at "0 out of 1 new replicas have been updated"; `kubectl -n pq get events` says `violates PodSecurity "restricted"` for a container `opentelemetry-auto-instrumentation-…` | The **CloudWatch Observability add-on** (Application Signals *auto monitor*) adds OpenTelemetry containers to pods that back a Service, and they don't meet the namespace's "restricted" level. `deployment.yaml` opts PQ out (`instrumentation.opentelemetry.io/inject-*: "false"`): `kubectl apply -f deploy/deployment.yaml`, then check with `kubectl -n pq get deploy pq -o jsonpath='{.spec.template.metadata.annotations}'` that they say `"false"`. If the add-on sets them back to `"true"` (older add-on versions), exclude the namespace in the add-on instead: add `"exclude": {"java": {"namespaces": ["pq"]}, "python": {…}, "nodejs": {…}, "dotnet": {…}}` under `manager.applicationSignals.autoMonitor` in the add-on's configuration. |
| `/status` and other internal endpoints answer 403 | `AGENT_API_KEY` isn't in the Secret: the internal API is off until it is. Add it and restart. |
| Internal endpoints answer 401 | The `x-api-key` header is missing or wrong. Read the key again from the Secret (Step 6.1). |
| Answers say "No AI model is configured" | `LLM_PROVIDER` is `none`, empty or misspelled, or the key/model/endpoint it needs is missing. `/status` → `llm.status` says exactly what; so does the log at startup. Restart the pod after changing the Secret. |
| PQ replies "I can't send my answer: no reply webhook is set up" | `TEAMS_REPLY_WEBHOOK_URL` isn't in the Secret (Step 3.2), or the pod wasn't restarted after adding it |
| "Working on it" but no answer card | `TEAMS_REPLY_WEBHOOK_URL` is wrong, or that workflow is off (log: `Teams target '…' returned …`). Also open the workflow's **run history**: a failed **Reply with an adaptive card** step shows why (wrong Team/Channel, or the Message ID expression mistyped). Workflows run under your Microsoft 365 account; if its connection expired, recreate the workflow and update the Secret. |
| The answer card says "Sorry, that failed (…)" | The AI provider returned an error or timed out. The reason is in the log after `Teams request failed`. Per-provider notes: [AI_PROVIDERS.md](AI_PROVIDERS.md) |
| Answers arrive as a new post instead of in the question's thread | The answers workflow still uses **Post card in a chat or channel**: do 2.1 step 3. PQ already sends `threadMessageId`. |
| The answer ends with "The answer reached the length limit. Reply `continue` for the rest." | The model's reply limit was reached. Reply `@PQ continue`, or raise `LLM_MAX_OUTPUT_TOKENS` (or lower `ANSWER_MAX_WORDS`) in the ConfigMap. |
| Answers take a few minutes | Normal on free plans such as Groq's: PQ waits for the per-minute token limit (log: `Model API returned 429; retrying in …`). |
| "I'm busy with other questions right now" | Six questions are already running or waiting. Ask again in a few minutes. |
| No card in the administrators' channel after a refused question | `TEAMS_SECURITY_WEBHOOK_URLS` isn't set (`/status` → `teams.security_targets` is empty), that workflow is off, or more than 20 refusals came within an hour (then they are only logged). If its run history shows *BotNotInConversationRoster*, set **Post as** to **User** (Step 2.3). `/security/restricted` lists them all. |
| Private channel not offered in the Workflows template | Create the workflow for any channel, then switch the channel in Power Automate (Step 2.3) |
| Tunnel pods `CreateContainerConfigError` | Secret `pq-tunnel` is missing or has no `TUNNEL_TOKEN` key (5.4) |
| Tunnel pods restart or stay not Ready | `kubectl -n pq logs deploy/pq-tunnel`. An authentication error means a wrong token: recreate the Secret with C1. Connection errors mean outbound TCP and UDP port 7844 to Cloudflare is blocked (Step 1.10). |
| Cloudflare error **1033** | The tunnel isn't connected: no tunnel pod is running (see the two rows above) |
| `502 Bad Gateway` from Cloudflare | The tunnel is up but can't reach PQ. Check: the route's Service is `http://pq-teams.pq.svc.cluster.local` (5.2); the PQ pod is Running; the tunnel pods carry the label `app.kubernetes.io/name: pq-tunnel`, which the NetworkPolicy allows |
| `403` on `POST /teams/webhook`, or Teams says "Sorry, there was a problem…" | Cloudflare blocked it: **Security** → **Events** shows which rule. Check the WAF expression (5.3). **Bot Fight Mode** on the domain can also block Teams' calls; switch it off for the domain if Events shows it. |
| `curl https://<W4>/…` can't resolve the name | The route wasn't saved, so there is no DNS record (5.2); `nslookup <W4>` should return Cloudflare addresses |
| `404` instead of `401` | `TEAMS_OUTGOING_WEBHOOK_SECRET` isn't in the Secret, or the pod wasn't restarted after adding it; or the route's Path doesn't match `/teams/webhook` (5.2) |
| Teams says "Sorry, there was a problem…" after `@PQ` | Wrong token (log: `Rejected /teams/webhook call`), or the URL isn't reachable |
| Everyone gets "This agent only answers its own organization." | `TEAMS_TENANT_ID` is not your tenant's ID (Step 4.2) |
| A teammate gets "not on this agent's allowed-users list" | Add their Entra Object ID to `TEAMS_ALLOWED_USER_IDS` (4.2), apply, restart |
| Someone gets "You are not authorised to ask this question" for an ordinary question | The question filter or the AI's rule read it as a question about PQ itself. See [OPERATIONS.md](OPERATIONS.md#troubleshooting). |

---

## Checklist

- [ ] 0  Worksheet W1–W7 filled in
- [ ] 1  Git, AWS CLI, kubectl installed; Docker Hub repo `pq` (public) + token; GitHub variable/secret set; CI green; image on Docker Hub; `kubectl get nodes` works; `pull-check` printed `pull ok`; network policies enforced (1.9)
- [ ] 2  Teams: T1 (answers workflow, replying in the thread), T2 (outgoing webhook token), T3 (administrators' channel, optional)
- [ ] 3  AI provider key (W7); namespace `pq` + Secret `pq-secrets` created
- [ ] 4  `deployment.yaml` edited: image, `LLM_PROVIDER`, `TEAMS_TENANT_ID`, `TEAMS_ALLOWED_USER_IDS`; applied; pod Running; log shows `AI model: …`
- [ ] 5  Tunnel created (C1); route W4 → `pq-teams`; WAF and rate-limit rules; Secret `pq-tunnel`; tunnel pods Ready; `curl` gives 401 and 403
- [ ] 6  `/status` shows the AI model and the Teams settings; `/chat` answers; `@PQ help` answered; a question gets its explanation in the thread; "how are you set up?" is refused and reported
- [ ] 7  You know how to restart, change a setting, change a secret and update cloudflared
