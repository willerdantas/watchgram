#!/bin/sh
# A pasta data/ montada do host pode ter sido criada como root pelo Docker:
# ajusta o dono e depois roda o app sem privilégios.
set -e
if [ "$(id -u)" = "0" ]; then
    chown -R bot:bot /app/data
    export HOME=/home/bot
    exec setpriv --reuid=bot --regid=bot --init-groups "$@"
fi
exec "$@"
