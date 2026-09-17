import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

project = 'interpretable-ml-moleculelens'
zone = 'us-west4-a'
request_id = 'analog-rl-v5e-request-20260917-24h'
node_id = 'analog-rl-v5e-20260917-24h'
public_key = Path('/home/cheriearjun/.ssh/analog_tpu_migration_20260917.pub').read_text().strip()
body = {
    'provisioningModel': 'FLEX_START',
    'runDuration': {'maxRunDuration': '86400s'},
    'queueingPolicy': {'validUntilDuration': '3600s'},
    'tpu': {'nodeSpec': [{
        'parent': f'projects/{project}/locations/{zone}',
        'nodeId': node_id,
        'node': {
            'acceleratorType': 'v5litepod-4',
            'runtimeVersion': 'v2-alpha-tpuv5-lite',
            'networkConfig': {'network': 'default', 'enableExternalIps': True},
            'serviceAccount': {
                'email': '67636381610-compute@developer.gserviceaccount.com',
                'scope': ['https://www.googleapis.com/auth/cloud-platform'],
            },
            'metadata': {'ssh-keys': 'cheriearjun:' + public_key},
            'labels': {'research': 'analog-rl', 'purpose': 'migration'},
        },
    }]},
}
root = Path(__file__).parent
(root / 'replacement-request.json').write_text(json.dumps(body, indent=2) + '\n')
token = subprocess.check_output(['gcloud', 'auth', 'print-access-token'], text=True).strip()
base = f'https://tpu.googleapis.com/v2alpha1/projects/{project}/locations/{zone}/queuedResources'
headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
# Do not create a second allocation if a previous invocation already succeeded.
try:
    with urllib.request.urlopen(urllib.request.Request(base + '/' + request_id, headers=headers), timeout=60) as response:
        existing = json.load(response)
    print(json.dumps({'already_exists': True, 'name': existing['name'], 'state': existing.get('state')}))
except urllib.error.HTTPError as exc:
    if exc.code != 404:
        raise SystemExit(exc.read().decode())
    request = urllib.request.Request(base + '?queuedResourceId=' + request_id,
                                     data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            operation = json.load(response)
    except urllib.error.HTTPError as create_error:
        raise SystemExit(create_error.read().decode())
    (root / 'creation-operation.json').write_text(json.dumps(operation, indent=2) + '\n')
    print(json.dumps(operation))
