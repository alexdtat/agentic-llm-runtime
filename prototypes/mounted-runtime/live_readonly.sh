#!/usr/bin/env bash
set -euo pipefail
umask 077

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
scripts="$PWD/prototypes/mounted-runtime"
evidence=$(mktemp -d /tmp/agentic-mounted-ro-evidence.XXXXXXXX)
printf 'Evidence directory: %s\n' "$evidence"
exec 3>&1
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$scripts"

phase=preflight
check_code=null
cleanup_code=null
export PROBE_ROOT=
owned_root=0
bind_attempted=0
bind_id=
local_socket=unix:///var/run/docker.sock

mount_ids() {
    timeout -k 2 3 awk -v target="$1" '$5 == target {print $1}' /proc/self/mountinfo
}

cleanup() {
    local current_id current_inode remaining
    if (( bind_attempted )); then
        timeout -k 3 65 "$scripts/stack.sh" cleanup || return "$?"
        current_id=$(mount_ids "$PROBE_ROOT/mount") || return "$?"
        if [[ -n $current_id ]]; then
            current_inode=$(stat -c '%d:%i' -- "$PROBE_ROOT/mount") || return "$?"
            if [[ ! $current_id =~ ^[0-9]+$ || $current_inode != "$bind_inode" ||
                  ( -n $bind_id && $current_id != "$bind_id" ) ]]; then
                printf '%s\n' 'Refusing to unmount an unexpected mount.' >&2
                return 1
            fi
            timeout -k 2 8 sudo -n umount -- "$PROBE_ROOT/mount" || return "$?"
        fi
    fi
    remaining=$(timeout -k 2 3 awk -v target="$PROBE_ROOT" \
        '$5 == target || index($5, target "/") == 1' /proc/self/mountinfo) || return "$?"
    if [[ -n $remaining ]]; then
        printf 'Mounts remain below %s\n' "$PROBE_ROOT" >&2
        return 1
    fi
    timeout -k 2 8 rm -rf --one-file-system -- "$PROBE_ROOT"
}

finish() {
    local probe_code=$? status=failed result_code=1
    trap - EXIT
    trap '' HUP INT TERM
    if (( owned_root )); then
        cleanup_code=0
        cleanup >"$evidence/cleanup.stdout.log" 2>"$evidence/cleanup.stderr.log" || cleanup_code=$?
    fi
    if [[ $probe_code == 0 && $check_code == 0 && $cleanup_code == 0 ]]; then
        status=passed
        result_code=0
    elif [[ $phase == preflight && $probe_code == 2 ]]; then
        status=blocked
        result_code=2
    fi
    printf '{"status":"%s","phase":"%s","probe_code":%s,"check_code":%s,"cleanup_code":%s,"root":"%s"}\n' \
        "$status" "$phase" "$probe_code" "$check_code" "$cleanup_code" "$PROBE_ROOT" >"$evidence/summary.json"
    cat "$evidence/summary.json" >&3
    exit "$result_code"
}

trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if ! timeout -k 3 35 python3 -c 'from live_failure import preflight; preflight()' \
        >"$evidence/preflight.stdout.log" 2>"$evidence/preflight.stderr.log"; then
    cat "$evidence/preflight.stderr.log" >&2
    exit 2
fi
export DOCKER_HOST=$local_socket

phase=prepare
prepared=$(timeout -k 2 15 python3 "$scripts/prepare.py" --access-mode ro 2>"$evidence/prepare.stderr.log")
[[ $prepared =~ ^/tmp/agentic-mounted-runtime\.[a-z0-9_]{8}$ ]]
PROBE_ROOT=$prepared
[[ $(realpath -e -- "$PROBE_ROOT") == "$PROBE_ROOT" && -O $PROBE_ROOT &&
   $(cat "$PROBE_ROOT/marker") == agentic-mounted-runtime &&
   $(cat "$PROBE_ROOT/access-mode") == ro ]]
owned_root=1

phase=setup
{
    scope=$(timeout -k 2 6 "$scripts/stack.sh" scope)
    [[ $scope =~ ^mount-v2-[a-f0-9]{12}$ ]]
    for label in "com.docker.compose.project=$scope" "agentic.scope=mounted-$scope"; do
        containers=$(timeout -k 2 6 docker --host "$local_socket" ps -aq --filter "label=$label")
        [[ -z $containers ]]
    done
    networks=$(timeout -k 2 6 docker --host "$local_socket" network ls -q \
        --filter "label=com.docker.compose.project=$scope")
    [[ -z $networks ]]
    parent=$(mount_ids "$PROBE_ROOT/mount")
    child=$(mount_ids "$PROBE_ROOT/mount/work")
    [[ -z $parent && -z $child ]]
    bind_inode=$(stat -c '%d:%i' -- "$PROBE_ROOT/mount")
    bind_attempted=1
    timeout -k 2 8 sudo -n mount --bind "$PROBE_ROOT/mount" "$PROBE_ROOT/mount"
    bind_id=$(mount_ids "$PROBE_ROOT/mount")
    [[ $bind_id =~ ^[0-9]+$ ]]
    timeout -k 2 8 sudo -n mount --make-rshared "$PROBE_ROOT/mount"
    timeout -k 3 45 "$scripts/stack.sh" up
} >"$evidence/setup.stdout.log" 2>"$evidence/setup.stderr.log"

phase=smoke
check_code=0
timeout -k 5 120 python3 "$scripts/smoke_readonly.py" \
    >"$evidence/smoke.stdout.log" 2>"$evidence/smoke.stderr.log" || check_code=$?
exit "$check_code"
