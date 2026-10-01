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

MAYHEM_QUEUE = 2400  # ARAM: Mayhem (verificato all'avvio sulle partite scaricate)

PLATFORM_TO_REGION = {
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}

DDRAGON = "https://ddragon.leagueoflegends.com"
CDRAGON_AUGMENTS = ("https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
                    "global/default/v1/cherry-augments.json")

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
            req = urllib.request.Request(url, headers={"X-Riot-Token": self.api_key})
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
        """Giocatori di Challenger/Grandmaster/Master: punto di partenza per trovare partite Mayhem."""
        data = self._get(self.platform, f"/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5")
        return [e["puuid"] for e in data.get("entries", []) if e.get("puuid")]

    def match_ids(self, puuid: str, queue: int, count: int = 100, start_time: int | None = None) -> list[str]:
        params = {"queue": queue, "count": count}
        if start_time:
            params["startTime"] = start_time
        return self._get(self.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)

    def match(self, match_id: str) -> dict:
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}")


# --------------------------------------------------------------------------- dati statici

def fetch_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "mayhem-crawler"}), timeout=30) as r:
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
    """id numerico -> augmentNameId, solo augment Mayhem (prefisso ARAM_)."""
    return {a["id"]: a["augmentNameId"] for a in cherry_json
            if str(a.get("augmentNameId", "")).startswith("ARAM_")}


# --------------------------------------------------------------------------- aggregazione

def patch_of(game_version: str) -> str:
    """'16.19.712.4521' -> '16.19'"""
    parts = (game_version or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else "?"


def empty_state() -> dict:
    return {"version": 1, "seen": [], "patches": {}}


def add_match(state: dict, match: dict, finished_items: set[str], augments: dict[int, str],
              queue: int = MAYHEM_QUEUE, champions: dict[int, str] | None = None) -> bool:
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

def crawl(client: RiotClient, state: dict, finished_items: set[str], augments: dict[int, str],
          max_matches: int, seed_tiers: Iterable[str] = ("challenger", "grandmaster"),
          days: int = 3, queue: int = MAYHEM_QUEUE, log=print,
          champions: dict[int, str] | None = None) -> int:
    """Esplora a palla di neve: giocatori seed -> loro partite Mayhem -> altri partecipanti -> ..."""
    start_time = int(time.time()) - days * 86400
    players: deque[str] = deque()
    known_players: set[str] = set()
    for tier in seed_tiers:
        try:
            for puuid in client.league_puuids(tier):
                if puuid not in known_players:
                    known_players.add(puuid)
                    players.append(puuid)
        except RiotApiError as e:
            log(f"seed {tier}: {e}")
    log(f"giocatori seed: {len(players)}")

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
            if add_match(state, m, finished_items, augments, queue, champions):
                added += 1
                if added % 100 == 0:
                    log(f"partite contate: {added} (richieste API: {client.requests})")
            for p in m.get("metadata", {}).get("participants", []):
                if p not in known_players:
                    known_players.add(p)
                    players.append(p)
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", default=os.environ.get("RIOT_PLATFORM", "euw1"))
    ap.add_argument("--max-matches", type=int, default=int(os.environ.get("MAX_MATCHES", "1500")))
    ap.add_argument("--days", type=int, default=3, help="cerca partite degli ultimi N giorni")
    ap.add_argument("--queue", type=int, default=MAYHEM_QUEUE)
    ap.add_argument("--state", default="data/state.json")
    ap.add_argument("--out", default="docs/stats.json")
    args = ap.parse_args(argv)

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
        added = crawl(client, state, finished, augments, args.max_matches, days=args.days,
                      queue=args.queue, champions=champions)
    except RiotApiError as e:
        print(f"Interrotto: {e}  (chiave scaduta? le chiavi development durano 24 ore)", file=sys.stderr)
        added = -1
    if state.get("_other_queues"):
        print(f"partite scartate per coda diversa da {args.queue}: {state['_other_queues']}")
    save_state(state, state_path)

    stats = build_stats_json(state)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(stats, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"nuove partite: {max(added, 0)} | patch {stats['patch']}: {stats['totalGames']} partite, "
          f"{len(stats['champions'])} campioni | richieste API: {client.requests}")
    return 0 if added >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
