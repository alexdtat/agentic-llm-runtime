#!/bin/sh
set -eu
cd "$(dirname "$0")"

die() { printf '%s\n' "$*" >&2; exit 1; }
limit() {
    seconds=$1
    shift
    timeout -k 2 "$seconds" "$@"
}

prepare() {
    [ "$(id -u)" = 1000 ] || die 'Fixture exporter expects host UID 1000.'
    umask 077
    root=$(mktemp -d /tmp/agentic-mount-probe.XXXXXXXX)
    mkdir -p "$root/client/.git" "$root/mount/work" "$root/keys/exporter" "$root/keys/broker"
    chmod 0755 "$root/client" "$root/mount" "$root/mount/work"
    printf 'client-initial\n' > "$root/client/client.txt"
    printf 'synthetic-git-canary\n' > "$root/client/.git/probe"
    printf 'synthetic-outside-canary\n' > "$root/outside.txt"
    chmod 0644 "$root/outside.txt"
    ln -s ../outside.txt "$root/client/outward"
    ssh-keygen -q -t ed25519 -N '' -f "$root/keys/exporter/host"
    ssh-keygen -q -t ed25519 -N '' -f "$root/keys/broker/client"
    cp "$root/keys/broker/client.pub" "$root/keys/exporter/client.pub"
    printf '[exporter]:2222 %s\n' "$(ssh-keygen -y -f "$root/keys/exporter/host")" \
        > "$root/keys/broker/known_hosts"
    printf 'agentic-mount-probe\n' > "$root/probe-marker"
    printf '%s\n' "$root"
}

case "${1:-}" in
    prepare) prepare; exit ;;
    run|cleanup) mode=$1 ;;
    *) die 'Usage: probe.sh prepare | run | cleanup' ;;
esac

: "${PROBE_ROOT:?Set PROBE_ROOT from probe.sh prepare}"
case "$PROBE_ROOT" in
    /tmp/agentic-mount-probe.????????) ;;
    *) die 'Refusing unexpected probe root.' ;;
esac
[ "$(realpath -e -- "$PROBE_ROOT")" = "$PROBE_ROOT" ] || die 'Probe root is not canonical.'
[ -O "$PROBE_ROOT" ] && [ "$(cat "$PROBE_ROOT/probe-marker")" = agentic-mount-probe ] ||
    die 'Probe ownership marker missing.'
[ ! -L "$PROBE_ROOT/mount" ] && [ ! -L "$PROBE_ROOT/client" ] || die 'Unexpected symlink.'
project=mountprobe-$(printf '%s' "$PROBE_ROOT" | sha256sum | cut -c 1-12)
export PROBE_ROOT
docker_local() {
    seconds=$1
    shift
    limit "$seconds" docker --host unix:///var/run/docker.sock "$@"
}
compose() {
    seconds=$1
    shift
    docker_local "$seconds" compose --env-file /dev/null -p "$project" -f compose.yaml "$@"
}

child_mount() {
    # A shared self-bind can report the same FUSE type under two mount IDs.
    types=$(limit 2 findmnt -rnC --mountpoint "$PROBE_ROOT/mount/work" --output FSTYPE) ||
        return "$?"
    printf '%s\n' "$types" | sort -u
}

cleanup() {
    # Remove the sandbox before touching FUSE. Never traverse or delete a mount.
    compose 12 rm -sf sandbox || die 'Sandbox removal uncertain; leave mount in place.'
    remaining=$(compose 5 ps -aq sandbox) || die 'Cannot verify sandbox removal.'
    [ -z "$remaining" ] || die 'Sandbox still exists; leave mount in place.'
    if mount_type=$(child_mount); then
        [ "$mount_type" = fuse.sshfs ] || die 'Unexpected child mount type; leaving it in place.'
        if ! compose 5 exec -T broker fusermount3 -u /probe-mount/work; then
            printf '%s\n' 'Broker unmount failed; checking the exact host mountpoint.' >&2
        fi
    else
        status=$?
        [ "$status" = 1 ] || die 'Cannot inspect the exact host mountpoint.'
    fi
    compose 12 down --timeout 2 || die 'Compose removal uncertain.'
    remaining=$(compose 5 ps -aq) || die 'Cannot verify project removal.'
    [ -z "$remaining" ] || die 'Project containers remain.'
    networks=$(docker_local 5 network ls -q --filter "label=com.docker.compose.project=$project") ||
        die 'Cannot verify project network removal.'
    [ -z "$networks" ] || die 'Project networks remain.'
    if child_mount; then
        die 'Host FUSE mount remains. See the explicit recovery in README.'
    else
        status=$?
        [ "$status" = 1 ] || die 'Cannot verify host unmount.'
    fi
    printf 'cleanup confirmed for project %s; host self-bind still needs explicit umount\n' "$project"
}

if [ "$mode" = cleanup ]; then
    cleanup
    exit
fi

limit 2 findmnt -rnC --mountpoint "$PROBE_ROOT/mount" --output PROPAGATION | grep -qx shared ||
    die 'Prepare the exact probe self-bind with shared propagation first.'
if child_mount; then
    die 'Unexpected pre-existing child mount.'
else
    status=$?
    [ "$status" = 1 ] || die 'Cannot inspect the child mountpoint.'
