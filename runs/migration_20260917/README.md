# TPU replacement: 2026-09-17

Replacement node: `analog-rl-v5e-20260917-24h`
Queued request: `analog-rl-v5e-request-20260917-24h`
Project: `interpretable-ml-moleculelens`
Zone: `us-west4-a`
Hardware/runtime: `v5litepod-4`, `v2-alpha-tpuv5-lite`
Allocation ends: `2026-09-18T05:23:32Z`
Workspace: `/home/cheriearjun/AnalogDesignRL`
Project backup: `gs://molecule-lens/analog-design/`

## Connect

Run from your local machine or Cloud Shell with your Google account signed in:

```bash
gcloud compute tpus tpu-vm ssh cheriearjun@analog-rl-v5e-20260917-24h \
  --zone=us-west4-a --project=interpretable-ml-moleculelens
```

Then:

```bash
cd /home/cheriearjun/AnalogDesignRL
source training/activate.sh
```

Code, Git history, datasets, run outputs and checkpoints were transferred. The
Gemma model cache was copied separately without authentication tokens. The Python
environment and simulator are rebuilt by the existing bootstrap procedure.
The original workspace path is retained because checkpoint provenance currently
binds that absolute path.

## Validation results

- All 361 CPU tests passed. Simulator verification and the HTTP worker smoke test passed.
- JAX detected four TPU devices; JAX 0.11.1, Tunix 0.1.7, Python 3.12.14 and ngspice 47 match the source environment. The installed package freeze matches exactly.
- Checkpoint 007, step 2 restored with its final adapter hash verified. One TPU inference episode completed and invoked ngspice twice. This episode did not solve its circuit; migration success does not imply model improvement.
- A checksum comparison of all transferred project files found no file-content differences.
- `bootstrap.log`: package installation, ngspice build, device models and initial checks. The first verification failed because two CPU numerical tests were inadvertently run on TPU; `finish-bootstrap.sh` runs the intended CPU tests and checks TPU separately.
- `verification.log`, `smoke.log`, `tests.log`: successful final verification, worker smoke test and 361-test CPU suite.
- `tpu.json`, `simulator.json`: detected runtime versions.
- `checkpoint_rollout/`: one inference episode restored from the existing two-update checkpoint; no optimizer updates.
- `validation-complete.json`: written only when checkpoint inference and source provenance checks pass.
- `source-manifest.json`: original dependency and checkpoint metadata hashes.

The existing GitHub CLI/login and active Hugging Face login were transferred
directly over SSH to the same user's private directories and verified as
`MALLI7622` and `arjun7622`. Credential files have mode 0600 and are excluded from
Git and Cloud Storage backups. The existing pinned Gemma weights can also be used
offline from the copied model cache. Cloud Storage uses the VM service account.
The new worker has its own private token.

This migration does not implement exact optimizer/session resumption or certify
model quality. Check the actual validation files rather than treating this
README as a success marker.

## Save new work to Cloud Storage

Run `bash runs/migration_20260917/backup-project.sh` after new experiments and before
deleting the replacement VM. Its boot disk is temporary. This copies project
files, datasets, checkpoints and run outputs to the existing project prefix and
excludes environments, downloaded dependencies, caches and local credentials.

Git author identity remains Mallikarjuna (`tmallikarjuna111@gmail.com`). GitHub
credential integration is configured and the active Hugging Face login is valid.

## Codex sessions

Codex CLI 0.154.0, its settings, login, skills and all three saved conversations
were transferred privately to the replacement home directory. SQLite databases
were copied with the online backup API. The snapshot from
`2026-09-17T05:59:28Z` passed file checksum and database integrity checks, and the
new Codex app server successfully listed and read all three histories. No new
model turn was started during validation.

On the replacement VM, use `codex resume --all` to select a saved conversation.
To reopen the VM migration conversation specifically:

```bash
cd /home/cheriearjun
~/.local/bin/codex resume 01a0ad7c-176b-7741-bdce-dd64d32505ca
```

The running process and terminal connection do not transfer. This is a snapshot;
further messages written on the old VM after the snapshot are not automatically
synchronized. Codex history and credentials are kept outside the project and
were not uploaded to the project's GCP backup. Verification details are in
`/home/cheriearjun/codex-migration-verification.json` on the replacement VM.
