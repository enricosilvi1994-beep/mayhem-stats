import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import metasrc_bridge as b

# Struttura reale (ridotta) di /liveclientdata/allgamedata
SAMPLE = {
    "activePlayer": {"riotId": "PerdoConStile#EASY", "summonerName": "PerdoConStile", "currentGold": 1234.56, "level": 9},
    "allPlayers": [
        {"championName": "Brand", "rawChampionName": "game_character_displayname_Brand",
         "riotId": "PerdoConStile#EASY", "team": "ORDER",
         "items": [{"itemID": 1001, "displayName": "Boots", "count": 1, "slot": 0},
                   {"itemID": 3802, "displayName": "Lost Chapter", "count": 1, "slot": 1}]},
        {"championName": "Miss Fortune", "rawChampionName": "game_character_displayname_MissFortune",
         "riotId": "Amico#EUW", "team": "ORDER"},
        {"championName": "Wukong", "rawChampionName": "game_character_displayname_MonkeyKing",
         "riotId": "Nemico1#EUW", "team": "CHAOS"},
        {"championName": "Fiddlesticks", "rawChampionName": "game_character_displayname_FiddleSticks",
         "riotId": "Nemico2#EUW", "team": "CHAOS"},
    ],
    "gameData": {"gameMode": "KIWI", "gameTime": 42.0},
}


class SummarizeTest(unittest.TestCase):
    def test_me_alleati_nemici(self):
        g = b.summarize(SAMPLE)
        self.assertTrue(g["inGame"])
        self.assertEqual(g["me"], {"internal": "Brand", "display": "Brand"})
        self.assertEqual([a["internal"] for a in g["allies"]], ["MissFortune"])
        self.assertEqual([e["internal"] for e in g["enemies"]], ["MonkeyKing", "FiddleSticks"])
        self.assertEqual(g["mode"], "KIWI")

    def test_oggetti_e_oro(self):
        g = b.summarize(SAMPLE)
        self.assertEqual(g["myItems"], ["1001", "3802"])
        self.assertEqual(g["gold"], 1234.6)
        self.assertEqual(g["level"], 9)

    def test_non_in_partita(self):
        self.assertEqual(b.summarize(None), {"inGame": False})
        self.assertEqual(b.summarize({}), {"inGame": False})

    def test_giocatore_non_riconosciuto(self):
        data = dict(SAMPLE, activePlayer={"riotId": "Altro#X"})
        g = b.summarize(data)
        self.assertTrue(g["inGame"])
        self.assertIsNone(g["me"])

    def test_vecchio_formato_summoner_name(self):
        data = {
            "activePlayer": {"summonerName": "Enrico"},
            "allPlayers": [
                {"championName": "Lux", "rawChampionName": "game_character_displayname_Lux", "summonerName": "Enrico", "team": "CHAOS"},
                {"championName": "Zed", "rawChampionName": "game_character_displayname_Zed", "summonerName": "X", "team": "ORDER"},
            ],
        }
        g = b.summarize(data)
        self.assertEqual(g["me"]["internal"], "Lux")
        self.assertEqual([e["internal"] for e in g["enemies"]], ["Zed"])


class ServerTest(unittest.TestCase):
    def test_endpoint_game(self):
        b.State.game = b.summarize(SAMPLE)
        server = ThreadingHTTPServer(("127.0.0.1", 0), b.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/game", timeout=3) as r:
                data = json.loads(r.read())
            self.assertEqual(data["me"]["internal"], "Brand")
            self.assertEqual(len(data["enemies"]), 2)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