fi
image=$(docker_local 5 image inspect --format '{{.Id}}' course-feasibility:2026-11-06)
[ "$image" = sha256:0b57fcf6e6b5d59a6a9f3255b89a1f2ba198d1870b63b6ad06d3199e1fb384f6 ] ||
    die 'Cached diagnostic image ID mismatch.'
docker_local 5 image inspect --format '{{.Id}}' agentic-sandbox:cpu-v1
existing=$(compose 5 ps -aq)
[ -z "$existing" ] || die 'Probe project already exists. Run cleanup first.'

compose 20 up --no-build --pull never -d --wait --wait-timeout 16 exporter broker
[ "$(child_mount)" = fuse.sshfs ] || die 'SSHFS did not propagate to the exact host path.'
compose 10 up --no-build --pull never -d --wait --wait-timeout 7 sandbox
sandbox_id=$(compose 5 ps -q sandbox)
[ -n "$sandbox_id" ] || die 'Sandbox not running.'
compose 5 exec -T sandbox python3 -c "from pathlib import Path; assert Path('/work/client.txt').read_text() == 'client-initial\\n'"
printf 'client-updated\n' > "$PROBE_ROOT/client/client.txt"
compose 5 exec -T sandbox python3 -c "from pathlib import Path; p=Path('/work'); assert (p/'client.txt').read_text() == 'client-updated\\n'; (p/'agent.txt').write_text('agent-write\\n')"
[ "$(cat "$PROBE_ROOT/client/agent.txt")" = agent-write ] || die 'Agent write not visible on client.'
printf '%s\n' 'Bidirectional changes confirmed.'

# These diagnostics deliberately report a policy failure rather than hiding a canary.
compose 5 exec -T sandbox python3 - <<'PY'
import os
from pathlib import Path

for name in (".git/probe", "outward"):
    path = Path("/work") / name
    if path.is_symlink():
        print(f"{name} -> {os.readlink(path)}")
    try:
        print(f"{name}: readable ({path.read_text().strip()})")
    except OSError as exc:
        print(f"{name}: read failed ({exc.errno})")
PY
if printf 'get /outside.txt /tmp/outside-canary\n' | compose 5 exec -T broker \
    sftp -q -b - -P 2222 -i /keys/client -o IdentitiesOnly=yes \
    -o UserKnownHostsFile=/keys/known_hosts -o StrictHostKeyChecking=yes ubuntu@exporter; then
    compose 5 exec -T broker cat /tmp/outside-canary
    printf '%s\n' 'POLICY GAP: the SFTP account can read a sibling of the selected folder.'
else
    status=$?
    printf 'Direct sibling diagnostic exit=%s; check broker logs before interpreting.\n' "$status"
fi

namespace_pid=$(compose 5 exec -T sandbox python3 - "$project" < writer.py)
case "$namespace_pid" in ''|*[!0-9]*) die 'Detached child PID missing.' ;; esac
host_pid=$(docker_local 5 top "$sandbox_id" -eo pid,args | awk -v marker="$project" '$NF == marker {print $1}')
case "$host_pid" in ''|*[!0-9]*) die 'Detached child not found in sandbox process listing.' ;; esac
sleep 0.3
before=$(cat "$PROBE_ROOT/client/heartbeat.txt")
sleep 0.3
after=$(cat "$PROBE_ROOT/client/heartbeat.txt")
[ -n "$before" ] && [ -n "$after" ] && [ "$before" != "$after" ] || die 'Detached child did not advance.'
compose 8 stop -t 2 sandbox
[ "$(docker_local 5 inspect --format '{{.State.Running}} {{.State.Pid}}' "$sandbox_id")" = 'false 0' ] ||
    die 'Sandbox stop is unconfirmed.'
[ ! -e "/proc/$host_pid" ] || die 'Detached writer host PID still exists.'
stopped=$(cat "$PROBE_ROOT/client/heartbeat.txt")
sleep 0.5
[ "$(cat "$PROBE_ROOT/client/heartbeat.txt")" = "$stopped" ] || die 'Write continued after stop.'
printf 'Detached writer stopped (sandbox PID %s, host PID %s).\n' "$namespace_pid" "$host_pid"

compose 10 up --no-deps --no-build --pull never -d sandbox
compose 5 exec -T sandbox python3 -c "from pathlib import Path; assert (Path('/work')/'agent.txt').read_text() == 'agent-write\\n'"
printf 'exists before channel loss\n' > "$PROBE_ROOT/client/disconnect.txt"
compose 8 stop -t 2 exporter
set +e
# Start outside the disconnected mount so Python can test the read itself.
compose 8 exec -T -w / sandbox python3 - <<'PY_READ'
import errno
import sys
from pathlib import Path

try:
    print(Path('/work/disconnect.txt').read_text())
except OSError as exc:
    print(f"post-disconnect filesystem error: {exc.errno} {exc.strerror}", file=sys.stderr)
    sys.exit(42 if exc.errno in (errno.ENOTCONN, errno.EIO, errno.ECONNRESET, errno.ETIMEDOUT) else 43)
PY_READ
status=$?
set -e
case "$status" in
    0) die 'Post-disconnect read succeeded unexpectedly.' ;;
    124|137) printf 'Post-disconnect read did not return before deadline (exit=%s).\n' "$status" ;;
    42) printf '%s\n' 'Post-disconnect read returned a filesystem transport error.' ;;
    *) die "Post-disconnect read is inconclusive (exit=$status). Inspect stderr." ;;
esac
printf '%s\n' 'Run complete. Use probe.sh cleanup even after a failed run.'
