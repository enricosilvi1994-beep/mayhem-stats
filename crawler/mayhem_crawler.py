#!/usr/bin/env python3
"""
Crawler statistiche ARAM: Mayhem (Riot Match-V5) -> docs/stats.json

- Solo libreria standard Python (3.9+), nessuna dipendenza.
- La chiave Riot si legge dalla variabile d'ambiente RIOT_API_KEY e non finisce MAI nell'output.
- Lo stato (partite già contate + conteggi per patch) è in data/state.json, così ogni
  esecuzione aggiunge partite nuove a quelle dei giorni precedenti.
- docs/stats.json contiene SOLO conteggi aggregati (nessun PUUID, nessun nome giocatore).

Uso:
    RIOT_API_KEY=RGAPI-... python mayhem_crawler.py --platform euw1 --max-matches 1500
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# ARAM: Pandemonio (Mayhem) nel 2026: coda 1750. L'API la registra con gameMode "CHERRY" e mapId 30
# (gli stessi codici di Arena) anche se si gioca su Abisso ululante / Valico Koeshin / Ponte del massacro.
# Verificato confrontando la cronologia partite del client con Match-V5. 0 = rilevamento automatico.
MAYHEM_QUEUE = 1750
HOWLING_ABYSS_MAP = 12

PLATFORM_TO_REGION = {
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}

DDRAGON = "https://ddragon.leagueoflegends.com"
CDRAGON_AUGMENTS = ("https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
                    "global/default/v1/cherry-augments.json")

USER_AGENT = "mayhem-stats-crawler/1.0 (+https://github.com/enricosilvi1994-beep/mayhem-stats)"

# Un oggetto conta nella build se è "finito": non si trasforma in altro e costa almeno questo.
FINISHED_ITEM_MIN_GOLD = 1000
AUGMENT_SLOTS = 6
ITEM_SLOTS = 6  # item0..item5 (item6 è il trinket)


# --------------------------------------------------------------------------- HTTP

class RiotApiError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} su {url} {body[:200]}")
        self.status = status


@dataclass
class RateLimiter:
    """Limite chiave development: 20 richieste/1s e 100 richieste/120s. Si resta sotto con margine."""
    windows: tuple = ((18, 1.0), (95, 120.0))
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _stamps: deque = field(default_factory=deque)

    def acquire(self) -> None:
        while True:
            now = self.clock()
            longest = max(w for _, w in self.windows)
            while self._stamps and now - self._stamps[0] > longest:
                self._stamps.popleft()
            wait = 0.0
            for limit, window in self.windows:
                recent = [t for t in self._stamps if now - t <= window]
                if len(recent) >= limit:
                    wait = max(wait, window - (now - recent[0]) + 0.05)
            if wait <= 0:
                self._stamps.append(now)
                return
            self.sleep(wait)


class RiotClient:
    def __init__(self, api_key: str, platform: str, limiter: RateLimiter | None = None,
                 opener: Callable[[urllib.request.Request], object] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        if platform not in PLATFORM_TO_REGION:
            raise ValueError(f"platform sconosciuta: {platform}")
        self.api_key = api_key
        self.platform = platform
        self.region = PLATFORM_TO_REGION[platform]
        self.limiter = limiter or RateLimiter()
        self.opener = opener or (lambda req: urllib.request.urlopen(req, timeout=20))
        self.sleep = sleep
        self.requests = 0

    def _get(self, host: str, path: str, params: dict | None = None, retries: int = 5):
        url = f"https://{host}.api.riotgames.com{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(retries):
            self.limiter.acquire()
            self.requests += 1
            req = urllib.request.Request(url, headers={
                "X-Riot-Token": self.api_key,
                # Le API Riot sono dietro Cloudflare: l'User-Agent di default "Python-urllib/x.y"
                # viene bloccato con HTTP 403 "error code: 1010".
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            try:
                with self.opener(req) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    retry_after = float(e.headers.get("Retry-After", 2 ** attempt) if e.headers else 2 ** attempt)
                    self.sleep(retry_after)
                    continue
                raise RiotApiError(e.code, url, e.read().decode("utf-8", "ignore") if hasattr(e, "read") else "")
            except urllib.error.URLError:
                self.sleep(2 ** attempt)
        raise RiotApiError(0, url, "troppi tentativi")

    # --- endpoint usati
    def league_puuids(self, tier: str) -> list[str]:
        """Giocatori di un rank (punto di partenza per trovare partite Mayhem).
        challenger/grandmaster/master: lega intera; DIAMOND…IRON: prima pagina della divisione I."""
        if tier.lower() in APEX_TIERS:
            data = self._get(self.platform, f"/lol/league/v4/{tier.lower()}leagues/by-queue/RANKED_SOLO_5x5")
            entries = data.get("entries", [])
        else:
            entries = self._get(self.platform, f"/lol/league/v4/entries/RANKED_SOLO_5x5/{tier.upper()}/I", {"page": 1})
        return [e["puuid"] for e in entries if e.get("puuid")]

    def match_ids(self, puuid: str, queue: int | None, count: int = 100, start_time: int | None = None) -> list[str]:
        params = {"count": count}
        if queue:
            params["queue"] = queue
        if start_time:
            params["startTime"] = start_time
        return self._get(self.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)

    def puuid_by_riot_id(self, riot_id: str) -> str:
        """'Nome#TAG' -> PUUID (ACCOUNT-V1). Utile come primo giocatore: chi usa l'app gioca Mayhem."""
        name, _, tag = riot_id.partition("#")
        path = f"/riot/account/v1/accounts/by-riot-id/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}"
        return self._get(self.region, path)["puuid"]

    def match(self, match_id: str) -> dict:
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}")


