"""
Zwift Click v2 -> Keyboard Mapper
Connects directly to a Zwift Click v2 over Bluetooth LE and turns button
presses into genuine keyboard keystrokes via Linux uinput (evdev).

Run:  python zwift_click_mapper.py
Stop: Ctrl+C in the terminal window
"""

import asyncio
import json
import platform
import sys
import time
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Bibliothèque manquante. Exécutez : pip install bleak")
    sys.exit(1)

# --- Backend d'injection de touches ---
IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    try:
        import pydirectinput
    except ImportError:
        print("Bibliothèque manquante. Exécutez : pip install pydirectinput")
        sys.exit(1)
    pydirectinput.PAUSE = 0

    def key_down_raw(key):
        pydirectinput.keyDown(key)

    def key_up_raw(key):
        pydirectinput.keyUp(key)

else:
    # --- Backend Linux : vrai clavier virtuel via uinput / evdev ---
    try:
        import evdev
        from evdev import UInput, ecodes
    except ImportError:
        print("Bibliothèque manquante sous Linux. Exécutez : pip install evdev")
        sys.exit(1)

    try:
        # Création du périphérique clavier virtuel au niveau noyau
        _ui = UInput(name="Zwift Click Virtual Keyboard")
        print("Clavier virtuel '/dev/uinput' initialisé avec succès.")
    except PermissionError:
        print(
            "ERREUR : Permission refusée sur /dev/uinput.\n"
            "Exécutez le script avec 'sudo' ou ajoutez une règle udev pour votre utilisateur."
        )
        sys.exit(1)

    def _to_ecodes_key(name: str):
        """Convertit le nom de configuration (ex: 'k', 'up', 'left') en code evdev (KEY_K, KEY_UP)."""
        name_clean = name.strip().upper()
        # 1. Essaie avec le préfixe standard KEY_
        code = getattr(ecodes, f"KEY_{name_clean}", None)
        if code is not None:
            return code
        # 2. Si l'utilisateur a déjà écrit 'KEY_UP' directement
        code = getattr(ecodes, name_clean, None)
        if code is not None:
            return code

        print(f"Code touche inconnu dans la config : '{name}'")
        return None

    def key_down_raw(key):
        code = _to_ecodes_key(key)
        if code is not None:
            _ui.write(ecodes.EV_KEY, code, 1)  # 1 = Appui
            _ui.syn()

    def key_up_raw(key):
        code = _to_ecodes_key(key)
        if code is not None:
            _ui.write(ecodes.EV_KEY, code, 0)  # 0 = Relâchement
            _ui.syn()


# ---------------------------------------------------------------- config ---
CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS = {
    "device_name_filter": "Zwift Click",
    "bindings": {
        "plus": "k",
        "minus": "i",
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
    },
    "hold_to_repeat": ["plus", "minus"],
    "repeat_initial_delay_ms": 350,
    "repeat_interval_ms": 150,
    "debounce_ms": 60,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            for k, v in user.items():
                if k == "bindings":
                    cfg["bindings"] = {**DEFAULTS["bindings"], **v}
                else:
                    cfg[k] = v
        except json.JSONDecodeError as e:
            print(f"Erreur dans config.json ({e}). Valeurs par défaut utilisées.")
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
        print(f"Fichier config.json créé : {CONFIG_PATH}")
    return cfg


# ------------------------------------------------------------- protocol ---
NOTIFY_CHAR = "00000002-19ca-4651-86e5-fa29dcdd09d1"
WRITE_CHAR = "00000003-19ca-4651-86e5-fa29dcdd09d1"
HANDSHAKE = ["526964654f6e0203", "000800", "000810"]
KEEPALIVE = "000810"

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


def decode_buttons(hx):
    """Extrait l'ensemble des boutons enfoncés d'après le paquet hexadécimal."""
    mask = bytes.fromhex(hx[4:12])
    pressed = set()
    for i, byte in enumerate(mask):
        for bit in range(8):
            if not byte & (1 << bit):
                pressed.add(BIT_NAMES.get((i, bit), f"btn{i}_{bit}"))
    return pressed


class ClickMapper:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bindings = cfg["bindings"]
        self.repeat_buttons = set(cfg.get("hold_to_repeat", []))
        self.debounce = cfg.get("debounce_ms", 60) / 1000
        self.initial_delay = cfg.get("repeat_initial_delay_ms", 350) / 1000
        self.interval = cfg.get("repeat_interval_ms", 150) / 1000
        self.last_press = {}
        self.pressed = set()

    def key_down(self, button):
        key = self.bindings.get(button)
        if not key:
            return
        now = time.time()
        if now - self.last_press.get(button, 0) < self.debounce:
            return
        self.last_press[button] = now
        key_down_raw(key)
        print(f"  {button:<6} -> {key} DOWN")

    def key_up(self, button):
        key = self.bindings.get(button)
        if not key:
            return
        key_up_raw(key)
        print(f"  {button:<6} -> {key} UP")

    def handle(self, _sender, data: bytearray):
        hx = data.hex().upper()
        if not hx.startswith("2308") or len(hx) != 14:
            return
        now_pressed = decode_buttons(hx)
        for button in now_pressed - self.pressed:
            if button not in self.bindings:
                print(f"  Bouton non configuré : {button} (ajoutez-le dans config.json)")
            self.key_down(button)
        for button in self.pressed - now_pressed:
            self.key_up(button)
        self.pressed = now_pressed


async def connect_once(cfg, mapper):
    print("Recherche du Zwift Click... (appuyez sur un bouton pour le réveiller)")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: d.name and cfg["device_name_filter"] in d.name,
        timeout=15.0,
    )
    if not device:
        return False

    print(f"Trouvé : {device.name} [{device.address}] — connexion...")
    async with BleakClient(device.address) as client:
        await client.start_notify(NOTIFY_CHAR, mapper.handle)
        for msg in HANDSHAKE:
            await client.write_gatt_char(
                WRITE_CHAR, bytearray.fromhex(msg), response=False
            )
            await asyncio.sleep(0.1)

        print("\n=== Connecté. Touches associées ===")
        for btn, key in cfg["bindings"].items():
            tag = " (répétition activée)" if btn in mapper.repeat_buttons else ""
            print(f"  {btn:<6} -> {key}{tag}")
        print("Ctrl+C pour quitter.\n")

        while client.is_connected:
            await client.write_gatt_char(
                WRITE_CHAR, bytearray.fromhex(KEEPALIVE), response=False
            )
            await asyncio.sleep(5)
    return True


async def main():
    cfg = load_config()
    mapper = ClickMapper(cfg)
    while True:
        try:
            found = await connect_once(cfg, mapper)
            if not found:
                print("Appareil introuvable. Nouvelle tentative dans 5s...")
                await asyncio.sleep(5)
            else:
                print("Déconnecté. Reconnexion...")
                await asyncio.sleep(2)
        except Exception as e:
            print(f"Erreur de connexion : {e}. Nouvelle tentative dans 5s...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nArrêt du programme.")
