#!/usr/bin/env python3
"""
webhook_server.py — TradingView webhook receiver for both engines.

Receives POST alerts from TradingView and routes them to crypto (hl_engine)
or equities (equities_engine) based on symbol.

Alert message format:
  {"symbol": "BTC", "action": "LONG"}
  {"symbol": "SPY", "action": "SHORT"}

Usage:
  python webhook_server.py --port 8765

Then configure TradingView alerts with webhook URL:
  http://YOUR_IP:8765/alert
"""

import argparse
import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger("webhook")

from util.platform_compat import control_socket_path, supports_unix_sockets

# Crypto symbols route to hl_engine
CRYPTO_SYMBOLS = {"BTC", "ETH"}
CRYPTO_SOCKET = control_socket_path("hl_engine")
EQUITIES_SOCKET = control_socket_path("equities_engine")


def send_control_command(sock_path: str, cmd: str, params: dict = None) -> dict:
    """Send a command to an engine via its Unix socket and get response."""
    if not supports_unix_sockets():
        return {"ok": False, "error": "control_plane_unavailable_on_windows"}
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)

        req = {"cmd": cmd}
        if params:
            req["params"] = params

        msg = json.dumps(req) + "\n"
        sock.sendall(msg.encode())

        resp_raw = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp_raw += chunk
            if b"\n" in resp_raw:
                break

        sock.close()
        return json.loads(resp_raw.strip())
    except Exception as e:
        log.error(f"socket_error: {sock_path}: {e}")
        return {"ok": False, "error": str(e)}


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/alert":
            self.send_error(404)
            return

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len == 0:
                self.send_error(400, "Empty body")
                return

            body = self.rfile.read(content_len)
            alert = json.loads(body.decode())

            symbol = str(alert.get("symbol", "")).upper()
            action = str(alert.get("action", "")).upper()

            if not symbol or not action:
                self.send_error(400, "Missing symbol or action")
                return

            # Route to appropriate engine
            sock_path = CRYPTO_SOCKET if symbol in CRYPTO_SYMBOLS else EQUITIES_SOCKET

            # Send manual trade command (future Phase 2)
            # For now, just log and acknowledge
            log.info(f"alert: {symbol} {action} → {sock_path}")

            resp = {
                "ok": True,
                "symbol": symbol,
                "action": action,
                "routed_to": "crypto" if symbol in CRYPTO_SYMBOLS else "equities",
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())

        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
        except Exception as e:
            log.exception("handler_error")
            self.send_error(500)

    def log_message(self, format, *args):
        log.info(format % args)


def main():
    parser = argparse.ArgumentParser(description="TradingView webhook receiver")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    server = HTTPServer((args.host, args.port), WebhookHandler)
    log.info(f"webhook_server_start: {args.host}:{args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("webhook_server_stop")
        server.shutdown()


if __name__ == "__main__":
    main()