# --------------------------------------------------------------------------- dati statici

def fetch_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def finished_item_ids(item_json: dict) -> set[str]:
    """Oggetti finiti (leggendari e stivali completi) da Data Dragon item.json."""
    out = set()
    for item_id, it in item_json.get("data", {}).items():
        gold = it.get("gold", {})
        if it.get("into"):
            continue
        if gold.get("total", 0) < FINISHED_ITEM_MIN_GOLD or not gold.get("purchasable", False):
            continue
        if "Consumable" in it.get("tags", []):
            continue
        out.add(item_id)
    return out


def champion_ids_by_key(champion_json: dict) -> dict[int, str]:
    """championId numerico -> ID Data Dragon (es. 9 -> 'Fiddlesticks', 62 -> 'MonkeyKing').
    Serve perché championName nelle partite non coincide sempre con l'ID DDragon ('FiddleSticks')."""
    return {int(c["key"]): c["id"] for c in champion_json.get("data", {}).values()}


def augment_name_ids(cherry_json: list) -> dict[int, str]:
    """id numerico -> augmentNameId per tutti gli augment (Mayhem `ARAM_…` e Arena).
    Le partite Mayhem possono riportare anche ID della variante Arena: l'app li abbina per nome."""
    names = {a["augmentNameId"] for a in cherry_json if a.get("augmentNameId")}
    out = {}
    for a in cherry_json:
        name = a.get("augmentNameId")
        if not name:
            continue
        # stesso augment nelle due varianti: si conta sotto il nome Mayhem, così i numeri si sommano
        out[a["id"]] = f"ARAM_{name}" if not name.startswith("ARAM_") and f"ARAM_{name}" in names else name
    return out


# --------------------------------------------------------------------------- aggregazione

