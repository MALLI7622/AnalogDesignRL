"""Local HTTP service for trusted ngspice episodes. No model-generated code runs here."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time

from analog_design.episode import Episode
from analog_design.model_clients import strict_json
from analog_design.simulator import digest, parameters_for
from training.catalog import load_catalog
from training.research_pilot import PilotAuthorization

MAX_BODY = 32768


def public_action_feedback(result, task, before, action):
    """Expose public action facts and fixed diagnostics, never raw server errors."""
    try:
        parameters_for(task, {**before, **action} if isinstance(action, dict) else action)
        valid = True
    except ValueError:
        valid = False
    changed = sorted(k for k, v in result['parameters'].items() if before.get(k) != v)
    feedback = {'action_valid': valid, 'parameters_changed': changed,
                'measurement_success': result['status'] == 'ok'}
    error = result.get('error', '')
    if not valid:
        category, message = 'invalid_action', 'Action rejected: use allowed names, finite numeric values within each parameter\'s own bounds, and integers where required.'
    elif result['status'] == 'ok':
        category, message = None, None
    elif result['status'] == 'timeout':
        category, message = 'simulation_timeout', 'The action was valid, but simulation timed out. Try a different candidate.'
    elif error in {'Expected positive low-frequency gain and non-inverting phase.',
                   'AC sweep must extend below unity gain.', 'Ambiguous or upward zero-dB crossing.',
                   'Multiple unity crossings: stability requires further analysis.',
                   'Expected one downward unity-gain crossing in the measured band.',
                   'AC sweep is incomplete.', 'AC sweep is empty or has invalid frequencies.',
                   'AC frequencies must increase strictly.'}:
        category, message = 'ac_measurement_failed', 'The action was valid, but AC measurements could not be extracted reliably. Try a different, modest parameter change.'
    elif error in {'Transient response does not settle within the observation window.',
                   'Transient response has insufficient time settled inside the tolerance.',
                   'Insufficient transient samples.', 'Transient simulation is incomplete.',
                   'Transient time moves backwards.',
                   'Transient output contains a gap larger than the permitted time step.'}:
        category, message = 'transient_measurement_failed', 'The action was valid, but transient measurements could not be extracted reliably. Try a different candidate.'
    elif error in {'Python and ngspice measurements disagree; inspect the crosscheck.',
                   'The two measurement paths disagree on pass/fail.'}:
        category, message = 'measurement_disagreement', 'The action was valid, but independent measurements disagreed. This evaluation is unsuccessful.'
    else:
        category, message = 'evaluation_failed', 'The action was valid, but simulation or measurement failed. Try a different candidate.'
    feedback['failure_category'] = category
    if message is not None:
        feedback['error'] = message
    return feedback


class ServiceError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class EpisodeService:
    def __init__(self, catalog, output, *, mode="evaluation", max_active=16, timeout_s=30, ttl_s=1800,
                 pilot_manifest=None):
        if mode not in {"evaluation", "training", "research-pilot"} or max_active < 1 or timeout_s <= 0 or ttl_s <= 0:
            raise ValueError("Invalid service configuration")
        if (mode == "research-pilot") != (pilot_manifest is not None):
            raise ValueError("Only research-pilot mode requires and accepts a pilot manifest")
        self.manifest, self.tasks = load_catalog(catalog, training=mode == "training")
        self.pilot = PilotAuthorization(pilot_manifest, catalog) if pilot_manifest is not None else None
        self.catalog_sha256 = digest(catalog)
        self.mode, self.max_active, self.timeout_s, self.ttl_s = mode, max_active, timeout_s, ttl_s
        self.output = Path(output).resolve()
        if self.pilot is not None:
            self.pilot.claim_worker(self.output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.sessions = {}
        self.lock = threading.Lock()

    def catalog(self):
        if self.pilot is not None:
            self.pilot.assert_current()
        return {"mode": self.mode, "catalog_sha256": self.catalog_sha256,
                **(self.pilot.public() if self.pilot is not None else {}),
                "split_policy": self.manifest["split_policy"], "max_active": self.max_active,
                "tasks": [{key: task[key] for key in ("id", "sha256", "split", "group", "max_evaluations")}
                          for task in self.tasks.values()]}

    def create(self, task_id):
        with self.lock:
            # Expired sessions keep their files but release memory/capacity.
            now = time.monotonic()
            for key, session in list(self.sessions.items()):
                if now - session["touched"] > self.ttl_s and session["lock"].acquire(blocking=False):
                    del self.sessions[key]
                    session["lock"].release()
            if len(self.sessions) >= self.max_active:
                raise ServiceError(503, "Episode capacity reached; close unused episodes")
            task = self.tasks.get(task_id)
            if task is None:
                raise ServiceError(404, "Unknown task ID")
            if digest(task["task_path"]) != task["sha256"]:
                raise ServiceError(503, "Task changed; restart with a reviewed catalog")
            if self.pilot is not None:
                self._reserve_pilot("episode")
            key = secrets.token_hex(16)
            episode = Episode(task["task_path"], self.output / key, self.timeout_s,
                              for_training=self.mode == "training")
            if self.pilot is not None:
                # Restrict the live episode without rewriting its frozen task definition.
                episode.budget = min(episode.budget, self.pilot.limits["max_evaluations_per_episode"])
            self.sessions[key] = {"episode": episode, "lock": threading.Lock(), "touched": now,
                                  "replies": {}, "failed": False}
            return {"episode_id": key, "specification": episode.specification(), "mode": self.mode}

    def step(self, key, number, action):
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ServiceError(400, "step must be a positive integer")
        with self.lock:
            session = self.sessions.get(key)
        if session is None:
            raise ServiceError(404, "Episode missing or expired")
        fingerprint = json.dumps(action, sort_keys=True, allow_nan=False)
        with session["lock"]:
            session["touched"] = time.monotonic()
            if session["failed"]:
                raise ServiceError(503, "Episode encountered an infrastructure error")
            previous = session["replies"].get(number)
            if previous:
                if previous[0] != fingerprint:
                    raise ServiceError(409, "Retry changed the action")
                return previous[1]
            episode = session["episode"]
            if number != len(episode.history) + 1 or episode.done:
                raise ServiceError(409, "Wrong step number or finished episode")
            if self.pilot is not None:
                with self.lock:
                    self._reserve_pilot("evaluation")
            try:
                before = dict(episode.parameters)
                result = episode.step(action)
            except Exception:
                session["failed"] = True
                raise
            # Paths and subprocess diagnostics stay in server-side artifacts.
            public = {k: v for k, v in result.items() if k != "error"}
            public.update(public_action_feedback(result, episode.task, before, action))
            session["replies"][number] = (fingerprint, public)
            session["touched"] = time.monotonic()
            return public

    def _reserve_pilot(self, kind):
        try:
            self.pilot.reserve(kind)
        except ValueError as exc:
            raise ServiceError(503, str(exc)) from exc

    def close(self, key):
        with self.lock:
            session = self.sessions.get(key)
        if session is not None:
            with session["lock"]:
                with self.lock:
                    self.sessions.pop(key, None)
        return {"closed": True}


class WorkerServer(HTTPServer):
    """Bound running handlers and waiting requests; simulations run in host threads."""
    def __init__(self, address, service, token, workers=4):
        if address[0] != "127.0.0.1":
            raise ValueError("Bind to loopback; use an SSH tunnel for a separate CPU host")
        if not token or len(token) < 24 or workers < 1:
            raise ValueError("Use a token of at least 24 characters and positive worker count")
        self.service, self.token = service, token
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.slots = threading.BoundedSemaphore(workers * 2)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            self.shutdown_request(request)
            return
        self.pool.submit(self._handle, request, client_address)

    def _handle(self, request, client_address):
        try:
            request.settimeout(10)
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self.slots.release()

    def server_close(self):
        super().server_close()
        self.pool.shutdown(wait=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never put credentials or generated actions in access logs.

    def respond(self, status, payload):
        data = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, post):
        try:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + self.server.token):
                raise ServiceError(401, "Invalid worker token")
            if not post and self.path == "/catalog":
                return self.respond(200, self.server.service.catalog())
            if not post:
                raise ServiceError(404, "Unknown endpoint")
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > MAX_BODY:
                raise ServiceError(413, "Request body too large or missing")
            body = strict_json(self.rfile.read(size).decode())
            if not isinstance(body, dict):
                raise ServiceError(400, "Expected a JSON object")
            if self.path == "/episodes":
                result = self.server.service.create(body["task_id"])
            elif self.path == "/step":
                result = self.server.service.step(body["episode_id"], body["step"], body["action"])
            elif self.path == "/close":
                result = self.server.service.close(body["episode_id"])
            else:
                raise ServiceError(404, "Unknown endpoint")
            self.respond(200, result)
        except ServiceError as exc:
            self.respond(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, UnicodeError):
            self.respond(400, {"error": "Invalid request"})
        except Exception:
            self.respond(503, {"error": "Worker failure; inspect server artifacts and restart"})

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)


def main():
    from training.preflight import check_simulator
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--catalog", required=True)
    cli.add_argument("--output", required=True)
    cli.add_argument("--mode", choices=["evaluation", "training", "research-pilot"], default="evaluation")
    cli.add_argument("--pilot-manifest", help="Explicitly authorized bounded research-pilot manifest")
    cli.add_argument("--port", type=int, default=8765)
    cli.add_argument("--workers", type=int, default=4)
    cli.add_argument("--max-active", type=int, default=16)
    args = cli.parse_args()
    check_simulator()
    service = EpisodeService(args.catalog, args.output, mode=args.mode, max_active=args.max_active,
                             pilot_manifest=args.pilot_manifest)
    with WorkerServer(("127.0.0.1", args.port), service, os.environ.get("ANALOG_WORKER_TOKEN"), args.workers) as server:
        print(f"Simulator service: http://127.0.0.1:{args.port}, mode={args.mode}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
