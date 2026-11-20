#!/bin/sh
set -eu

host=${SSHFS_HOST:-exporter}
port=${SSHFS_PORT:-2222}
case "$host:$port" in
    exporter:2222) lifetime=180 ;;
    127.0.0.1:24222) lifetime=600 ;;
    *) printf '%s\n' 'Invalid SSHFS endpoint' >&2; exit 1 ;;
esac

case "${SSHFS_ACCESS_MODE:-rw}" in
    ro) set -- -o ro ;;
    rw) set -- ;;
    *) printf '%s\n' 'Invalid SSHFS access mode' >&2; exit 1 ;;
esac

attempt=0
until printf 'pwd\n' | timeout 2 sftp -q -b - -P "$port" -i /keys/client \
    -o IdentitiesOnly=yes -o UserKnownHostsFile=/keys/known_hosts \
    -o StrictHostKeyChecking=yes -o ConnectTimeout=1 "ubuntu@$host"; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 8 ] || exit 1
    sleep 1
done
exec timeout -k 3 "$lifetime" sshfs -f -p "$port" -o IdentityFile=/keys/client,IdentitiesOnly=yes \
    -o UserKnownHostsFile=/keys/known_hosts,StrictHostKeyChecking=yes,BatchMode=yes \
    -o ConnectTimeout=1,ServerAliveInterval=1,ServerAliveCountMax=2 \
    -o allow_other,default_permissions,dir_cache=no,attr_timeout=0,entry_timeout=0,negative_timeout=0 \
    "$@" "ubuntu@$host:/work" /probe-mount/work