def patch_of(game_version: str) -> str:
    """'16.19.712.4521' -> '16.19'"""
    parts = (game_version or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else "?"


def empty_state() -> dict:
    return {"version": 1, "seen": [], "patches": {}}


def add_match(state: dict, match: dict, finished_items: set[str], augments: dict[int, str],
              queue: int = 2400, champions: dict[int, str] | None = None) -> bool:
    """Aggiunge una partita ai conteggi. Ritorna False se scartata (coda diversa, remake, già vista)."""
    meta = match.get("metadata", {})
    info = match.get("info", {})
    match_id = meta.get("matchId")
    if not match_id or match_id in state["_seen_set"]:
        return False
    state["_seen_set"].add(match_id)
    state["seen"].append(match_id)

    if info.get("queueId") != queue:
        other = state.setdefault("_other_queues", {})
        other[info.get("queueId")] = other.get(info.get("queueId"), 0) + 1
        return False
    if info.get("gameDuration", 0) < 300 or any(p.get("gameEndedInEarlySurrender") for p in info.get("participants", [])):
        return False  # remake / resa anticipata: falserebbe i win rate
    if not is_two_teams_of_five(info):
        other = state.setdefault("_other_queues", {})
        key = f"{info.get('queueId')} (non 5v5: Arena?)"
        other[key] = other.get(key, 0) + 1
        return False

    patch = patch_of(info.get("gameVersion", ""))
    bucket = state["patches"].setdefault(patch, {"matches": 0, "lastGameCreation": 0, "champions": {}})
    bucket["matches"] += 1
    bucket["lastGameCreation"] = max(bucket["lastGameCreation"], info.get("gameCreation", 0))

    for p in info.get("participants", []):
        champ = (champions or {}).get(p.get("championId")) or p.get("championName")
        if not champ:
            continue
        win = 1 if p.get("win") else 0
        c = bucket["champions"].setdefault(champ, {"games": 0, "wins": 0, "items": {}, "augments": {}})
        c["games"] += 1
        c["wins"] += win

        items = {str(p.get(f"item{i}", 0)) for i in range(ITEM_SLOTS)} - {"0"}
        for item_id in items & finished_items:   # ogni oggetto conta una volta per partita
            row = c["items"].setdefault(item_id, [0, 0])
            row[0] += 1
            row[1] += win

        seen_aug = set()
        for i in range(1, AUGMENT_SLOTS + 1):
            aug_id = p.get(f"playerAugment{i}", 0)
            name_id = augments.get(aug_id)
            if name_id and name_id not in seen_aug:
                seen_aug.add(name_id)
                row = c["augments"].setdefault(name_id, [0, 0])
                row[0] += 1
                row[1] += win
    return True


def is_two_teams_of_five(info: dict) -> bool:
    """Pandemonio è 5 contro 5 (squadre 100 e 200). Arena ha 16 giocatori in 8 coppie e un piazzamento.
    Serve perché l'API usa per entrambe gameMode "CHERRY": così l'Arena non finisce mai nei conti."""
    parts = info.get("participants", [])
    if len(parts) != 10:
        return False
    teams = [p.get("teamId") for p in parts]
    if sorted(set(teams)) != [100, 200] or teams.count(100) != 5:
        return False
    return not any(p.get("placement") or p.get("playerSubteamId") for p in parts)


def current_patch(state: dict) -> str | None:
    if not state["patches"]:
        return None
    return max(state["patches"].items(), key=lambda kv: kv[1]["lastGameCreation"])[0]


def build_stats_json(state: dict, min_games: int = 1) -> dict:
    """Formato letto dall'app (OwnStatsSource): conteggi grezzi, la classifica la calcola l'app."""
    patch = current_patch(state)
    if patch is None:
        return {"patch": None, "totalGames": 0, "champions": {}}
    bucket = state["patches"][patch]

    def rows(d: dict, key: str) -> list[dict]:
        out = [{key: k, "games": g, "wins": w} for k, (g, w) in d.items() if g >= min_games]
        return sorted(out, key=lambda r: (-r["games"], r[key]))

    return {
        "patch": patch,
        "generatedAt": int(time.time()),
        "totalGames": bucket["matches"],
        "champions": {
            champ: {
                "games": c["games"],
                "wins": c["wins"],
                "starters": [],  # Match-V5 dà solo l'inventario finale (gli iniziali richiederebbero la timeline)
                "items": rows(c["items"], "id"),
                "augments": rows(c["augments"], "nameId"),
            }
            for champ, c in sorted(bucket["champions"].items())
        },
    }


def load_state(path: Path) -> dict:
    state = json.loads(path.read_text("utf-8")) if path.exists() else empty_state()
    state["_seen_set"] = set(state["seen"])
    return state


def save_state(state: dict, path: Path, keep_patches: int = 2, keep_seen: int = 200_000) -> None:
    # tieni solo le patch più recenti e un numero limitato di ID già visti
    recent = sorted(state["patches"].items(), key=lambda kv: kv[1]["lastGameCreation"], reverse=True)[:keep_patches]
    out = {"version": 1, "seen": state["seen"][-keep_seen:], "patches": dict(recent)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, separators=(",", ":")), "utf-8")


# --------------------------------------------------------------------------- crawl

ARENA_MAP = 30
APEX_TIERS = ("challenger", "grandmaster", "master")
ALL_TIERS = APEX_TIERS + ("DIAMOND", "EMERALD", "PLATINUM", "GOLD", "SILVER", "BRONZE", "IRON")


def is_mayhem_match(match: dict) -> bool:
    """Mayhem = partita con augment scelti dai giocatori che NON è Arena (mappa 30 / gameMode CHERRY).
    Di norma è sull'Abisso Ululante (mappa 12), ma non lo si dà per scontato."""
    info = match.get("info", {})
    mode = str(info.get("gameMode", "")).upper()
    if info.get("mapId") == ARENA_MAP or mode == "CHERRY":
        return False
    if mode in MAYHEM_GAME_MODES:
        return True  # nome interno della modalità, anche se gli augment non fossero nei campi playerAugment
    return has_augments(match)


