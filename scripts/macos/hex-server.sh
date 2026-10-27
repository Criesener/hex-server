#!/bin/zsh
# Run the server in Docker on macOS and point the Mac Steam client at it.
#   ./hex-server.sh create           build-free first run of the container (needs the image)
#   ./hex-server.sh start|stop|logs  manage the container
#   ./hex-server.sh client-local     point the Mac client at 127.0.0.1
#   ./hex-server.sh client-original  restore the original client config
#
# Environment:
#   HEX_APP     path to Hex.app (default: Steam's default library)
#   HEX_STATE   host directory for hconnect.db and the config backup (default: ./server-state)
#   HEX_IMAGE   docker image tag (default: hex-server:local)
set -e
HEX_APP="${HEX_APP:-$HOME/Library/Application Support/Steam/steamapps/common/HEX SHARDS OF FATE/Hex.app}"
HEX_STATE="${HEX_STATE:-$PWD/server-state}"
HEX_IMAGE="${HEX_IMAGE:-hex-server:local}"
CFG="$HEX_APP/Contents/HexEnv.app/Contents/Resources/config.ini"
DATA="$HEX_APP/Contents/Resources/Data"
BACKUP="$HEX_STATE/config.ini.original"

case "$1" in
  create)
    mkdir -p "$HEX_STATE"
    docker run -d --name hex-server -p 9933:9933 -p 8081:8081 --user 1001:1001 \
      -v "$HEX_STATE:/hex/state" -v "$DATA:/client-data:ro" \
      -e HEX_DB_PATH=/hex/state/hconnect.db -e HEX_GAMEDATA=/client-data/gamedata \
      -e HEX_PROFILE_FLAGS=allowcon "$HEX_IMAGE" ;;
  start) docker start hex-server && echo "waiting for server..." &&
         until curl -s -m3 -o /dev/null http://127.0.0.1:8081/; do sleep 3; done && echo "ready" ;;
  stop) docker stop hex-server ;;
  logs) docker logs -f --tail 50 hex-server ;;
  client-local)
    mkdir -p "$HEX_STATE"
    [[ -f "$BACKUP" ]] || cp "$CFG" "$BACKUP"
    sed -e 's|^GameServerIP=.*|GameServerIP=127.0.0.1:9933|' \
        -e 's|^CZEAuthUrl=.*|CZEAuthUrl=http://127.0.0.1:8081/auth/hexlogin|' \
        -e 's|^CZEPayUrl=.*|CZEPayUrl=http://127.0.0.1:8081/auth/hexpaylinkses|' \
        -e 's|^NewsEventsURL=.*|NewsEventsURL=http://127.0.0.1:8081/NewsEvents/NewsEvents-{0}.txt|' \
        "$BACKUP" > "$CFG"
    cat "$CFG" ;;
  client-original) cp "$BACKUP" "$CFG" && echo "original config restored" ;;
  *) echo "usage: $0 create|start|stop|logs|client-local|client-original"; exit 1 ;;
esac
