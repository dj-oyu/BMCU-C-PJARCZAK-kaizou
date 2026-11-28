# Copy this file to secrets.py on the Pico.  secrets.py is intentionally ignored
# by Git because it contains network credentials.
WIFI_SSID = ""
WIFI_PASSWORD = ""

# Unique on this LAN: use lowercase letters, digits, and hyphens.  Browse to
# http://<this-name>.local/ after Wi-Fi connects.
MDNS_HOSTNAME = "bmcu-monitor-a"

# Shared token for POST/GET /api/control/long-reply (at least 16 characters).
# Leave unset to keep that endpoint off. Anyone on the LAN who knows it can
# change how the BMCU answers the printer, so treat it like a password.
# BMCU_CONTROL_TOKEN = ""

# The BMB1 device key belongs in config.py or another separately provisioned
# module. It is never served by the local diagnostic UI.