MAYHEM_GAME_MODES = {"KIWI", "ARAM_MAYHEM", "MAYHEM"}


def has_augments(match: dict) -> bool:
    return any(p.get(f"playerAugment{i}", 0)
               for p in match.get("info", {}).get("participants", []) for i in range(1, AUGMENT_SLOTS + 1))


def describe(match: dict) -> str:
    info = match.get("info", {})
    return (f"{match.get('metadata', {}).get('matchId')}: coda {info.get('queueId')} · modalità {info.get('gameMode')} · "
            f"mappa {info.get('mapId')} · augment {'sì' if has_augments(match) else 'no'}")


def explain_player(client, puuid: str, count: int = 10, log=print) -> None:
    """Diagnostica: stampa le ultime partite di un giocatore (tipicamente chi usa l'app)."""
    try:
        for mid in client.match_ids(puuid, None, count=count):
            log("  " + describe(client.match(mid)))
    except RiotApiError as e:
        log(f"  impossibile leggere le partite: {e}")


def detect_queue(client, players: list[str], max_players: int = 40, per_player: int = 10, log=print):
    """Scopre il queueId di Mayhem guardando le partite recenti (senza filtro) di alcuni giocatori.
    Ritorna (queueId o None, {(queueId, gameMode, mapId): conteggio} visti)."""
    seen: dict = {}
    for puuid in players[:max_players]:
        try:
            ids = client.match_ids(puuid, None, count=per_player)
        except RiotApiError as e:
            if e.status in (401, 403):
                raise
            continue
        for mid in ids:
            try:
                m = client.match(mid)
            except RiotApiError as e:
                if e.status in (401, 403):
                    raise
                continue
            info = m.get("info", {})
            key = (info.get("queueId"), info.get("gameMode"), info.get("mapId"))
            seen[key] = seen.get(key, 0) + 1
            if is_mayhem_match(m):
                log(f"coda Mayhem trovata: queueId={info.get('queueId')} gameMode={info.get('gameMode')}")
                return info.get("queueId"), seen
    return None, seen


def seed_players(client, seed_tiers: Iterable[str] = ALL_TIERS,
                 first: list[str] | None = None, log=print) -> list[str]:
    """Giocatori di partenza: prima quelli indicati (es. chi usa l'app), poi tutti i rank
    alternati (uno per rank a turno), così anche i primi campioni coprono rank diversi."""
    players: list[str] = []
    known: set[str] = set()
    for puuid in first or []:
        if puuid not in known:
            known.add(puuid)
            players.append(puuid)
    per_tier: list[list[str]] = []
    for tier in seed_tiers:
        try:
            per_tier.append(client.league_puuids(tier))
        except RiotApiError as e:
            if e.status in (401, 403):
                raise  # chiave errata/scaduta: deve fallire in rosso, non "Success" con 0 partite
            log(f"seed {tier}: {e}")
    for i in range(max((len(t) for t in per_tier), default=0)):
        for tier_list in per_tier:
            if i < len(tier_list) and tier_list[i] not in known:
                known.add(tier_list[i])
                players.append(tier_list[i])
    log(f"giocatori seed: {len(players)}")
    if not players:
        raise RiotApiError(0, "league-v4", "nessun giocatore di partenza trovato")
    return players


