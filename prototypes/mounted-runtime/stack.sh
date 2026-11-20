#!/bin/sh
set -eu
cd "$(dirname "$0")"
die() { printf '%s\n' "$*" >&2; exit 1; }

: "${PROBE_ROOT:?Set PROBE_ROOT to the exact output from prepare.py}"
case "$PROBE_ROOT" in
    /tmp/agentic-mounted-runtime.????????) ;;
    *) die 'Unexpected diagnostic directory' ;;
esac
[ "$(realpath -e -- "$PROBE_ROOT")" = "$PROBE_ROOT" ] &&
[ -O "$PROBE_ROOT" ] &&
[ "$(cat "$PROBE_ROOT/marker")" = agentic-mounted-runtime ] &&
[ ! -L "$PROBE_ROOT/mount" ] ||
    die 'Invalid diagnostic directory'
access_mode=rw
if [ -f "$PROBE_ROOT/access-mode" ]; then
    access_mode=$(cat "$PROBE_ROOT/access-mode")
fi
case "$access_mode" in
    ro|rw) ;;
    *) die 'Invalid export access mode' ;;
esac
scope=mount-v2-$(printf '%s' "$PROBE_ROOT" | sha256sum | cut -c 1-12)

docker_local() {
    seconds=$1
    shift
    timeout -k 2 "$seconds" docker --host unix:///var/run/docker.sock "$@"
}
compose() {
    seconds=$1
    shift
    docker_local "$seconds" compose --env-file /dev/null -p "$scope" \
        -f compose.yaml -f "$PROBE_ROOT/workspace.json" "$@"
}
child_mount() {
    timeout -k 1 2 awk -v path="$PROBE_ROOT/mount/work" '
        $5 == path {
            for (i = 7; i < NF; i++) if ($i == "-") type[$(i + 1)] = 1
        }
        END { for (name in type) print name }
    ' /proc/self/mountinfo
}
absent_child() {
    mount_type=$(child_mount) || die 'Cannot inspect child mount'
    [ -z "$mount_type" ] || die 'Child mount still present (or existed before setup)'
}
runners() {
    docker_local 5 ps -aq --filter "label=agentic.scope=mounted-$scope"
}

case "${1:-}" in
    scope) printf '%s\n' "$scope" ;;
    up)
        timeout 2 findmnt -rnC --mountpoint "$PROBE_ROOT/mount" -o PROPAGATION | grep -qx shared ||
            die 'Prepare the exact shared self-bind described in README'
        absent_child
        [ ! -L "$PROBE_ROOT/mount/work" ] || die 'Invalid child mountpoint'
        [ -z "$(compose 5 ps -aq)" ] && [ -z "$(runners)" ] || die 'Diagnostic project already exists'
        diagnostic_image=$(docker_local 5 image inspect course-feasibility:2026-11-06 --format '{{.Id}}')
        [ "$diagnostic_image" = sha256:0b57fcf6e6b5d59a6a9f3255b89a1f2ba198d1870b63b6ad06d3199e1fb384f6 ] ||
            die 'Unexpected cached diagnostic image'
        docker_local 5 image inspect agentic-sandbox:cpu-v1 --format '{{.Id}}'
        compose 20 up --no-build --pull never -d --wait --wait-timeout 16
        [ "$(child_mount)" = fuse.sshfs ] || die 'SSHFS did not propagate to the exact host path'
        if [ "$access_mode" = ro ]; then
            options=$(timeout 2 findmnt -rnC --mountpoint "$PROBE_ROOT/mount/work" -o VFS-OPTIONS)
            [ -n "$options" ] || die 'Expected read-only SSHFS mount'
            while IFS= read -r row; do
                case ",$row," in
                    *,rw,*) die 'Expected read-only SSHFS mount' ;;
                    *,ro,*) ;;
                    *) die 'Expected read-only SSHFS mount' ;;
                esac
            done <<EOF
$options
EOF
        fi
        ;;
    sftp)
        docker_local 8 run --rm -i --pull never --runtime runc --read-only \
            --label "agentic.scope=mounted-$scope" \
            --network "${scope}_file-channel" --cpus .5 --memory 128m --memory-swap 128m \
            --pids-limit 16 --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges \
            --tmpfs /tmp:size=1m,mode=1777 \
            --mount "type=bind,src=$PROBE_ROOT/keys/broker,dst=/keys,readonly" \
            course-feasibility:2026-11-06 \
            sftp -q -b - -P 2222 -i /keys/client -o IdentitiesOnly=yes \
            -o UserKnownHostsFile=/keys/known_hosts -o StrictHostKeyChecking=yes ubuntu@exporter
        ;;
    cleanup)
        printf 'Checking runtime containers for %s\n' "$scope"
        ids=$(runners) || die 'Cannot inspect runtime containers'
        for id in $ids; do
            docker_local 7 rm -f "$id" || die 'Runtime container removal failed'
        done
        ids=$(runners) || die 'Cannot confirm runtime container removal'
        [ -z "$ids" ] || die 'Runtime containers remain'
        mount_type=$(child_mount) || die 'Cannot inspect child mount'
        if [ -n "$mount_type" ]; then
            [ "$mount_type" = fuse.sshfs ] || die 'Unexpected child mount type'
            # timeout runs Compose in a separate process group. A terminal read can stop it.
            if ! compose 5 exec -T broker fusermount3 -u /probe-mount/work </dev/null; then
                printf '%s\n' 'Broker unmount failed; checking mount after Compose down.' >&2
            fi
        fi
        printf 'Removing Compose resources for %s\n' "$scope"
        compose 12 down --timeout 2 || die 'Compose removal uncertain'
        containers=$(docker_local 5 ps -aq --filter "label=com.docker.compose.project=$scope") ||
            die 'Cannot verify project removal'
        ids=$(runners) || die 'Cannot confirm runtime container removal'
        networks=$(docker_local 5 network ls -q --filter "label=com.docker.compose.project=$scope") ||
            die 'Cannot inspect project networks'
        [ -z "$containers" ] && [ -z "$networks" ] && [ -z "$ids" ] || die 'Scoped Docker resources remain'
        mount_type=$(child_mount) || die 'Cannot inspect child mount'
        if [ -n "$mount_type" ]; then
            [ "$mount_type" = fuse.sshfs ] || die 'Unexpected residual mount type'
            printf '%s\n' 'Scoped Docker resources removed; SSHFS mount remains.' >&2
            exit 3
        fi
        printf '%s\n' 'Runtime containers, Compose resources and SSHFS mount removed; unmount the self-bind separately.'
        ;;
    *) die 'Usage: stack.sh up | scope | sftp | cleanup' ;;
esac
