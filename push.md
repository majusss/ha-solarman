# ☁️ Logger data push (cloud listener)

Besides polling the logger on port `8899`, the integration can receive the data the logger periodically pushes to the Solarman cloud (Solarman V5 `DATA` frames, by default every 5 minutes). Both sources are merged into a single device:

| Source | Interval | What it gives |
|---|---|---|
| Polling (`8899`) | seconds | Everything defined in the profile, writable settings |
| Push (`10000`) | ~5 min | Values with a different resolution or not available through Modbus (profile dependent, e.g. Sofar string inverters: power in 1 W, total production in 0.1 kWh, temperature in 0.1 °C, inverter serial number and firmware) |

Push entities stay available for 15 minutes after the last received frame, independently of the polling state. The `Last Push` diagnostic sensor shows when the last frame arrived.

## ⚙️ Integration options

Settings > Devices & services > Solarman > Configure > **Logger data push**

| Option | Description |
|---|---|
| Mode | `Disabled`, `Local` (Home Assistant acknowledges frames itself, nothing is sent to the cloud) or `Proxy` (frames are forwarded to the cloud and the cloud replies are passed back to the logger) |
| Listening port | TCP port Home Assistant listens on (default `10000`) |
| Upstream cloud server | Proxy mode only, e.g. `access3.solarmanpv.com:10000` (the original value of *Server A* in the logger) |

If the upstream server is unreachable in proxy mode, frames are acknowledged locally so the logger keeps sending.

## 🔀 Sending the data to Home Assistant

### Option A: Logger web UI, Server B (recommended)

Most stick loggers (LSW-3, …) can send data to two servers. Keep *Server A* pointed at the cloud and point *Server B* at Home Assistant, then use the **Local** mode.

1. Open `http://<logger-ip>/config_hide.html` (default credentials `admin` / `admin`)
2. In *Server B setting* enter the Home Assistant IP address, port `10000` and protocol `TCP`, clear the domain field and save
3. Restart the logger

The cloud keeps working (it receives data through *Server A*) and nothing has to be changed on the router.

### Option B: DNAT on the router (OpenWrt)

Redirect the logger's connection to the cloud to Home Assistant. Use **Proxy** mode to keep the cloud working, or **Local** mode to cut it off.

```sh
LOGGER=192.168.1.219
HA=192.168.1.10

uci add firewall redirect
uci set firewall.@redirect[-1].name='solarman-to-ha'
uci set firewall.@redirect[-1].target='DNAT'
uci set firewall.@redirect[-1].proto='tcp'
uci set firewall.@redirect[-1].src='lan'
uci set firewall.@redirect[-1].src_ip="$LOGGER"
uci set firewall.@redirect[-1].src_dport='10000'
uci set firewall.@redirect[-1].dest='lan'
uci set firewall.@redirect[-1].dest_ip="$HA"
uci set firewall.@redirect[-1].dest_port='10000'
uci set firewall.@redirect[-1].reflection='0'

# The logger and Home Assistant are in the same subnet, so the replies must go back through the router
uci add firewall nat
uci set firewall.@nat[-1].name='solarman-to-ha-snat'
uci set firewall.@nat[-1].target='MASQUERADE'
uci set firewall.@nat[-1].proto='tcp'
uci set firewall.@nat[-1].src='lan'
uci set firewall.@nat[-1].src_ip="$LOGGER"
uci set firewall.@nat[-1].dest_ip="$HA"
uci set firewall.@nat[-1].dest_port='10000'

uci commit firewall
/etc/init.d/firewall restart
```

The rule matches only connections coming from the logger, so the proxy connection made by Home Assistant to the cloud is not redirected. Because of the masquerade, Home Assistant sees the router as the source address and identifies the logger by its serial number (taken from the frame).

## 🐳 Home Assistant in Docker

The listening port has to be reachable from the logger:

```yaml
services:
  homeassistant:
    network_mode: host # recommended (also needed for the logger autodiscovery)
    # or, with bridge networking:
    # ports:
    #   - "10000:10000"
```

## 🧩 Profiles

The push frame layout is defined in the profile under the `push` key, and the entities use the frame control code as their `code`:

```yaml
push:
  - code: 0x42                 # Solarman V5 control code of the frame (0x42 = DATA)
    length: 155                # optional, payload length filter
    big_endian: [[32, 46]]     # byte ranges (start, end) stored big-endian, e.g. ASCII strings

parameters:
  - group: Push
    code: 0x42
    items:
      - name: "Push Power"
        rule: 1
        registers: [36]        # register N = frame bytes 2N..2N+1 (counted from the 0xA5 start byte)
```

Registers are little-endian by default. The last received raw frames are included in the diagnostics download (`push_frames`) to help mapping the layout of other devices.
