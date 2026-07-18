#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# Laptop Diagnostics Self-Registering Agent  —  Linux / macOS
# Zero dependency — uses only built-in system tools.
#
# Requirements (all pre-installed on every Linux/macOS):
#   bash, curl, sha1sum (or shasum on macOS), awk, cat, grep
#   Optional: lm-sensors (sudo apt install lm-sensors) for voltage/fan
#
# Configuration — set env vars OR edit the CONFIG block below:
#   DIAG_SERVER_URL   http://<server-ip>:5000
#   DIAG_API_KEY      your agent API key
#   DIAG_INTERVAL     seconds between reports (default 600 = 10 min)
#   DIAG_EMAIL        alert email for this laptop
#   DIAG_NAME         display name (default: hostname)
#   DIAG_CATEGORY     basic|midrange|highend|gaming|workstation
#
# Usage:
#   chmod +x laptop_agent.sh
#   ./laptop_agent.sh --test          # test sensors only
#   ./laptop_agent.sh --once          # register + one report
#   ./laptop_agent.sh                 # run forever
#   ./laptop_agent.sh --install       # install as systemd service (Linux)
#   ./laptop_agent.sh --uninstall     # remove systemd service
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── CONFIG (env vars take precedence) ────────────────────────────────────────
SERVER_URL="${DIAG_SERVER_URL:-http://localhost:5000}"
API_KEY="${DIAG_API_KEY:-}"
INTERVAL="${DIAG_INTERVAL:-600}"
CATEGORY="${DIAG_CATEGORY:-midrange}"
ALERT_EMAIL="${DIAG_EMAIL:-}"
DISP_NAME="${DIAG_NAME:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.agent_state.json"
LOG_FILE="$SCRIPT_DIR/laptop_agent.log"
SERVICE_NAME="laptop-diag-agent"

# ── Defaults ──────────────────────────────────────────────────────────────────
D_CPU_TEMP=60.0
D_FAN_RPM=2500.0
D_CPU_V=1.20
D_RAM_V=1.25
D_GPU_V=1.00
D_RAIL_3V3=3.30
D_RAIL_5V=500.0

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
log() {
    local level="${2:-INFO}"
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local line="$ts [$level] $1"
    echo "$line"
    echo "$line" >> "$LOG_FILE"
}

