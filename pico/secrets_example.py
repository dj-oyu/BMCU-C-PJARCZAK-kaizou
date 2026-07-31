# Copy this file to secrets.py on the Pico.  secrets.py is intentionally ignored
# by Git because it contains network credentials.
WIFI_SSID = ""
WIFI_PASSWORD = ""

# Unique on this LAN: use lowercase letters, digits, and hyphens.  Browse to
# http://<this-name>.local/ after Wi-Fi connects.
MDNS_HOSTNAME = "bmcu-monitor-a"

# The BMB1 device key belongs in config.py or another separately provisioned
# module. It is never served by the local diagnostic UI.
