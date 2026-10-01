import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

import mayhem_crawler as mc

ITEMS = {"data": {
    "2503": {"gold": {"total": 2800, "purchasable": True}, "tags": ["SpellDamage"]},
    "3020": {"gold": {"total": 1100, "purchasable": True}, "tags": ["Boots"]},
    "3802": {"gold": {"total": 1200, "purchasable": True}, "into": ["2503"], "tags": []},
    "2003": {"gold": {"total": 50, "purchasable": True}, "tags": ["Consumable"]},
    "2139": {"gold": {"total": 1000, "purchasable": True}, "tags": ["Consumable"]},
    "6653": {"gold": {"total": 3000, "purchasable": True}, "tags": ["SpellDamage"]},
    "9999": {"gold": {"total": 3000, "purchasable": False}, "tags": []},
}}
CHERRY = [
    {"id": 1036, "augmentNameId": "ARAM_Firebrand"},
    {"id": 386, "augmentNameId": "Firebrand"},          # Arena
    {"id": 1099, "augmentNameId": "ARAM_TwinFire"},
]
CHAMPS = {"data": {"Fiddlesticks": {"id": "Fiddlesticks", "key": "9"}, "Brand": {"id": "Brand", "key": "63"}}}


def participant(champ_id, name, win, items=(), augs=()):
    p = {"championId": champ_id, "championName": name, "win": win}
    for i in range(7):
        p[f"item{i}"] = items[i] if i < len(items) else 0
    for i in range(1, 7):
        p[f"playerAugment{i}"] = augs[i - 1] if i - 1 < len(augs) else 0
    return p


def match(mid, queue=2400, version="16.19.712.1", created=1000, duration=900, parts=None, early=False):
    parts = parts or []
    for p in parts:
        p["gameEndedInEarlySurrender"] = early
    return {"metadata": {"matchId": mid, "participants": [f"puuid-{mid}-{i}" for i in range(len(parts))]},
            "info": {"queueId": queue, "gameVersion": version, "gameCreation": created,
                     "gameDuration": duration, "participants": parts}}


class StaticDataTest(unittest.TestCase):
    def test_oggetti_finiti(self):
        self.assertEqual(mc.finished_item_ids(ITEMS), {"2503", "3020", "6653"})

    def test_solo_augment_mayhem(self):
        self.assertEqual(mc.augment_name_ids(CHERRY), {1036: "ARAM_Firebrand", 1099: "ARAM_TwinFire"})

    def test_campioni_per_chiave(self):
        self.assertEqual(mc.champion_ids_by_key(CHAMPS), {9: "Fiddlesticks", 63: "Brand"})

    def test_patch(self):
        self.assertEqual(mc.patch_of("16.19.712.4521"), "16.19")
        self.assertEqual(mc.patch_of(""), "?")


class AggregationTest(unittest.TestCase):
    def setUp(self):
        self.state = mc.load_state(Path("/nonexistent/state.json"))
        self.fin = mc.finished_item_ids(ITEMS)
        self.aug = mc.augment_name_ids(CHERRY)
        self.champs = mc.champion_ids_by_key(CHAMPS)

    def add(self, m):
        return mc.add_match(self.state, m, self.fin, self.aug, champions=self.champs)

    def test_conteggi_base(self):
        self.assertTrue(self.add(match("A", parts=[
            participant(63, "Brand", True, items=(2503, 3020, 3802, 2003, 0, 0, 3340), augs=(1036, 1099, 386)),
            participant(9, "FiddleSticks", False, items=(6653,), augs=(1036,)),
        ])))
        self.assertTrue(self.add(match("B", parts=[participant(63, "Brand", False, items=(2503, 6653), augs=(1036,))])))
        c = self.state["patches"]["16.19"]["champions"]
        self.assertEqual((c["Brand"]["games"], c["Brand"]["wins"]), (2, 1))
        self.assertEqual(c["Brand"]["items"], {"2503": [2, 1], "3020": [1, 1], "6653": [1, 0]})
        self.assertEqual(c["Brand"]["augments"], {"ARAM_Firebrand": [2, 1], "ARAM_TwinFire": [1, 1]})
        # nome della partita "FiddleSticks" normalizzato sull'ID Data Dragon
        self.assertIn("Fiddlesticks", c)
        self.assertNotIn("FiddleSticks", c)

    def test_oggetto_doppio_conta_una_volta(self):
        self.add(match("A", parts=[participant(63, "Brand", True, items=(2503, 2503))]))
        self.assertEqual(self.state["patches"]["16.19"]["champions"]["Brand"]["items"], {"2503": [1, 1]})

    def test_scarti(self):
        self.assertFalse(self.add(match("Q", queue=450, parts=[participant(63, "Brand", True)])))
        self.assertFalse(self.add(match("R", duration=200, parts=[participant(63, "Brand", True)])))
        self.assertFalse(self.add(match("S", early=True, parts=[participant(63, "Brand", True)])))
        self.assertTrue(self.add(match("T", parts=[participant(63, "Brand", True)])))
        self.assertFalse(self.add(match("T", parts=[participant(63, "Brand", True)])))  # già vista
        self.assertEqual(self.state["patches"]["16.19"]["matches"], 1)
        self.assertEqual(self.state["_other_queues"], {450: 1})

    def test_patch_corrente_e_output(self):
        self.add(match("OLD", version="16.18.1", created=500, parts=[participant(63, "Brand", True, items=(2503,))]))
        self.add(match("NEW", version="16.19.1", created=900, parts=[participant(63, "Brand", False, items=(6653,))]))
        out = mc.build_stats_json(self.state)
        self.assertEqual(out["patch"], "16.19")
        self.assertEqual(out["totalGames"], 1)
        self.assertEqual(out["champions"]["Brand"]["items"], [{"id": "6653", "games": 1, "wins": 0}])
        self.assertEqual(out["champions"]["Brand"]["starters"], [])
        self.assertNotIn("puuid", json.dumps(out))  # nessun dato personale nell'output pubblico

    def test_stato_su_disco(self):
        self.add(match("A", parts=[participant(63, "Brand", True, items=(2503,))]))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "data" / "state.json"
            mc.save_state(self.state, path)
            again = mc.load_state(path)
            self.assertIn("A", again["_seen_set"])
            self.assertFalse(mc.add_match(again, match("A", parts=[participant(63, "Brand", True)]), self.fin, self.aug))
            self.assertNotIn("_seen_set", json.loads(path.read_text()))


