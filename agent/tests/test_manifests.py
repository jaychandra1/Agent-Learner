"""Checks of deploy/*.yaml (and the rename), so a broken manifest fails CI."""
import re
from pathlib import Path

import yaml

from providers import CATALOG

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"


def load(name):
    return [d for d in yaml.safe_load_all((ROOT / "deploy" / name).read_text(encoding="utf-8")) if d]


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def settings_read_by_code() -> set:
    """Every environment variable the agent reads, including each provider's own key name."""
    names = {spec.key_env for spec in CATALOG.values()}
    for f in [*AGENT_DIR.glob("*.py"), *AGENT_DIR.glob("providers/*.py")]:
        names |= set(re.findall(r'(?:os\.getenv\(|_setting\(env, |_number\(env, )"([A-Z_]+)"',
                                f.read_text(encoding="utf-8")))
    return names


DOCS = load("deployment.yaml")
TUNNEL_DOCS = load("cloudflare-tunnel.yaml")
SECRET_KEYS = {"LLM_API_KEY", "GROQ_API_KEY", "CURSOR_API_KEY", "AGENT_API_KEY", "TEAMS_REPLY_WEBHOOK_URL",
               "TEAMS_OUTGOING_WEBHOOK_SECRET", "TEAMS_SECURITY_WEBHOOK_URLS"}


def test_all_objects_present_and_named_pq():
    kinds = sorted(d["kind"] for d in DOCS)
    assert kinds == sorted(["Namespace", "ConfigMap", "Deployment", "Service", "Service", "NetworkPolicy"])
    for d in DOCS + TUNNEL_DOCS:
        assert d["metadata"]["name"].startswith("pq"), d["metadata"]["name"]
        if d["kind"] != "Namespace":
            assert d["metadata"]["namespace"] == "pq", d["kind"]


def test_no_kubernetes_permissions_at_all():
    """PQ teaches; it never reads a cluster. No RBAC object, no service account token in the pod."""
    kinds = {d["kind"] for d in DOCS + TUNNEL_DOCS}
    assert not kinds & {"ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding",
                        "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
    for dep in by_kind(DOCS + TUNNEL_DOCS, "Deployment"):
        assert dep["spec"]["template"]["spec"]["automountServiceAccountToken"] is False, dep["metadata"]["name"]
    assert sorted(p.name for p in (ROOT / "deploy").iterdir()) == ["cloudflare-tunnel.yaml", "deployment.yaml"]


def test_configmap_keys_are_real_settings():
    (cm,) = by_kind(DOCS, "ConfigMap")
    unknown = set(cm["data"]) - settings_read_by_code()
    assert not unknown, f"ConfigMap keys the code never reads: {unknown}"
    assert cm["data"]["LLM_PROVIDER"] in CATALOG  # one provider, named explicitly
    assert not any(k.endswith("_API_KEY") for k in cm["data"]), "keys belong in the Secret"
    # shipped with the boundaries on, protecting the namespace everything here is installed in
    assert cm["data"]["BOUNDARIES_ENABLED"] == "true" and cm["data"]["PROTECTED_NAMESPACES"] == "pq"


def test_secret_keys_are_real_settings():
    assert SECRET_KEYS <= settings_read_by_code()


def test_no_setting_of_the_removed_features_is_left():
    gone = {"CLUSTER_NAME", "ALLOWED_NAMESPACES", "WATCH_ENABLED", "ACTIVITY_ENABLED", "TEAMS_ALERT_WEBHOOK_URLS",
            "REPORT_DIR", "RUNBOOK_DIR", "LLM_DEADLINE_SECONDS", "MAX_LLM_RCA_PER_HOUR"}
    (cm,) = by_kind(DOCS, "ConfigMap")
    assert not gone & settings_read_by_code() and not gone & set(cm["data"])


def test_deployment():
    (dep,) = by_kind(DOCS, "Deployment")
    spec = dep["spec"]
    assert spec["replicas"] == 1  # conversations live in the pod's memory: one copy
    # RollingUpdate must start the new pod before stopping the old one, so a broken image
    # never takes the running pod down.
    strategy = spec["strategy"]
    assert strategy["type"] in ("Recreate", "RollingUpdate")
    if strategy["type"] == "RollingUpdate":
        assert strategy["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}
    pod = spec["template"]["spec"]
    assert "serviceAccountName" not in pod
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert spec["selector"]["matchLabels"].items() <= spec["template"]["metadata"]["labels"].items()
    (c,) = pod["containers"]
    assert c["image"].startswith("docker.io/") and c["image"].endswith("/learner:latest")
    assert {p["name"]: p["containerPort"] for p in c["ports"]} == {"http": 8000, "teams": 8080}
    env_from = {next(iter(k for k in e if k.endswith("Ref"))): e for e in c["envFrom"]}
    assert env_from["configMapRef"]["configMapRef"]["name"] == "pq-config"
    assert env_from["secretRef"]["secretRef"] == {"name": "pq-secrets", "optional": True}
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert c["readinessProbe"]["httpGet"]["path"] == "/ready" and c["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert [m["name"] for m in c["volumeMounts"]] == ["tmp"] and c["volumeMounts"][0]["mountPath"] == "/tmp"
    assert [v["name"] for v in pod["volumes"]] == ["tmp"]


def test_pods_opt_out_of_automatic_instrumentation():
    """Injected OpenTelemetry init containers break the namespace's 'restricted' Pod Security level."""
    for dep in by_kind(DOCS + TUNNEL_DOCS, "Deployment"):
        ann = dep["spec"]["template"]["metadata"]["annotations"]
        for lang in ("java", "python", "nodejs", "dotnet"):
            assert ann[f"instrumentation.opentelemetry.io/inject-{lang}"] == "false", (dep["metadata"]["name"], lang)


def test_services_target_the_pod():
    services = {s["metadata"]["name"]: s for s in by_kind(DOCS, "Service")}
    (dep,) = by_kind(DOCS, "Deployment")
    for svc in services.values():
        assert svc["spec"]["selector"] == dep["spec"]["selector"]["matchLabels"]
        assert svc["spec"]["type"] == "ClusterIP"  # nothing is exposed outside the cluster
    assert services["pq"]["spec"]["ports"][0]["targetPort"] == "http"
    assert services["pq-teams"]["spec"]["ports"] == [{"name": "teams", "port": 80, "targetPort": "teams"}]


def test_no_load_balancer_or_ingress_anywhere():
    kinds = {d["kind"] for d in DOCS + TUNNEL_DOCS}
    assert "Ingress" not in kinds
    assert all(s["spec"].get("type", "ClusterIP") == "ClusterIP" for s in by_kind(DOCS, "Service"))


def test_network_policy_only_tunnel_reaches_the_teams_port():
    (pol,) = by_kind(DOCS, "NetworkPolicy")
    assert pol["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "pq"}
    assert pol["spec"]["policyTypes"] == ["Ingress"]
    rules = pol["spec"]["ingress"]
    allowed = {p["port"]: r.get("from") for r in rules for p in r["ports"]}
    assert set(allowed) == {8080}  # 8000, the internal API, is reachable from no pod
    assert allowed[8080] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "pq-tunnel"}}}]


