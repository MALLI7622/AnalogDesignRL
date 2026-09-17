"""Bounded CPU HTTP/ngspice audit of parameter application; never loads an LLM."""
import argparse
import json
import math
from pathlib import Path
import re
import secrets
import threading

from analog_design.simulator import ROOT
from training.client import RemoteEpisode, WorkerClient
from training.preflight import check_simulator
from training.worker import EpisodeService, WorkerServer


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--output', required=True, type=Path)
    args = cli.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    environment = check_simulator()
    task_id = 'ramos_pfc_frontier_00f3831c949d08ca'
    service = EpisodeService(ROOT / 'datasets/analog_benchmark_250_v1/train_validation_catalog.json', output / 'worker')
    token = secrets.token_urlsafe(32)
    server = WorkerServer(('127.0.0.1', 0), service, token, workers=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = WorkerClient(f'http://127.0.0.1:{server.server_port}', token)
    remote = RemoteEpisode(client, task_id, max_steps=4, required_mode='evaluation',
                           trajectory_directory=output / 'trajectories')
    evidence = []
    try:
        initial = json.loads(remote.reset())
        episode_id = remote.key
        start = initial['current_parameters']
        expected = dict(start)
        actions = [{}, {'CURRENT_0_BIAS': start['CURRENT_0_BIAS'] * .98},
                   {'CAPACITOR_0': start['CAPACITOR_0'] * 1.02}, {'__not_editable__': 1}]
        previous_metrics = None
        for index, action in enumerate(actions, 1):
            observation, delta, done, info = remote.step(json.dumps(action))
            feedback = json.loads(observation)
            if index < 4:
                expected.update(action)
                assert feedback['status'] == 'ok', feedback
                assert feedback['parameters'] == expected
                assert info['simulator_invocations'] == 2
                spice = output / 'worker' / episode_id / f'evaluation_{index:03d}' / 'parameters.spice'
                text = spice.read_text()
                for key, value in expected.items():
                    match = re.search(rf'(?i)(?<!\w){re.escape(key)}\s*=\s*([^\s]+)', text)
                    assert match and math.isclose(float(match[1]), value, rel_tol=1e-12)
                if previous_metrics is not None:
                    assert feedback['metrics'] != previous_metrics, 'Metrics did not respond to changed parameters'
                previous_metrics = feedback['metrics']
                assert not done, 'Diagnostic unexpectedly solved the task; do not silently extend its episode'
            else:
                assert feedback['status'] == 'failed'
                assert feedback['parameters'] == expected
                assert info['simulator_invocations'] == 0
                assert done
            evidence.append({'attempt': index, 'action': action, 'expected_parameters': dict(expected),
                             'feedback': feedback, 'reward_delta': delta,
                             'simulator_invocations': info['simulator_invocations']})
        report = {'status': 'passed', 'scope': 'CPU only; manually specified diagnostic actions, no LLM inference or learning',
                  'environment': environment, 'task_id': task_id, 'episode_id': episode_id,
                  'checks': ['raw JSON crosses HTTP unchanged', 'partial updates reach parameters.spice',
                             'omitted parameters retain prior edits', 'real simulation metrics change',
                             'invalid keys spend an attempt without simulation or state change'],
                  'evaluation_attempts': 4, 'simulator_invocations': 6, 'evidence': evidence}
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({key: value for key, value in report.items() if key != 'evidence'}, indent=2))
    finally:
        try:
            remote.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == '__main__':
    main()
