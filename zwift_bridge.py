"""
Zwift Click / Cog v2 — TCP Bridge
==================================
Se connecte au périphérique Zwift Click v2 (ou Cog v2) via Bluetooth LE (bleak)
et expose les événements boutons sur un serveur TCP local (localhost:7847).

Chaque événement est une ligne JSON terminée par \\n :

  {"type": "scanning"}
  {"type": "connected", "name": "Zwift Click v2", "address": "XX:XX:XX:XX:XX:XX"}
  {"type": "button", "button": "plus",  "state": "down"}
  {"type": "button", "button": "plus",  "state": "up"}
  {"type": "disconnected"}

L'application Electron (ou tout autre client) se connecte sur localhost:7847
et reçoit ces événements en temps réel.

Installation :
  pip install bleak

Utilisation :
  python zwift_bridge.py          # port par défaut 7847
  python zwift_bridge.py --port 9000
"""

import asyncio
import json
import sys
import argparse
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Bibliothèque manquante. Exécutez : pip install bleak")
    sys.exit(1)

# ─── Protocole BLE ────────────────────────────────────────────────────────────

NOTIFY_CHAR = "00000002-19ca-4651-86e5-fa29dcdd09d1"
WRITE_CHAR  = "00000003-19ca-4651-86e5-fa29dcdd09d1"
HANDSHAKE   = ["526964654f6e0203", "000800", "000810"]
KEEPALIVE   = "000810"

DEVICE_NAME_FILTER = "Zwift"   # sous-chaîne présente dans "Zwift Click v2" / "Zwift Cog v2"

BIT_NAMES = {
    (0, 0): "left",
    (0, 1): "up",
    (0, 2): "right",
    (0, 3): "down",
    (0, 4): "a",
    (0, 5): "b",
    (0, 6): "y",
    (1, 0): "z",
    (1, 1): "minus",
    (1, 5): "plus",
}


def decode_buttons(data: bytearray) -> set:
    """Extrait les boutons enfoncés depuis un paquet de notification de 7 octets."""
    hx = data.hex().upper()
    if not hx.startswith("2308") or len(hx) != 14:
        return set()
    mask = bytes.fromhex(hx[4:12])   # octets [2..5]
    pressed = set()
    for i, byte in enumerate(mask):
        for bit in range(8):
            if not (byte & (1 << bit)):
                name = BIT_NAMES.get((i, bit))
                if name:
                    pressed.add(name)
    return pressed


# ─── Gestionnaire de clients TCP ─────────────────────────────────────────────

class BridgeServer:
    """Maintient la liste des clients TCP connectés et diffuse des messages JSON."""

    def __init__(self):
        self._clients: set[asyncio.StreamWriter] = set()
        self._lock = asyncio.Lock()
        # État courant pour informer les nouveaux clients
        self._ble_connected = False
        self._ble_name = None
        self._ble_address = None

    async def _client_handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        print(f"[Bridge] Client connecté : {peer}")
        async with self._lock:
            self._clients.add(writer)

        # Informe le nouveau client de l'état BLE actuel
        if self._ble_connected:
            await self._send_one(writer, {
                "type": "connected",
                "name": self._ble_name,
                "address": self._ble_address,
            })
        else:
            await self._send_one(writer, {"type": "scanning"})

        try:
            # Maintient la connexion ouverte ; ignore les données entrantes
            while True:
                data = await reader.read(256)
                if not data:
                    break
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            async with self._lock:
                self._clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            print(f"[Bridge] Client déconnecté : {peer}")

    async def _send_one(self, writer: asyncio.StreamWriter, msg: dict):
        try:
            writer.write((json.dumps(msg) + "\n").encode())
            await writer.drain()
        except Exception:
            pass

    async def broadcast(self, msg: dict):
        """Diffuse msg à tous les clients connectés."""
        line = (json.dumps(msg) + "\n").encode()
        async with self._lock:
            dead = set()
            for w in self._clients:
                try:
                    w.write(line)
                    await w.drain()
                except Exception:
                    dead.add(w)
            self._clients -= dead

    async def on_ble_connected(self, name: str, address: str):
        self._ble_connected = True
        self._ble_name = name
        self._ble_address = address
        await self.broadcast({"type": "connected", "name": name, "address": address})

    async def on_ble_disconnected(self):
        self._ble_connected = False
        self._ble_name = None
        self._ble_address = None
        await self.broadcast({"type": "disconnected"})

    async def on_button(self, button: str, state: str):
        await self.broadcast({"type": "button", "button": button, "state": state})

    async def on_scanning(self):
        await self.broadcast({"type": "scanning"})

    async def start(self, host: str, port: int):
        server = await asyncio.start_server(self._client_handler, host, port)
        addr = server.sockets[0].getsockname()
        print(f"[Bridge] Serveur TCP en écoute sur {addr[0]}:{addr[1]}")
        return server


