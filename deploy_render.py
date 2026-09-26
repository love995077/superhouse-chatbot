"""
Create (or redeploy) the Superhouse Chatbot as a Render Web Service via the Render REST API.

Reads from .env / environment:
    RENDER_API_KEY    (required)  Render API key - never commit it
    GITHUB_REPO_URL   (optional)  defaults to `git remote get-url origin`
    OPENAI_API_KEY    (optional)  pushed to Render only if it is a real key
    OPENAI_MODEL      (optional)
    RENDER_REGION     (optional)  default: singapore
    RENDER_PLAN       (optional)  default: free

Usage:  python deploy_render.py [--no-wait]
"""
import os
import subprocess
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.render.com/v1"
SERVICE_NAME = os.getenv("RENDER_SERVICE_NAME", "superhouse-chatbot")
BUILD_CMD = "pip install -r requirements.txt && python setup_db.py"
START_CMD = ("streamlit run app.py --server.port $PORT --server.address 0.0.0.0 "
             "--server.headless true")
TERMINAL_OK = {"live"}
TERMINAL_BAD = {"build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed"}


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def session():
    key = (os.getenv("RENDER_API_KEY") or "").strip()
    if not key:
        die("RENDER_API_KEY is not set (put it in .env).")
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {key}", "Accept": "application/json",
                      "Content-Type": "application/json"})
    return s


def call(s, method, path, **kw):
    r = s.request(method, API + path, timeout=60, **kw)
    if r.status_code >= 400:
        die(f"{method} {path} -> HTTP {r.status_code}: {r.text[:600]}")
    return r.json() if r.text else {}


def repo_url():
    url = os.getenv("GITHUB_REPO_URL")
    if not url:
        try:
            url = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip()
        except Exception:
            die("GITHUB_REPO_URL not set and no git 'origin' remote found.")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.split(":", 1)[1]
    return url.removesuffix(".git")


def env_vars():
    out = [{"key": "PYTHON_VERSION", "value": os.getenv("RENDER_PYTHON_VERSION", "3.12.8")},
           {"key": "OPENAI_MODEL", "value": os.getenv("OPENAI_MODEL", "gpt-4.1-mini")}]
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if key and "your_openai" not in key:
        out.append({"key": "OPENAI_API_KEY", "value": key})
    else:
        print("NOTE: OPENAI_API_KEY is a placeholder - the app will deploy, but answers need a real key.\n"
              "      Set it later in Render > Environment, or put it in .env and re-run this script.")
    return out


def find_service(s, owner_id):
    for item in call(s, "GET", "/services", params={"name": SERVICE_NAME, "ownerId": owner_id, "limit": 20}):
        svc = item.get("service", item)
        if svc.get("name") == SERVICE_NAME:
            return svc
    return None


def wait_for_deploy(s, service_id, timeout_s=1200):
    print("Waiting for the deploy to go live (free instances usually take 3-8 min)...")
    start, last = time.time(), None
    while time.time() - start < timeout_s:
        deploys = call(s, "GET", f"/services/{service_id}/deploys", params={"limit": 1})
        dep = (deploys[0].get("deploy", deploys[0])) if deploys else {}
        status = dep.get("status")
        if status != last:
            print(f"  [{int(time.time() - start):>4}s] {status}")
            last = status
        if status in TERMINAL_OK:
            return True
        if status in TERMINAL_BAD:
            return False
        time.sleep(15)
    print("Timed out waiting; check the Render dashboard.")
    return False


def main():
    s = session()
    owners = call(s, "GET", "/owners", params={"limit": 20})
    if not owners:
        die("No Render workspace/owner found for this API key.")
    owner = owners[0].get("owner", owners[0])
    print(f"Render workspace: {owner.get('name')} ({owner.get('id')})")

    repo = repo_url()
    svc = find_service(s, owner["id"])
    if svc:
        print(f"Service '{SERVICE_NAME}' already exists ({svc['id']}) - updating env and redeploying.")
        call(s, "PUT", f"/services/{svc['id']}/env-vars", json=env_vars())
        call(s, "POST", f"/services/{svc['id']}/deploys", json={"clearCache": "do_not_clear"})
    else:
        body = {
            "type": "web_service",
            "name": SERVICE_NAME,
            "ownerId": owner["id"],
            "repo": repo,
            "branch": os.getenv("GIT_BRANCH", "main"),
            "autoDeploy": "yes",
            "envVars": env_vars(),
            "serviceDetails": {
                "runtime": "python",
                "plan": os.getenv("RENDER_PLAN", "free"),
                "region": os.getenv("RENDER_REGION", "singapore"),
                "healthCheckPath": "/_stcore/health",
                "envSpecificDetails": {"buildCommand": BUILD_CMD, "startCommand": START_CMD},
            },
        }
        print(f"Creating web service '{SERVICE_NAME}' from {repo} ...")
        created = call(s, "POST", "/services", json=body)
        svc = created.get("service", created)

    svc = call(s, "GET", f"/services/{svc['id']}")
    url = (svc.get("serviceDetails") or {}).get("url")
    print(f"Service id : {svc['id']}")
    print(f"Dashboard  : {svc.get('dashboardUrl', 'https://dashboard.render.com')}")
    print(f"Live URL   : {url}")
    if "--no-wait" not in sys.argv:
        ok = wait_for_deploy(s, svc["id"])
        print("DEPLOYED - live at " + str(url) if ok else "Deploy did not reach 'live' - check the logs in the dashboard.")
        sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
