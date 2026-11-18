#!/bin/sh
set -eu

attempt=0
until printf 'pwd\n' | timeout 2 sftp -q -b - -P 2222 -i /keys/client \
    -o IdentitiesOnly=yes -o UserKnownHostsFile=/keys/known_hosts \
    -o StrictHostKeyChecking=yes -o ConnectTimeout=1 ubuntu@exporter; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 8 ] || exit 1
    sleep 1
done
exec timeout -k 3 180 sshfs -f -p 2222 -o IdentityFile=/keys/client,IdentitiesOnly=yes \
    -o UserKnownHostsFile=/keys/known_hosts,StrictHostKeyChecking=yes,BatchMode=yes \
    -o ConnectTimeout=1,ServerAliveInterval=1,ServerAliveCountMax=2 \
    -o allow_other,default_permissions,dir_cache=no,attr_timeout=0,entry_timeout=0,negative_timeout=0 \
    ubuntu@exporter:/work /probe-mount/work
