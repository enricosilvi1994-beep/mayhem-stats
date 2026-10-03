#!/usr/bin/env python3
"""
Metasrc Bridge — collega la partita di League of Legends in corso sul PC all'app sul telefono.

Cosa fa:
  1. legge la Live Client Data API ufficiale di Riot (https://127.0.0.1:2999), disponibile sul PC
     mentre sei in partita (dalla schermata di caricamento in poi), Pandemonio compreso;
  2. ricava il tuo campione, gli alleati e i nemici;
  3. li espone sulla rete di casa:  http://<ip-del-pc>:8765/game  (JSON)
  4. risponde alla ricerca automatica dell'app (UDP 8766), così non serve scrivere l'IP.

Uso:    python metasrc_bridge.py
Solo libreria standard Python 3.8+. Nessun dato lascia la tua rete di casa.
Al primo avvio Windows può chiedere il permesso del firewall: consenti su "Reti private".
"""
from __future__ import annotations

import json
import socket
import ssl
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIVE_CLIENT_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"
HTTP_PORT = 8765
DISCOVERY_PORT = 8766
DISCOVERY_REQUEST = b"METASRC_DISCOVER"
DISCOVERY_REPLY = b"METASRC_BRIDGE " + str(HTTP_PORT).encode()
POLL_SECONDS = 2.0
RAW_PREFIX = "game_character_displayname_"


# --------------------------------------------------------------------------- logica (testabile)

def champion_ref(player: dict) -> dict:
    """{'internal': 'MissFortune', 'display': 'Miss Fortune'} — l'app li abbina agli ID Data Dragon."""
    raw = str(player.get("rawChampionName", ""))
    internal = raw[len(RAW_PREFIX):] if raw.startswith(RAW_PREFIX) else raw
    return {"internal": internal, "display": player.get("championName", "")}


def _same_player(p: dict, active: dict) -> bool:
    keys = ("riotId", "summonerName", "riotIdGameName")
    a = {str(active.get(k, "")).lower() for k in keys if active.get(k)}
    b = {str(p.get(k, "")).lower() for k in keys if p.get(k)}
    return bool(a & b)


def summarize(allgamedata: dict | None) -> dict:
    """Riduce la risposta della Live Client Data API a ciò che serve all'app."""
    if not allgamedata or "allPlayers" not in allgamedata:
        return {"inGame": False}
    players = allgamedata.get("allPlayers", [])
    active = allgamedata.get("activePlayer", {}) or {}
    me = next((p for p in players if _same_player(p, active)), None)
    if me is None:
        return {"inGame": True, "me": None, "allies": [], "enemies": [], "mode": allgamedata.get("gameData", {}).get("gameMode")}
    my_team = me.get("team")
    return {
        "inGame": True,
        "mode": allgamedata.get("gameData", {}).get("gameMode"),
        "gameTime": allgamedata.get("gameData", {}).get("gameTime"),
        "me": champion_ref(me),
        # inventario e oro: l'app calcola il prossimo acquisto della build
        "myItems": [str(i.get("itemID")) for i in me.get("items", []) or [] if i.get("itemID")],
        "gold": round(float(active.get("currentGold", 0) or 0), 1),
        "level": active.get("level") or me.get("level"),
        "allies": [champion_ref(p) for p in players if p is not me and p.get("team") == my_team],
        "enemies": [champion_ref(p) for p in players if p.get("team") != my_team],
    }


# --------------------------------------------------------------------------- rete

class State:
    lock = threading.Lock()
    game: dict = {"inGame": False}


def read_live_client(timeout: float = 1.5) -> dict | None:
    # La Live Client Data API usa un certificato self-signed su localhost: verifica disattivata SOLO qui.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(LIVE_CLIENT_URL, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None  # gioco chiuso o non in partita


def poll_loop() -> None:
    last = None
    while True:
        game = summarize(read_live_client())
        with State.lock:
            State.game = game
        status = (f"in partita: {game['me']['display'] if game.get('me') else '?'} contro "
                  f"{', '.join(e['display'] for e in game.get('enemies', []))}") if game.get("inGame") else "non in partita"
        if status != last:
            print(time.strftime("%H:%M:%S"), status, flush=True)
            last = status
        time.sleep(POLL_SECONDS)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/game"):
            with State.lock:
                body = json.dumps(State.game).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Metasrc Bridge attivo. Dati partita: /game".encode("utf-8"))

    def log_message(self, *args):
        pass  # niente log per ogni richiesta del telefono


def discovery_loop() -> None:
    """Risponde ai broadcast dell'app: così il telefono trova il PC da solo."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    while True:
        data, addr = sock.recvfrom(256)
        if data.strip() == DISCOVERY_REQUEST:
            sock.sendto(DISCOVERY_REPLY, addr)


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # nessun pacchetto inviato: serve solo a scegliere l'interfaccia
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def wait_before_closing() -> None:
    """Avviato con doppio clic (.exe): senza questo la finestra sparirebbe prima di leggere l'errore."""
    if getattr(sys, "frozen", False):
        try:
            input("\nPremi Invio per chiudere...")
        except EOFError:
            pass


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("Metasrc Bridge")
        except Exception:
            pass
    try:
        server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    except OSError:
        print(f"La porta {HTTP_PORT} è già occupata: probabilmente Metasrc Bridge è già aperto in un'altra finestra.")
        wait_before_closing()
        return 1
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    print("=" * 60)
    print(" METASRC BRIDGE")
    print("=" * 60)
    print(f" IP di questo PC: {local_ip()}")
    print(" Nell'app: tab BUILD > 'Collega al PC' (stessa Wi-Fi).")
    print(" Se Windows chiede il permesso del firewall, consenti le reti PRIVATE.")
    print(" Lascia questa finestra aperta mentre giochi. Chiudila per uscire.")
    print("=" * 60, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:  # pragma: no cover - imprevisto: lo si mostra invece di chiudere in silenzio
        print(f"Errore: {e}")
        wait_before_closing()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
