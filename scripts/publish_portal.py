"""Publishes the APIM Developer Portal, so model catalog and model API changes are visible to developers.

  publish_portal.py <environment> [note]

Same mechanism as the agent pipeline: a new portal revision made current.
- The portal must be enabled (v2 tiers create APIM with it disabled): the script fails with the fix if it isn't.
- Concurrent publishes (several runs at once) get 429 from APIM: the script retries, honouring Retry-After.
- Polls are throttled too: a 429 or an empty reply is retried. A publish that still doesn't complete is a warning,
  not a failure: the APIs and docs are registered before this step and appear with the next publish.
- If the portal has never been opened in the Azure portal (APIM > Developer portal), Azure hasn't provisioned it
  yet: the script warns instead of failing; the APIs and docs are registered and appear once it's published.
"""
import datetime
import os
import sys
import time

import requests
import yaml
from azure.identity import AzureCliCredential

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cred = AzureCliCredential(process_timeout=60)


def main(environment, note):
    env = yaml.safe_load(open(os.path.join(ROOT, "platform", "environments", f"{environment}.yaml"), encoding="utf-8"))
    g = env["gateway"]
    base = (f"https://management.azure.com/subscriptions/{g['subscription_id']}/resourceGroups/{g['resource_group']}"
            f"/providers/Microsoft.ApiManagement/service/{g['apim_name']}")
    headers = {"Authorization": f"Bearer {cred.get_token('https://management.azure.com/.default').token}"}
    svc = requests.get(base, params={"api-version": "2024-05-01"}, headers=headers, timeout=60).json()
    if svc.get("properties", {}).get("developerPortalStatus") == "Disabled":
        sys.exit(f"The Developer Portal of {g['apim_name']} is disabled. Enable it (APIM > Developer portal, or "
                 "properties.developerPortalStatus = Enabled) and re-run.")
    for attempt in range(1, 11):
        rev = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        r = requests.put(f"{base}/portalRevisions/{rev}", params={"api-version": "2024-05-01"}, headers=headers,
                         json={"properties": {"description": note[:100], "isCurrent": True}}, timeout=120)
        if r.status_code != 429:
            break
        wait = int(r.headers.get("Retry-After", 30)) + 5 * attempt
        print(f"Another publish is in progress (429); retrying in {wait}s ({attempt}/10)")
        time.sleep(wait)
    if r.status_code == 412:
        print("WARNING: the Developer Portal isn't provisioned yet. Open it once from the Azure portal "
              "(APIM > Developer portal) and re-run; the APIs and docs are already registered.")
        return
    if r.status_code not in (200, 201, 202):
        print(f"WARNING: Developer Portal publish not accepted ({r.status_code} {r.text[:200]}); the APIs and docs are "
              "registered and appear with the next publish.")
        return
    end, status = time.time() + 900, None
    while time.time() < end and status not in ("completed", "failed"):
        time.sleep(10)
        p = requests.get(f"{base}/portalRevisions/{rev}", params={"api-version": "2024-05-01"}, headers=headers,
                         timeout=60)
        try:
            status = p.json()["properties"].get("status") if p.status_code == 200 else None
        except ValueError:   # throttled polls can come back empty
            status = None
    if status != "completed":
        print(f"WARNING: Developer Portal revision {rev} did not complete ({status or 'timed out'}); the APIs and docs "
              "are registered and appear with the next publish.")
        return
    print(f"Developer Portal published (revision {rev}): https://{g['apim_name']}.developer.azure-api.net")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "model pipeline")