# ─────────────────────────────────────────────────────────────────────────────
# Machine Identity
# ─────────────────────────────────────────────────────────────────────────────
get_mac() {
    # Try various sources to get a stable MAC address
    local mac=""
    # Linux
    if command -v ip &>/dev/null; then
        mac=$(ip link show 2>/dev/null | awk '/ether/ && !/00:00:00:00:00:00/ {print $2; exit}')
    fi
    # Fallback: /sys
    if [[ -z "$mac" ]]; then
        for f in /sys/class/net/*/address; do
            local addr; addr=$(cat "$f" 2>/dev/null || true)
            if [[ "$addr" != "00:00:00:00:00:00" && -n "$addr" ]]; then
                mac="$addr"
                break
            fi
        done
    fi
    # macOS
    if [[ -z "$mac" ]] && command -v ifconfig &>/dev/null; then
        mac=$(ifconfig 2>/dev/null | awk '/ether/ && !/00:00:00:00:00:00/ {print $2; exit}')
    fi
    echo "${mac:-$(hostname)}"
}

get_machine_id() {
    local mac; mac=$(get_mac)
    local raw="$mac:$(hostname)"
    # sha1sum on Linux, shasum on macOS
    if command -v sha1sum &>/dev/null; then
        echo -n "$raw" | sha1sum | awk '{print $1}'
    elif command -v shasum &>/dev/null; then
        echo -n "$raw" | shasum -a 1 | awk '{print $1}'
    else
        # fallback: md5
        echo -n "$raw" | md5sum 2>/dev/null | awk '{print $1}' || echo "unknown-$(hostname)"
    fi
}

get_model() {
    # Linux DMI
    if [[ -f /sys/class/dmi/id/product_name ]]; then
        local m; m=$(cat /sys/class/dmi/id/product_name 2>/dev/null | tr -d '\n')
        if [[ -n "$m" && "$m" != "None" && "$m" != "To Be Filled by O.E.M." ]]; then
            echo "$m"; return
        fi
    fi
    # macOS
    if command -v system_profiler &>/dev/null; then
        system_profiler SPHardwareDataType 2>/dev/null | awk -F': ' '/Model Name/ {print $2; exit}'
        return
    fi
    echo "Linux Machine"
}

get_display_name() {
    if [[ -n "$DISP_NAME" ]]; then echo "$DISP_NAME"; else hostname; fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Sensor Collection — pure bash, /proc, /sys, lm-sensors (optional)
# ─────────────────────────────────────────────────────────────────────────────
get_cpu_usage() {
    # Read /proc/stat twice 0.5s apart for accurate delta
    local s1; s1=$(awk '/^cpu / {print $2+$3+$4+$5+$6+$7+$8, $5}' /proc/stat 2>/dev/null)
    sleep 0.5
    local s2; s2=$(awk '/^cpu / {print $2+$3+$4+$5+$6+$7+$8, $5}' /proc/stat 2>/dev/null)
    local t1 i1 t2 i2
    t1=$(echo "$s1" | awk '{print $1}')
    i1=$(echo "$s1" | awk '{print $2}')
    t2=$(echo "$s2" | awk '{print $1}')
    i2=$(echo "$s2" | awk '{print $2}')
    local dt=$((t2-t1))
    local di=$((i2-i1))
    if [[ $dt -gt 0 ]]; then
        awk "BEGIN {printf \"%.1f\", (1 - $di/$dt) * 100}"
    else
        echo "0.0"
    fi
}

get_cpu_temp() {
    # Method 1: /sys/class/thermal (most Linux laptops)
    local t
    for zone in /sys/class/thermal/thermal_zone*/temp; do
        if [[ -f "$zone" ]]; then
            local raw; raw=$(cat "$zone" 2>/dev/null || true)
            if [[ -n "$raw" && "$raw" -gt 0 ]]; then
                t=$(awk "BEGIN {printf \"%.1f\", $raw/1000}")
                # Sanity check: 20°C to 120°C
                local check; check=$(awk "BEGIN {print ($t >= 20 && $t <= 120)}")
                if [[ "$check" == "1" ]]; then echo "$t"; return; fi
            fi
        fi
    done
    # Method 2: /sys/devices hwmon
    for f in /sys/devices/platform/*/hwmon/hwmon*/temp*_input \
             /sys/class/hwmon/hwmon*/temp*_input; do
        if [[ -f "$f" ]]; then
            local raw; raw=$(cat "$f" 2>/dev/null || true)
            if [[ -n "$raw" && "$raw" -gt 0 ]]; then
                t=$(awk "BEGIN {printf \"%.1f\", $raw/1000}")
                local check; check=$(awk "BEGIN {print ($t >= 20 && $t <= 120)}")
                if [[ "$check" == "1" ]]; then echo "$t"; return; fi
            fi
        fi
    done
    # Method 3: lm-sensors (optional)
    if command -v sensors &>/dev/null; then
        local st; st=$(sensors 2>/dev/null | grep -iE "core 0|cpu|package" | \
                       grep -oP '\+\K[0-9.]+' | head -1)
        if [[ -n "$st" ]]; then echo "$st"; return; fi
    fi
    # macOS
    if command -v osx-cpu-temp &>/dev/null; then
        osx-cpu-temp 2>/dev/null | grep -oP '[0-9.]+'; return
    fi
    echo "$D_CPU_TEMP"
}

get_fan_rpm() {
    # /sys hwmon fan inputs
    for f in /sys/class/hwmon/hwmon*/fan*_input \
             /sys/devices/platform/*/hwmon/hwmon*/fan*_input; do
        if [[ -f "$f" ]]; then
            local v; v=$(cat "$f" 2>/dev/null || true)
            if [[ -n "$v" && "$v" -gt 0 ]]; then echo "$v"; return; fi
        fi
    done
    # lm-sensors
    if command -v sensors &>/dev/null; then
        local fan; fan=$(sensors 2>/dev/null | grep -iE "fan" | \
                         grep -oP '[0-9]+(?= RPM)' | head -1)
        if [[ -n "$fan" ]]; then echo "$fan"; return; fi
    fi
    echo "$D_FAN_RPM"
}