class RateLimiterTest(unittest.TestCase):
    def test_rispetta_la_finestra_di_120s(self):
        now = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s

        rl = mc.RateLimiter(windows=((18, 1.0), (95, 120.0)), clock=lambda: now[0], sleep=sleep)
        for _ in range(96):
            rl.acquire()
            now[0] += 0.06  # ~16 richieste/s: sotto il limite al secondo
        self.assertTrue(any(s > 100 for s in slept), slept[-3:])  # la 96ª aspetta la finestra lunga

    def test_limite_al_secondo(self):
        now = [0.0]
        slept = []
        rl = mc.RateLimiter(windows=((18, 1.0), (95, 120.0)), clock=lambda: now[0],
                            sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
        for _ in range(19):
            rl.acquire()
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 1.05, places=2)


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class ClientTest(unittest.TestCase):
    def test_429_rispetta_retry_after_e_riprova(self):
        calls, sleeps = [], []

        def opener(req):
            calls.append(req.full_url)
            self.assertEqual(req.get_header("X-riot-token"), "KEY")
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": "3"}, None)
            return FakeResp(b'["EUW1_1"]')

        c = mc.RiotClient("KEY", "euw1", limiter=mc.RateLimiter(windows=((1000, 1.0),)), opener=opener, sleep=sleeps.append)
        self.assertEqual(c.match_ids("abc", 2400, count=20, start_time=10), ["EUW1_1"])
        self.assertEqual(sleeps, [3.0])
        self.assertIn("https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/abc/ids?", calls[0])
        self.assertIn("queue=2400", calls[0])

    def test_403_chiave_non_valida(self):
        def opener(req):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"forbidden"))
        c = mc.RiotClient("BAD", "euw1", limiter=mc.RateLimiter(windows=((1000, 1.0),)), opener=opener, sleep=lambda s: None)
        with self.assertRaises(mc.RiotApiError) as ctx:
            c.match("EUW1_1")
        self.assertEqual(ctx.exception.status, 403)

    def test_regione_dalla_piattaforma(self):
        self.assertEqual(mc.RiotClient("k", "kr").region, "asia")
        with self.assertRaises(ValueError):
            mc.RiotClient("k", "xx1")


class CrawlTest(unittest.TestCase):
    def test_palla_di_neve_e_limite(self):
        class Fake:
            requests = 0
            def league_puuids(self, tier):
                return ["seed"] if tier == "challenger" else []
            def match_ids(self, puuid, queue, count=100, start_time=None):
                return {"seed": ["M1", "M2"], "puuid-M1-0": ["M3"]}.get(puuid, [])
            def match(self, mid):
                return match(mid, parts=[participant(63, "Brand", True, items=(2503,))])

        state = mc.load_state(Path("/nonexistent"))
        added = mc.crawl(Fake(), state, mc.finished_item_ids(ITEMS), {}, max_matches=3, log=lambda *a: None)
        self.assertEqual(added, 3)
        self.assertEqual(set(state["seen"]), {"M1", "M2", "M3"})


class SeedFailureTest(unittest.TestCase):
    def test_chiave_rifiutata_sui_seed_fa_fallire(self):
        class Fake:
            requests = 0
            def league_puuids(self, tier):
                raise mc.RiotApiError(403, "league", "Forbidden")
        with self.assertRaises(mc.RiotApiError):
            mc.crawl(Fake(), mc.load_state(Path("/x")), set(), {}, max_matches=10, log=lambda *a: None)

    def test_nessun_giocatore_fa_fallire(self):
        class Fake:
            requests = 0
            def league_puuids(self, tier):
                return []
        with self.assertRaises(mc.RiotApiError):
            mc.crawl(Fake(), mc.load_state(Path("/x")), set(), {}, max_matches=10, log=lambda *a: None)


if __name__ == "__main__":
    unittest.main()