def test_tunnel_deployment_is_locked_down():
    (dep,) = by_kind(TUNNEL_DOCS, "Deployment")
    spec = dep["spec"]
    pod = spec["template"]["spec"]
    assert spec["replicas"] >= 2
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    (c,) = pod["containers"]
    image, _, tag = c["image"].rpartition(":")
    assert image == "docker.io/cloudflare/cloudflared" and tag not in ("", "latest")
    assert c["args"][0] == "tunnel" and "--no-autoupdate" in c["args"] and c["args"][-1] == "run"
    (token,) = c["env"]
    assert token == {"name": "TUNNEL_TOKEN", "valueFrom": {"secretKeyRef": {"name": "pq-tunnel",
                                                                          "key": "TUNNEL_TOKEN"}}}
    sc = c["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert c["livenessProbe"]["httpGet"]["path"] == "/ready"
    labels = spec["template"]["metadata"]["labels"]
    (pol,) = by_kind(TUNNEL_DOCS, "NetworkPolicy")
    assert pol["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
    assert pol["spec"]["policyTypes"] == ["Ingress"] and not pol["spec"].get("ingress")  # nothing may connect in
    (pdb,) = by_kind(TUNNEL_DOCS, "PodDisruptionBudget")
    assert pdb["spec"]["minAvailable"] == 1


def test_image_and_requirements_carry_nothing_of_the_old_agent():
    requirements = (AGENT_DIR / "requirements.txt").read_text(encoding="utf-8")
    assert "kubernetes" not in requirements and "jinja2" not in requirements
    dockerfile = (AGENT_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 8000 8080\n" in dockerfile and "8443" not in dockerfile
    for gone in ("tools.py", "watcher.py", "activity.py", "rules.py", "report.py", "runbooks", "templates"):
        assert not (AGENT_DIR / gone).exists(), gone


def test_old_names_are_gone_everywhere():
    pattern = re.compile(r"kube[\s-]?support|sre-agent|SRE [Aa]gent", re.I)
    files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
             and ".pytest_cache" not in p.parts and p.suffix in {".py", ".md", ".yaml", ".yml", ".j2", ".sh", ".txt", ""}]
    leftovers = [str(p.relative_to(ROOT)) for p in files if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))
                 and p.name != "test_manifests.py"]
    assert not leftovers, leftovers
