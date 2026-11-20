"""Entrypoint. Starts two servers:

  8000  internal API and health checks (app.app)
  8080  the public port (app.public_app): POST /teams/webhook and, when WEB_UI is set, the
        web UI's API under /api. In Kubernetes the Cloudflare tunnel forwards here and the
        NetworkPolicy only lets the tunnel pods in. A hosting platform that sets PORT
        (Render, ...) gets the public app on that port instead, so only it is exposed.

The public port runs in a background thread so the main server keeps handling signals. If
it dies /healthz fails and the pod is restarted.
"""
import logging
import os
import threading


def setup_logging():
    """Has to run before the other imports, they log while loading.
    LOG_LEVEL is DEBUG / INFO / WARNING / ERROR. Probe requests are kept out of the access log."""
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", force=True)

    class _NoProbes(logging.Filter):
        def filter(self, record):
            return not any(path in record.getMessage() for path in ('"GET /healthz', '"GET /ready'))
    logging.getLogger("uvicorn.access").addFilter(_NoProbes())


setup_logging()

import uvicorn  # noqa: E402

import app as app_module  # noqa: E402
from app import app, teams_app  # noqa: E402

logger = logging.getLogger("pq.main")

API_PORT = 8000
TEAMS_PORT = int(os.getenv("PORT", "") or 8080)   # the public port


def build_configs():
    """uvicorn configs for the api and teams servers."""
    api = uvicorn.Config(app, host="0.0.0.0", port=API_PORT, log_config=None)
    public = uvicorn.Config(teams_app, host="0.0.0.0", port=TEAMS_PORT, log_config=None, access_log=False)
    return api, public


def supervised(name: str, config, on_failure):
    """Thread that runs one server and reports when it stops (port in use, crash)."""
    def run():
        try:
            uvicorn.Server(config).run()
            on_failure(name, "stopped")
        except BaseException as e:  # uvicorn exits with SystemExit when the bind fails
            logger.error("The %s server stopped: %s: %s", name, type(e).__name__, e)
            on_failure(name, f"{type(e).__name__}: {e}")
    return threading.Thread(target=run, name=f"{name}-server", daemon=True)


def record_failure(name: str, why: str):
    app_module.SERVER_FAILURES[name] = why


def main():
    api, public = build_configs()
    supervised("teams", public, record_failure).start()
    logger.info("Public port :%d (POST /teams/webhook, and /api for the web UI when WEB_UI is set)", TEAMS_PORT)
    uvicorn.Server(api).run()


if __name__ == "__main__":
    main()