# ─── Boucle BLE ──────────────────────────────────────────────────────────────

class ZwiftBleLoop:
    def __init__(self, server: BridgeServer):
        self.server = server
        self._pressed: set = set()

    def _notification_handler(self, _sender, data: bytearray):
        now_pressed = decode_buttons(data)

        # Boutons nouvellement enfoncés
        for btn in now_pressed - self._pressed:
            asyncio.ensure_future(self.server.on_button(btn, "down"))
            print(f"  {btn:<6} DOWN")

        # Boutons relâchés
        for btn in self._pressed - now_pressed:
            asyncio.ensure_future(self.server.on_button(btn, "up"))
            print(f"  {btn:<6} UP")

        self._pressed = now_pressed

    async def _connect_once(self) -> bool:
        print("[BLE] Recherche du Zwift Click / Cog... (appuyez sur un bouton pour le réveiller)")
        await self.server.on_scanning()

        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: d.name and DEVICE_NAME_FILTER in d.name,
            timeout=15.0,
        )
        if not device:
            return False

        print(f"[BLE] Trouvé : {device.name} [{device.address}] — connexion...")

        try:
            async with BleakClient(device.address) as client:
                # 1. Subscribe d'abord (comme dans le protocole de référence)
                await client.start_notify(NOTIFY_CHAR, self._notification_handler)

                # 2. Handshake en 3 étapes, 100 ms entre chaque
                for msg in HANDSHAKE:
                    await client.write_gatt_char(
                        WRITE_CHAR, bytearray.fromhex(msg), response=False
                    )
                    await asyncio.sleep(0.1)

                await self.server.on_ble_connected(device.name, device.address)
                print(f"[BLE] Connecté : {device.name}")
                print("       Ctrl+C pour quitter.\n")

                # 3. Keepalive toutes les 5 s
                while client.is_connected:
                    await client.write_gatt_char(
                        WRITE_CHAR, bytearray.fromhex(KEEPALIVE), response=False
                    )
                    await asyncio.sleep(5)

        except Exception as e:
            print(f"[BLE] Erreur : {e}")
            return False

        return True

    async def run(self):
        """Boucle principale : scan → connexion → keepalive → reconnexion."""
        while True:
            try:
                self._pressed = set()
                found = await self._connect_once()
                if not found:
                    print("[BLE] Appareil introuvable. Nouvelle tentative dans 5 s...")
                else:
                    print("[BLE] Déconnecté. Reconnexion dans 2 s...")
                    await self.server.on_ble_disconnected()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[BLE] Exception inattendue : {e}. Nouvelle tentative dans 5 s...")
                await self.server.on_ble_disconnected()

            await asyncio.sleep(5 if not found else 2)


# ─── Point d'entrée ──────────────────────────────────────────────────────────

async def amain(host: str, port: int):
    server = BridgeServer()
    tcp_server = await server.start(host, port)

    ble_loop = ZwiftBleLoop(server)

    async with tcp_server:
        await asyncio.gather(
            tcp_server.serve_forever(),
            ble_loop.run(),
        )


def main():
    parser = argparse.ArgumentParser(description="Zwift Click/Cog v2 — TCP Bridge")
    parser.add_argument("--host", default="127.0.0.1", help="Adresse d'écoute (défaut: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7847, help="Port TCP (défaut: 7847)")
    args = parser.parse_args()

    print("=== Zwift Click / Cog v2 — TCP Bridge ===")
    print(f"    Connectez Incyclist à tcp://{args.host}:{args.port}")
    print("    Ctrl+C pour quitter.\n")

    try:
        asyncio.run(amain(args.host, args.port))
    except KeyboardInterrupt:
        print("\n[Bridge] Arrêt.")


if __name__ == "__main__":
    main()