get_cpu_voltage() {
    # lm-sensors JSON (if available)
    if command -v sensors &>/dev/null; then
        local v; v=$(sensors -j 2>/dev/null | \
                     python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    for chip in d.values():
        if not isinstance(chip,dict): continue
        for k,sub in chip.items():
            if not isinstance(sub,dict): continue
            if any(x in k.lower() for x in ('vcore','in0','cpu')):
                for sk,val in sub.items():
                    if 'input' in sk and isinstance(val,(int,float)) and 0.5<=val<=2.0:
                        print(round(val,3)); sys.exit()
except: pass
" 2>/dev/null || true)
        if [[ -n "$v" ]]; then echo "$v"; return; fi
    fi
    echo "$D_CPU_V"
}

get_ram_info() {
    if [[ -f /proc/meminfo ]]; then
        local total free avail
        total=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
        avail=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
        local used=$((total - avail))
        local pct; pct=$(awk "BEGIN {printf \"%.1f\", $used/$total*100}")
        local total_gb; total_gb=$(awk "BEGIN {printf \"%.2f\", $total/1048576}")
        local used_gb;  used_gb=$(awk  "BEGIN {printf \"%.2f\", $used/1048576}")
        echo "$pct $total_gb $used_gb"
    else
        echo "50.0 8.00 4.00"
    fi
}

get_disk_percent() {
    df / 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}' || echo "0"
}

