#!/bin/sh
# v2 container init: frozen clock + home layout + optional Gitea (all
# unchanged from v1), then the multi-service environment API on :9100
# (replaces the per-port Flask webapps + start_webapps.py).
#
# Expects (staged by evaluation/run_task.py):
#   /config/private/task_config.json  root-only runtime contract
#   /config/private/fixtures/<id>.json per-service fixtures (referenced by
#                                 task_config.services[].fixture, resolved
#                                 relative to /config by multi_server)
#   /config/private/oracle.env    root-only Oracle/grader credentials
#   /config/private/gitea/, agent_credentials.json   when gitea is set
set -e

echo "=== Authorization Sandbox v2 Initialization ==="

# pkg_resources for litellm (setuptools 81+ removed it); cheap if satisfied.
/usr/local/bin/python_default -c "import pkg_resources" 2>/dev/null || \
    /usr/local/bin/python_default -m pip install --quiet "setuptools<81" 2>/dev/null || true

TASK_CONFIG="${TASK_CONFIG:-/config/private/task_config.json}"

if [ ! -f "$TASK_CONFIG" ]; then
    echo "Warning: task_config.json not found at $TASK_CONFIG, skipping environment setup"
    exit 0
fi

########## NARRATIVE SANDBOX CLOCK (faketime, file-driven) ##########
# Anchor the in-container wall clock to task_config.reference_time — the
# bundle's ONLY absolute instant (services/_clock.py). The libfaketime spec
# lives in a FILE re-read on every call (FAKETIME_TIMESTAMP_FILE +
# FAKETIME_NO_CACHE), so multi_server's environment events can MOVE
# narrative time mid-run (advance_clock / set_clock) and every process —
# the agent's shell `date`, the services, file mtimes — jumps together.
# Monotonic clocks stay real (sleeps/timeouts unaffected).
REFERENCE_TIME=$(python3 -c "
import json
try:
    cfg = json.load(open('$TASK_CONFIG'))
    print(cfg.get('reference_time') or '')
except Exception:
    print('')
")
FAKETIME_LIB=""
for cand in /usr/lib/*/faketime/libfaketime.so.1 /usr/lib/faketime/libfaketime.so.1 /usr/local/lib/faketime/libfaketime.so.1; do
    [ -f "$cand" ] && FAKETIME_LIB="$cand" && break
done
CLOCK_FILE=/run/sandbox_clock/faketime.rc
if [ -n "$REFERENCE_TIME" ] && [ -n "$FAKETIME_LIB" ]; then
    FAKETIME_SPEC="@$(echo "$REFERENCE_TIME" | sed 's/T/ /;s/Z$//;s/\.[0-9]*$//')"
    mkdir -p "$(dirname "$CLOCK_FILE")"
    echo "$FAKETIME_SPEC" > "$CLOCK_FILE"
    export LD_PRELOAD="$FAKETIME_LIB"
    export FAKETIME_TIMESTAMP_FILE="$CLOCK_FILE"
    export FAKETIME_NO_CACHE=1
    export FAKETIME_DONT_FAKE_MONOTONIC=1
    export SANDBOX_REFERENCE_TIME="$REFERENCE_TIME"
    cat > /etc/profile.d/zz_faketime.sh <<FTEOF
export LD_PRELOAD="$FAKETIME_LIB"
export FAKETIME_TIMESTAMP_FILE="$CLOCK_FILE"
export FAKETIME_NO_CACHE=1
export FAKETIME_DONT_FAKE_MONOTONIC=1
export SANDBOX_REFERENCE_TIME="$REFERENCE_TIME"
FTEOF
    echo "init.sh: narrative clock active -> $FAKETIME_SPEC (file: $CLOCK_FILE, lib: $FAKETIME_LIB)"
else
    echo "init.sh: narrative clock NOT active (reference_time='$REFERENCE_TIME', faketime_lib='$FAKETIME_LIB')"
fi
#####################################################

########## HOME / WORKSPACE LAYOUT (unchanged from v1) ##########
AGENT_HOME=$(python3 -c "
import json
try:
    cfg = json.load(open('$TASK_CONFIG'))
    print(cfg.get('home', '/workspace'))
except Exception:
    print('/workspace')
")
if [ "$AGENT_HOME" = "/root" ]; then
    echo "init.sh: /root is private; remapping agent HOME to /workspace"
    AGENT_HOME=/workspace
fi
mkdir -p "$AGENT_HOME" "$AGENT_HOME/.config" "$AGENT_HOME/.cache" \
    "$AGENT_HOME/.local/share" "$AGENT_HOME/.npm-global"
chown -R agent:agent "$AGENT_HOME"
cat > /etc/profile.d/workspace_home.sh <<RCEOF
export HOME=$AGENT_HOME
export XDG_CONFIG_HOME=$AGENT_HOME/.config
export XDG_CACHE_HOME=$AGENT_HOME/.cache
export XDG_DATA_HOME=$AGENT_HOME/.local/share
export NPM_CONFIG_USERCONFIG=$AGENT_HOME/.npmrc
export NPM_CONFIG_PREFIX=$AGENT_HOME/.npm-global
export PIP_CACHE_DIR=$AGENT_HOME/.cache/pip
export GIT_CONFIG_GLOBAL=$AGENT_HOME/.gitconfig
export HISTFILE=/dev/null
[ -f /etc/profile.d/zz_faketime.sh ] && . /etc/profile.d/zz_faketime.sh
cd $AGENT_HOME 2>/dev/null || true
RCEOF
export HOME="$AGENT_HOME"
export XDG_CONFIG_HOME="$AGENT_HOME/.config"
export XDG_CACHE_HOME="$AGENT_HOME/.cache"
export XDG_DATA_HOME="$AGENT_HOME/.local/share"
export NPM_CONFIG_USERCONFIG="$AGENT_HOME/.npmrc"
export NPM_CONFIG_PREFIX="$AGENT_HOME/.npm-global"
export PIP_CACHE_DIR="$AGENT_HOME/.cache/pip"
export GIT_CONFIG_GLOBAL="$AGENT_HOME/.gitconfig"
export HISTFILE=/dev/null
echo "init.sh: agent HOME=$AGENT_HOME"
#####################################################

########## GITEA SETUP (optional, unchanged from v1) ##########
NEEDS_GITEA=$(python3 -c "
import json
try:
    with open('$TASK_CONFIG') as f:
        cfg = json.load(f)
    print('1' if cfg.get('gitea') else '0')
except Exception:
    print('0')
")

if [ "$NEEDS_GITEA" = "1" ]; then
    echo "Task requests Gitea; starting embedded git server..."
    mkdir -p /srv/gitea/data /srv/gitea/custom/conf /srv/gitea/log /srv/gitea/work

    SEED_SRC="/config/private/gitea"
    if [ -d "$SEED_SRC" ]; then
        echo "Seeding /srv/gitea/data from $SEED_SRC"
        cp -a "$SEED_SRC/." /srv/gitea/data/
    else
        echo "Warning: task_config.gitea is set but $SEED_SRC missing; starting empty Gitea"
    fi
    id -u git >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home /srv/gitea git
    chown -R git:git /srv/gitea

    export GITEA_INTERNAL_TOKEN="${GITEA_INTERNAL_TOKEN:-sandbox-internal-token-$(date +%s)}"
    envsubst < /srv/gitea/custom/conf/app.ini.template > /srv/gitea/custom/conf/app.ini
    chown git:git /srv/gitea/custom/conf/app.ini
    chmod 660 /srv/gitea/custom/conf/app.ini

    BRIDGE_PY="$(command -v python3)"
    "$BRIDGE_PY" -c "import flask, requests" 2>/dev/null || \
        "$BRIDGE_PY" -m pip install --quiet --disable-pip-version-check flask requests >/dev/null 2>&1 || true
    nohup "$BRIDGE_PY" /utils/gitea_bridge.py > /tmp/gitea_bridge.log 2>&1 &
    BRIDGE_PID=$!
    disown $BRIDGE_PID 2>/dev/null || true
    for attempt in $(seq 1 10); do
        curl -fs http://127.0.0.1:9999/health >/dev/null 2>&1 && { echo "Gitea bridge is ready"; break; }
        sleep 0.5
    done

    nohup su -s /bin/sh git -c \
        "GITEA_WORK_DIR=/srv/gitea/work GITEA_CUSTOM=/srv/gitea/custom \
         /usr/local/bin/gitea web --config /srv/gitea/custom/conf/app.ini" \
        > /tmp/gitea.log 2>&1 &
    GITEA_PID=$!
    disown $GITEA_PID 2>/dev/null || true

    echo "Waiting for Gitea on http://127.0.0.1:3000 ..."
    for attempt in $(seq 1 60); do
        curl -fs http://127.0.0.1:3000/api/v1/version >/dev/null 2>&1 && { echo "Gitea is ready"; break; }
        sleep 1
    done
    if ! curl -fs http://127.0.0.1:3000/api/v1/version >/dev/null 2>&1; then
        echo "ERROR: Gitea failed to come up; tail of /tmp/gitea.log:"
        tail -n 40 /tmp/gitea.log || true
    fi

    if [ -f /config/private/agent_credentials.json ]; then
        AGENT_LOGIN=$(python3 -c "
import json
try:
    cfg = json.load(open('$TASK_CONFIG'))
    print((cfg.get('gitea') or {}).get('agent_login') or '')
except Exception:
    print('')
")
        AGENT_TOKEN=$(python3 -c "
import json
try:
    creds = json.load(open('/config/private/agent_credentials.json'))
    print((creds.get('tokens') or {}).get('$AGENT_LOGIN') or '')
except Exception:
    print('')
")
        if [ -n "$AGENT_LOGIN" ] && [ -n "$AGENT_TOKEN" ]; then
            echo "Installing git credentials for $AGENT_LOGIN"
            git config --global credential.helper store
            mkdir -p "$HOME"
            printf "http://%s:%s@127.0.0.1:3000\n" "$AGENT_LOGIN" "$AGENT_TOKEN" > "$HOME/.git-credentials"
            chmod 600 "$HOME/.git-credentials"
            git config --global user.name "$AGENT_LOGIN"
            git config --global user.email "$AGENT_LOGIN@local.test"
            chown -R agent:agent "$HOME"
        fi
    fi
fi
#####################################################

########## ENVIRONMENT API (multi_server on :9100) ##########
echo "Starting environment API (multi_server)..."
export PYTHONPATH=/utils
set -a
. /config/private/oracle.env
set +a
nohup python3 -m services.multi_server --task-config "$TASK_CONFIG" \
    > /tmp/services.log 2>&1 &
SERVICES_PID=$!
disown $SERVICES_PID 2>/dev/null || true

for attempt in $(seq 1 30); do
    if curl -fs http://127.0.0.1:9100/_env/health >/dev/null 2>&1; then
        echo "Environment API ready (PID $SERVICES_PID): $(curl -fs http://127.0.0.1:9100/_env/health)"
        break
    fi
    sleep 1
done
if ! curl -fs http://127.0.0.1:9100/_env/health >/dev/null 2>&1; then
    echo "ERROR: environment API failed to come up; tail of /tmp/services.log:"
    tail -n 40 /tmp/services.log || true
    exit 1
fi
#####################################################

echo "=== Initialization complete ==="
