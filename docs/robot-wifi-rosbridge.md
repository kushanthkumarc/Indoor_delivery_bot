# Local robot Wi-Fi ROSBridge connection

The Android mapping console can ask the backend to open a ROSBridge WebSocket
to a robot on the backend host's local Wi-Fi. The phone does not connect
directly to the robot for this feature.

## Requirements

- The backend host and robot must be on a network that can route between them.
- Use an administrator account in the Android app.
- In the backend's local, untracked `.env`, set `ROSBRIDGE_ENABLED=true`, then
  restart the backend. Cloud and production deployments must keep this setting
  false; production startup validation rejects ROS integration.
- The backend accepts literal RFC1918 IPv4 addresses, plus the already
  configured local camera host `192.0.0.4`, and ports from 1 to 65535.
  Hostnames and public addresses are rejected.
- ROSBridge is an unencrypted `ws://` connection. Use it only on a trusted,
  isolated robot LAN.

## Connect

1. Open **Console**; the screen switches to landscape while it is open.
2. Choose **Robot Wi-Fi**.
3. Enter the robot's private IPv4 address and ROSBridge WebSocket port (often
   `9090`) and choose **Connect**.
4. Choose **Refresh** to check whether the backend reports the WebSocket as
   connected, connecting, retrying, or disconnected.

The selected endpoint is held in backend memory and is not persisted across a
backend restart. `CONNECTED` confirms only that the ROSBridge WebSocket is
connected; it does not confirm robot readiness, localization, safe motion, or
physical command execution. This screen does not add motor controls.