def crawl(client, state: dict, finished_items: set[str], augments: dict[int, str],
          max_matches: int, seed_tiers: Iterable[str] = ("challenger", "grandmaster"),  # solo se players_seed è None
          days: int = 3, queue: int = 2400, log=print,
          champions: dict[int, str] | None = None, players_seed: list[str] | None = None) -> int:
    """Esplora a palla di neve: giocatori seed -> loro partite Mayhem -> altri partecipanti -> ..."""
    start_time = int(time.time()) - days * 86400
    seeds = players_seed if players_seed is not None else seed_players(client, seed_tiers, log=log)
    players: deque[str] = deque(seeds)
    known_players: set[str] = set(seeds)

    added = 0
    while players and added < max_matches:
        puuid = players.popleft()
        try:
            ids = client.match_ids(puuid, queue, count=20, start_time=start_time)
        except RiotApiError as e:
            if e.status in (401, 403):
                raise  # chiave scaduta o non valida: inutile continuare
            continue
        for match_id in ids:
            if added >= max_matches:
                break
            if match_id in state["_seen_set"]:
                continue
            try:
                m = client.match(match_id)
            except RiotApiError as e:
                if e.status in (401, 403):
                    raise
                continue
            accepted = add_match(state, m, finished_items, augments, queue, champions)
            if accepted:
                added += 1
                if added % 100 == 0:
                    log(f"partite contate: {added} (richieste API: {client.requests})")
            for p in m.get("metadata", {}).get("participants", []):
                if p not in known_players:
                    known_players.add(p)
                    # chi ha appena giocato Mayhem probabilmente lo rigioca: lo si visita per primo
                    if accepted:
                        players.appendleft(p)
                    else:
                        players.append(p)
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", default=os.environ.get("RIOT_PLATFORM", "euw1"))
    ap.add_argument("--max-matches", type=int, default=int(os.environ.get("MAX_MATCHES", "1500")))
    ap.add_argument("--days", type=int, default=3, help="cerca partite degli ultimi N giorni")
    ap.add_argument("--queue", type=int, default=int(os.environ.get("MAYHEM_QUEUE", MAYHEM_QUEUE)),
                    help="queueId di Mayhem; 0 = rilevamento automatico")
    ap.add_argument("--riot-id", default=os.environ.get("RIOT_ID", ""),
                    help="'Nome#TAG' di un giocatore Mayhem da usare come primo seed (facoltativo)")
    ap.add_argument("--state", default="data/state.json")
    ap.add_argument("--out", default="docs/stats.json")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # log visibili in tempo reale su GitHub Actions

    key = os.environ.get("RIOT_API_KEY", "").strip()
    if not key:
        print("Manca RIOT_API_KEY", file=sys.stderr)
        return 2

    version = fetch_json(f"{DDRAGON}/api/versions.json")[0]
    finished = finished_item_ids(fetch_json(f"{DDRAGON}/cdn/{version}/data/en_US/item.json"))
    augments = augment_name_ids(fetch_json(CDRAGON_AUGMENTS))
    champions = champion_ids_by_key(fetch_json(f"{DDRAGON}/cdn/{version}/data/en_US/champion.json"))
    print(f"DDragon {version}: {len(finished)} oggetti finiti, {len(augments)} augment Mayhem")

    state_path, out_path = Path(args.state), Path(args.out)
    state = load_state(state_path)
    client = RiotClient(key, args.platform)
    try:
        first = []
        if args.riot_id:
            try:
                first = [client.puuid_by_riot_id(args.riot_id)]
            except RiotApiError as e:
                print(f"riot-id {args.riot_id}: {e}")
        if first:
            print(f"Ultime partite di {args.riot_id}:")
            explain_player(client, first[0])
        players = seed_players(client, first=first)
        queue = args.queue
        if not queue:
            queue, seen = detect_queue(client, players)
            if queue is None:
                print("Partite viste durante il rilevamento (coda · modalità · mappa → quante):")
                for (q, g, mp), n in sorted(seen.items(), key=lambda kv: -kv[1]):
                    print(f"  coda {q} · {g} · mappa {mp} → {n}")
                raise RiotApiError(0, "rilevamento coda", "nessuna partita Mayhem tra quelle recenti dei giocatori campione")
        args.queue = queue
        added = crawl(client, state, finished, augments, args.max_matches, days=args.days,
                      queue=queue, champions=champions, players_seed=players)
    except RiotApiError as e:
        hint = {0: "nessun problema di chiave: vedi il messaggio sopra",
                401: "chiave mancante o non inviata", 403: "chiave non valida o rigenerata: aggiorna il secret RIOT_API_KEY"}
        if "1010" in str(e):
            hint[403] = "blocco Cloudflare sull'User-Agent (error code 1010), non un problema di chiave"
        print(f"ERRORE: {e}\n→ {hint.get(e.status, 'controlla piattaforma (RIOT_PLATFORM) e connessione')}", file=sys.stderr)
        added = -1
    if state.get("_other_queues"):
        print(f"partite scartate per coda diversa da {args.queue}: {state['_other_queues']}")
    save_state(state, state_path)

    stats = build_stats_json(state)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(stats, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"nuove partite: {max(added, 0)} | patch {stats['patch']}: {stats['totalGames']} partite, "
          f"{len(stats['champions'])} campioni | richieste API: {client.requests}")
    if added == 0 and stats["totalGames"] == 0:
        print("ERRORE: nessuna partita Mayhem trovata. Se sopra compare 'partite scartate per coda diversa', "
              "la coda non è 2400: rilancia con --queue <numero>.", file=sys.stderr)
        return 1
    return 0 if added >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
