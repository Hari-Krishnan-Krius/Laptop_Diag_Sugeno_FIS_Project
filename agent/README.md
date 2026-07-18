# Laptop Diagnostics Agent

## Zero dependency. No Python. No installation.

### Windows
Uses built-in PowerShell + native WMI. Nothing to install.

### Linux / macOS
Uses built-in bash + curl. Nothing to install.

---

## Files in this folder

| File | Purpose |
|---|---|
| `LaptopDiagAgent.ps1` | Main Windows agent (PowerShell) |
| `laptop_agent.sh` | Main Linux/macOS agent (bash) |
| `START_AGENT.bat` | Double-click launcher for Windows |
| `INSTALL_AUTOSTART.bat` | Installs as auto-start Task Scheduler job |
| `laptop_agent.service` | systemd service file for Linux auto-start |
| `laptop_agent.py` | Optional Python agent (if Python is available) |

---

## Windows — Quick Start (2 minutes)

**Step 1:** Edit `START_AGENT.bat` — change these two lines:
```
set DIAG_SERVER_URL=http://YOUR_SERVER_IP:5000
set DIAG_API_KEY=YOUR_AGENT_API_KEY_HERE
```

**Step 2:** Double-click `START_AGENT.bat`

That's it. The laptop appears in the dashboard within seconds.

---

## Windows — Run silently at startup (no window)

**Step 1:** Edit `INSTALL_AUTOSTART.bat` — same two lines as above

**Step 2:** Right-click `INSTALL_AUTOSTART.bat` → **Run as administrator**

The agent installs as a Windows Scheduled Task and starts automatically on every boot, silently in the background. No window, no user interaction needed.

---

## Linux / macOS — Quick Start

```bash
# Step 1: set env vars
export DIAG_SERVER_URL=http://YOUR_SERVER_IP:5000
export DIAG_API_KEY=YOUR_AGENT_API_KEY_HERE

# Step 2: test sensors
chmod +x laptop_agent.sh
./laptop_agent.sh --test

# Step 3: run
./laptop_agent.sh
```

## Linux — Auto-start at boot

```bash
export DIAG_SERVER_URL=http://YOUR_SERVER_IP:5000
export DIAG_API_KEY=YOUR_AGENT_API_KEY_HERE
sudo ./laptop_agent.sh --install

# View logs
journalctl -fu laptop-diag-agent
```

---

## What the DIAG_API_KEY is

It is the `AGENT_API_KEY` value from your server's `.env` file.
Every agent must use the same key to authenticate with the server.
Without it, any machine on the network could register itself.

---

## Sensor coverage

| Sensor | Windows (no tools) | Windows (with LHM) | Linux |
|---|---|---|---|
| CPU Usage | ✅ Always | ✅ | ✅ Always |
| CPU Temperature | ✅ Most laptops | ✅ | ✅ Most laptops |
| Fan RPM | ⚠️ Default | ✅ | ✅ If hwmon exposed |
| CPU Voltage | ⚠️ Default | ✅ | ✅ If lm-sensors |
| RAM / GPU Voltage | ⚠️ Default | ✅ | ⚠️ Default |
| +3.3V / +5V Rail | ⚠️ Default | ✅ | ⚠️ Default |
| RAM Usage | ✅ Always | ✅ | ✅ Always |
| Disk Usage | ✅ Always | ✅ | ✅ Always |

**LHM = Libre Hardware Monitor running as Administrator**
(optional — the agent works without it, just with fewer voltage readings)

---

## Troubleshooting

**Laptop not appearing in dashboard**
- Check `laptop_agent.log` in the same folder
- Verify `DIAG_SERVER_URL` is correct (can you ping the server?)
- Verify `DIAG_API_KEY` matches `AGENT_API_KEY` in server `.env`

**Voltages / fan showing defaults**
- Windows: run Libre Hardware Monitor as Administrator before the agent
- Linux: `sudo apt install lm-sensors && sudo sensors-detect && sensors`

**Want to remove this laptop from the dashboard**
- Click 🗑️ on the laptop card in the Fleet Overview

**Want to re-register with a new name**
- Delete `.agent_state.json` in this folder
- Set `DIAG_NAME=New Name Here` and restart the agent
