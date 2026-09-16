#!/usr/bin/env bash
# GCE startup script: mount the separately retained disk; never start paid training.
set -euo pipefail
ANALOG_DEVICE=/dev/disk/by-id/google-analog-rl-data
ANALOG_MOUNT=/mnt/data
test "$(id -u)" -eq 0

for attempt in {1..30}; do
  test -b "$ANALOG_DEVICE" && break
  sleep 1
done
test -b "$ANALOG_DEVICE" || { printf '%s\n' 'Attached analog-rl-data disk not found' >&2; exit 1; }

if mountpoint -q "$ANALOG_MOUNT"; then
  test "$(readlink -f "$(findmnt -n -o SOURCE --target "$ANALOG_MOUNT")")" = "$(readlink -f "$ANALOG_DEVICE")"
  printf '%s\n' 'Analog RL data disk already mounted.'
  exit 0
fi

if ANALOG_PROBE="$(blkid -p -o export "$ANALOG_DEVICE")"; then
  # Reject partitioned or unexpected filesystems instead of reformatting them.
  if ! printf '%s\n' "$ANALOG_PROBE" | grep -qx 'TYPE=ext4' || \
      printf '%s\n' "$ANALOG_PROBE" | grep -q '^PTTYPE='; then
    printf '%s\n' 'Data disk has a different signature; inspect it manually.' >&2
    exit 1
  fi
else
  ANALOG_PROBE_STATUS=$?
  test "$ANALOG_PROBE_STATUS" -eq 2 || exit "$ANALOG_PROBE_STATUS"
  ANALOG_ALLOW_FORMAT="$(curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
    -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/attributes/analog-rl-format-blank-disk)"
  test "$ANALOG_ALLOW_FORMAT" = true || { printf '%s\n' 'Blank disk needs explicit first-boot initialization.' >&2; exit 1; }
  test -z "$(wipefs --no-act --noheadings --output TYPE "$ANALOG_DEVICE")"
  test "$(lsblk --noheadings --raw --output TYPE "$ANALOG_DEVICE")" = disk
  mkfs.ext4 -m 0 "$ANALOG_DEVICE"
fi

mkdir -p "$ANALOG_MOUNT"
mount -o defaults,nosuid,nodev "$ANALOG_DEVICE" "$ANALOG_MOUNT"
printf '%s\n' 'Analog RL data disk ready at /mnt/data. Create your private workspace here before cloning.'