collect_metrics() {
    log "Collecting metrics..."

    local cpu_usage; cpu_usage=$(get_cpu_usage)
    local cpu_temp;  cpu_temp=$(get_cpu_temp)
    local fan_rpm;   fan_rpm=$(get_fan_rpm)
    local cpu_v;     cpu_v=$(get_cpu_voltage)
    local ram_info;  ram_info=$(get_ram_info)
    local ram_pct;   ram_pct=$(echo "$ram_info" | awk '{print $1}')
    local ram_total; ram_total=$(echo "$ram_info" | awk '{print $2}')
    local ram_used;  ram_used=$(echo "$ram_info"  | awk '{print $3}')
    local disk_pct;  disk_pct=$(get_disk_percent)
    local ts;        ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    local hn;        hn=$(hostname)
    local platform="Linux"
    if [[ "$(uname)" == "Darwin" ]]; then platform="Darwin"; fi

    # Build JSON manually (no jq dependency)
    cat <<JSONEOF
{
  "cpu_usage":    $cpu_usage,
  "cpu_temp":     $cpu_temp,
  "fan_rpm":      $fan_rpm,
  "cpu_voltage":  $cpu_v,
  "ram_voltage":  $D_RAM_V,
  "gpu_voltage":  $D_GPU_V,
  "rail_3v3":     $D_RAIL_3V3,
  "rail_5v_mw":   $D_RAIL_5V,
  "ram_percent":  $ram_pct,
  "ram_total_gb": $ram_total,
  "ram_used_gb":  $ram_used,
  "disk_percent": $disk_pct,
  "platform":     "$platform",
  "hostname":     "$hn",
  "timestamp":    "$ts"
}
JSONEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────────────
load_laptop_id() {
    if [[ -f "$STATE_FILE" ]]; then
        local saved_url; saved_url=$(grep -o '"server_url":"[^"]*"' "$STATE_FILE" | cut -d'"' -f4 || true)
        if [[ "$saved_url" == "$SERVER_URL" ]]; then
            local lid; lid=$(grep -o '"laptop_id":"[^"]*"' "$STATE_FILE" | cut -d'"' -f4 || true)
            echo "$lid"
        fi
    fi
}

save_state() {
    local lid="$1" name="$2"
    cat > "$STATE_FILE" <<STATEEOF
{"laptop_id":"$lid","name":"$name","server_url":"$SERVER_URL","registered":"$(date -u '+%Y-%m-%dT%H:%M:%SZ')"}
STATEEOF
}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────
api_post() {
    local endpoint="$1"
    local body="$2"
    local url="${SERVER_URL%/}$endpoint"
    local response http_code

    for attempt in 1 2 3; do
        response=$(curl -s -w "\n%{http_code}" \
            -X POST "$url" \
            -H "Content-Type: application/json" \
            -H "X-Agent-Key: $API_KEY" \
            --connect-timeout 10 \
            --max-time 20 \
            -d "$body" 2>/dev/null || true)

        http_code=$(echo "$response" | tail -1)
        local body_resp; body_resp=$(echo "$response" | head -n -1)

        if [[ "$http_code" == "200" || "$http_code" == "201" ]]; then
            echo "$body_resp"
            return 0
        elif [[ "$http_code" == "401" ]]; then
            log "API key rejected (401). Check DIAG_API_KEY." "ERROR"
            return 1
        else
            log "Attempt $attempt/3 failed (HTTP $http_code)" "WARN"
            [[ $attempt -lt 3 ]] && sleep 10
        fi
    done
    return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# Auto Registration
# ─────────────────────────────────────────────────────────────────────────────
register_or_load() {
    local lid; lid=$(load_laptop_id)
    if [[ -n "$lid" ]]; then
        local saved_name; saved_name=$(grep -o '"name":"[^"]*"' "$STATE_FILE" | cut -d'"' -f4 || true)
        log "Using saved laptop ID: $lid  (name: ${saved_name:-unknown})"
        echo "$lid"
        return
    fi

    log "No saved registration — registering with $SERVER_URL ..."
    local machine_id; machine_id=$(get_machine_id)
    local display;    display=$(get_display_name)
    local model;      model=$(get_model)
    local hn;         hn=$(hostname)
    local platform="Linux"
    [[ "$(uname)" == "Darwin" ]] && platform="Darwin"

    local body; body=$(cat <<BODYEOF
{
  "machine_id": "$machine_id",
  "name":       "$display",
  "model":      "$model",
  "category":   "$CATEGORY",
  "email":      "$ALERT_EMAIL",
  "platform":   "$platform",
  "hostname":   "$hn"
}
BODYEOF
)

    local result; result=$(api_post "/api/agent/register" "$body") || {
        log "Registration failed. Check DIAG_SERVER_URL and DIAG_API_KEY." "ERROR"
        exit 1
    }

    # Parse laptop_id from JSON response (no jq needed)
    local lid_new; lid_new=$(echo "$result" | grep -o '"laptop_id":"[^"]*"' | cut -d'"' -f4)
    local name_new; name_new=$(echo "$result" | grep -o '"name":"[^"]*"' | cut -d'"' -f4)
    local existing; existing=$(echo "$result" | grep -o '"existing":[a-z]*' | cut -d: -f2)

    if [[ -z "$lid_new" ]]; then
        log "Registration response missing laptop_id: $result" "ERROR"
        exit 1
    fi

    local action="Registered as"
    [[ "$existing" == "true" ]] && action="Re-connected to"
    log "$action '$name_new'  (ID: $lid_new)"
    save_state "$lid_new" "$name_new"
    echo "$lid_new"
}

# ─────────────────────────────────────────────────────────────────────────────
# Send report
# ─────────────────────────────────────────────────────────────────────────────
send_report() {
    local laptop_id="$1"
    local metrics; metrics=$(collect_metrics)
    local body; body="{\"laptop_id\":\"$laptop_id\",\"metrics\":$metrics}"

    local result; result=$(api_post "/api/agent/report" "$body") || {
        log "Report failed — will retry next interval." "WARN"
        return 1
    }

    local diagnosis; diagnosis=$(echo "$result" | grep -o '"diagnosis":"[^"]*"' | cut -d'"' -f4)
    local severity;  severity=$(echo  "$result" | grep -o '"severity":"[^"]*"'  | cut -d'"' -f4)
    local conf_raw;  conf_raw=$(echo  "$result" | grep -o '"confidence":[0-9.]*' | cut -d: -f2)
    local conf_pct;  conf_pct=$(awk "BEGIN {printf \"%.1f\", ${conf_raw:-0} * 100}")
    local notified;  notified=$(echo "$result" | grep -o '"notified":[a-z]*' | cut -d: -f2)

    log "Diagnosis: ${diagnosis:-?}  |  Severity: ${severity:-?}  |  Confidence: ${conf_pct}%"
    [[ "$notified" == "true" ]] && log "Alert email sent."
}

# ─────────────────────────────────────────────────────────────────────────────
# systemd install (Linux only)
# ─────────────────────────────────────────────────────────────────────────────
install_service() {
    if [[ "$(uname)" != "Linux" ]]; then
        log "systemd install only supported on Linux" "ERROR"; exit 1
    fi
    if [[ $EUID -ne 0 ]]; then
        log "Run with sudo for systemd install" "ERROR"; exit 1
    fi

    local script_path; script_path="$(realpath "${BASH_SOURCE[0]}")"
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SVCEOF
[Unit]
Description=Laptop Diagnostics Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash $script_path
Restart=always
RestartSec=30
Environment="DIAG_SERVER_URL=$SERVER_URL"
Environment="DIAG_API_KEY=$API_KEY"
Environment="DIAG_INTERVAL=$INTERVAL"
Environment="DIAG_EMAIL=$ALERT_EMAIL"
Environment="DIAG_NAME=$DISP_NAME"
Environment="DIAG_CATEGORY=$CATEGORY"
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME"
    log "Service '$SERVICE_NAME' installed and started."
    log "View logs: journalctl -fu $SERVICE_NAME"
}

uninstall_service() {
    systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    log "Service '$SERVICE_NAME' removed."
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
case "${1:-}" in
    --test)
        echo ""
        echo "=== Sensor Readings (no data sent) ==="
        echo "  cpu_usage    : $(get_cpu_usage) %"
        echo "  cpu_temp     : $(get_cpu_temp) °C"
        echo "  fan_rpm      : $(get_fan_rpm) RPM"
        echo "  cpu_voltage  : $(get_cpu_voltage) V"
        echo "  ram          : $(get_ram_info)"
        echo "  disk         : $(get_disk_percent) %"
        echo "  machine_id   : $(get_machine_id)"
        echo "  hostname     : $(hostname)"
        echo "  model        : $(get_model)"
        echo ""
        exit 0 ;;

    --install)
        [[ -z "$SERVER_URL" ]] && { log "Set DIAG_SERVER_URL" "ERROR"; exit 1; }
        [[ -z "$API_KEY"    ]] && { log "Set DIAG_API_KEY"    "ERROR"; exit 1; }
        install_service
        exit 0 ;;

    --uninstall)
        uninstall_service
        exit 0 ;;

    --once)
        [[ -z "$SERVER_URL" ]] && { log "Set DIAG_SERVER_URL" "ERROR"; exit 1; }
        [[ -z "$API_KEY"    ]] && { log "Set DIAG_API_KEY"    "ERROR"; exit 1; }
        LAPTOP_ID=$(register_or_load)
        send_report "$LAPTOP_ID"
        exit 0 ;;

    "")
        [[ -z "$SERVER_URL" ]] && { log "Set DIAG_SERVER_URL" "ERROR"; exit 1; }
        [[ -z "$API_KEY"    ]] && { log "Set DIAG_API_KEY"    "ERROR"; exit 1; }

        log "======================================================="
        log "  Laptop Diagnostics Agent"
        log "  Server  : $SERVER_URL"
        log "  Hostname: $(hostname)"
        log "  Interval: $((INTERVAL/60)) minutes"
        log "======================================================="

        LAPTOP_ID=$(register_or_load)
        log "Running. Reporting every $((INTERVAL/60)) min. Ctrl+C to stop."
        while true; do
            send_report "$LAPTOP_ID" || LAPTOP_ID=$(register_or_load)
            sleep "$INTERVAL"
        done ;;

    *)
        echo "Usage: $0 [--test|--once|--install|--uninstall]"
        exit 1 ;;
esac
