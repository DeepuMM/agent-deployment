"""Publishes the APIM Developer Portal, so model catalog and model API changes are visible to developers.

  publish_portal.py <environment> [note]

Same mechanism as the agent pipeline: a new portal revision made current. If the portal has never been opened in
the Azure portal (APIM > Developer portal), Azure hasn't provisioned it yet: the script warns instead of failing,
because the APIs and their docs are already registered and appear once the portal is opened and published.
"""
import datetime
import os
import sys
import time

import requests
import yaml
from azure.identity import AzureCliCredential

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
cred = AzureCliCredential(process_timeout=60)


def main(environment, note):
    env = yaml.safe_load(open(os.path.join(ROOT, "platform", "environments", f"{environment}.yaml"), encoding="utf-8"))
    g = env["gateway"]
    base = (f"https://management.azure.com/subscriptions/{g['subscription_id']}/resourceGroups/{g['resource_group']}"
            f"/providers/Microsoft.ApiManagement/service/{g['apim_name']}")
    rev = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    headers = {"Authorization": f"Bearer {cred.get_token('https://management.azure.com/.default').token}"}
    r = requests.put(f"{base}/portalRevisions/{rev}", params={"api-version": "2024-05-01"}, headers=headers,
                     json={"properties": {"description": note[:100], "isCurrent": True}}, timeout=120)
    if r.status_code == 412:
        print("WARNING: the Developer Portal isn't provisioned yet. Open it once from the Azure portal "
              "(APIM > Developer portal) and re-run; the APIs and docs are already registered.")
        return
    if r.status_code not in (200, 201, 202):
        sys.exit(f"Developer Portal publish failed: {r.status_code} {r.text[:300]}")
    end = time.time() + 900
    while time.time() < end:
        s = requests.get(f"{base}/portalRevisions/{rev}", params={"api-version": "2024-05-01"}, headers=headers,
                         timeout=60).json()["properties"]
        if s.get("status") in ("completed", "failed"):
            if s["status"] != "completed":
                sys.exit(f"Developer Portal publish failed: {s.get('statusDetails')}")
            print(f"Developer Portal published (revision {rev}): https://{g['apim_name']}.developer.azure-api.net")
            return
        time.sleep(10)
    sys.exit("Timed out waiting for the Developer Portal to publish")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "model pipeline")
