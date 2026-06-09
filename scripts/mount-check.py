#!/usr/bin/env python3
"""
mount_check.py

Checks whether one or more systemd mount units are active and publishes
the results to Home Assistant via MQTT discovery.

All configuration is read from environment variables (set via the systemd
EnvironmentFile or Environment= directives).

Required environment variables:
    MQTT_BROKER         MQTT broker hostname or IP
    DEVICE_ID           Unique identifier for this host (e.g. echo-G3)
    DEVICE_NAME         Friendly name shown in Home Assistant
    DEVICE_MANUFACTURER Device manufacturer string
    DEVICE_MODEL        Device model string
    MOUNT_UNITS         Comma-separated list of systemd mount units to check
                        e.g. "mymount.mount,backup.mount"

Optional environment variables:
    MQTT_PORT           Default: 1883
    MQTT_USERNAME       Default: (empty, no auth)
    MQTT_PASSWORD       Default: (empty, no auth)
    POLL_INTERVAL       Seconds between checks. Default: 30
    HA_PREFIX           Home Assistant discovery prefix. Default: homeassistant
    HOST_PREFIX         Topic prefix for this host. Default: value of DEVICE_ID

Dependencies:
    pip install paho-mqtt
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        log.error("Required environment variable %s is not set.", name)
        sys.exit(1)
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Logging must be set up before we call _require/_optional with log.error
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

MQTT_BROKER    = _require("MQTT_BROKER")
MQTT_PORT      = int(_optional("MQTT_PORT", "1883"))
MQTT_USERNAME  = _optional("MQTT_USERNAME")
MQTT_PASSWORD  = _optional("MQTT_PASSWORD")
POLL_INTERVAL  = int(_optional("POLL_INTERVAL", "30"))
HA_PREFIX      = _optional("HA_PREFIX", "homeassistant")

DEVICE_ID      = _require("DEVICE_ID")
HOST_PREFIX    = _optional("HOST_PREFIX", DEVICE_ID)

DEVICE = {
    "identifiers": [DEVICE_ID],
    "name":         _require("DEVICE_NAME"),
    "manufacturer": _require("DEVICE_MANUFACTURER"),
    "model":        _require("DEVICE_MODEL"),
}

# Build the list of mount descriptors from the comma-separated MOUNT_UNITS var
_raw_units = _require("MOUNT_UNITS")
MOUNTS = []
for unit in [u.strip() for u in _raw_units.split(",") if u.strip()]:
    # Strip the .mount suffix to build clean topic/id slugs
    slug = unit.removesuffix(".mount").replace("-", "_").replace(".", "_")
    MOUNTS.append({
        "unit":         unit,
        "config_topic": f"{HA_PREFIX}/binary_sensor/{HOST_PREFIX}/{slug}/config",
        "state_topic":  f"{HOST_PREFIX}/{slug}/state",
        "unique_id":    f"{DEVICE_ID}_{slug}_status",
        "name":         f"{DEVICE['name']} {slug}",
    })

# ---------------------------------------------------------------------------
# Systemd helpers
# ---------------------------------------------------------------------------

def is_unit_active(unit: str) -> bool:
    """Return True if the given systemd unit is in the 'active' state."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.warning("Timeout while checking unit: %s", unit)
        return False
    except FileNotFoundError:
        log.error("systemctl not found – are you running on a systemd host?")
        return False


# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        log.info("Connected to MQTT broker %s:%d", MQTT_BROKER, MQTT_PORT)
        publish_discovery(client)
    else:
        log.error("MQTT connection failed with return code %d", rc)


def publish_discovery(client: mqtt.Client):
    """Publish Home Assistant MQTT discovery config for every mount."""
    for mount in MOUNTS:
        payload = {
            "name":         mount["name"],
            "state_topic":  mount["state_topic"],
            "payload_on":   "ON",
            "payload_off":  "OFF",
            "unique_id":    mount["unique_id"],
            "device_class": "connectivity",
            "device":       DEVICE,
        }
        client.publish(mount["config_topic"], json.dumps(payload), retain=True)
        log.info("Published discovery config for '%s'", mount["name"])


def publish_states(client: mqtt.Client):
    """Poll every mount unit and publish its current ON/OFF state."""
    for mount in MOUNTS:
        active = is_unit_active(mount["unit"])
        state  = "ON" if active else "OFF"
        client.publish(mount["state_topic"], state, retain=True)
        log.info("%-35s -> %s", mount["unit"], state)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("Signal %d received – shutting down.", signum)
    _running = False


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    log.info("Starting mount_check for units: %s", _raw_units)

    client = mqtt.Client(client_id=f"mount-check-{DEVICE_ID}")

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect

    log.info("Connecting to %s:%d …", MQTT_BROKER, MQTT_PORT)
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()

    time.sleep(2)   # let the connection establish

    while _running:
        publish_states(client)
        for _ in range(POLL_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    log.info("Disconnecting.")
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()