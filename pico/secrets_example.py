# Copy this file to secrets.py on the Pico.  secrets.py is intentionally ignored
# by Git because it contains network credentials.
WIFI_SSID = ""
WIFI_PASSWORD = ""

# Unique on this LAN: use lowercase letters, digits, and hyphens.  Browse to
# http://<this-name>.local/ after Wi-Fi connects.
MDNS_HOSTNAME = "bmcu-monitor-a"

# Optional bootstrap for Bambuddy. These values can later be replaced from
# http://<this-name>.local/settings without exposing the token back to a client.
# ws:// is allowed only on an explicitly trusted LAN.
BAMBUDDY_ENABLED = False
BAMBUDDY_WS_URL = "ws://bambuddy.local:8000/api/v1/bmcu-link/ws"
BAMBUDDY_TOKEN = ""
