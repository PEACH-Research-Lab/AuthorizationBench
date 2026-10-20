"""Smoke tests for services/music_player/service.py."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.music_player.service import MusicPlayerService, create_service  # noqa: E402


FIXTURE = {
    "user_name": "Alex Morgan",
    "tracks": [
        {
            "id": "tr-1",
            "title": "Morning Light",
            "artist": "Rivera Band",
            "album": "Dawn Sessions",
            "duration_seconds": 210,
        },
        {
            "id": "tr-2",
            "title": "Night Drive",
            "artist": "Jordan Lee",
            "album": "City Loops",
            "duration_seconds": 185,
        },
        {
            "id": "tr-3",
            "title": "Quiet Hours",
            "artist": "Rivera Band",
            "album": "Dawn Sessions",
            "duration_seconds": 240,
        },
    ],
    "playlists": [
        {
            "id": "pl-focus",
            "name": "Focus",
            "track_ids": ["tr-1"],
        },
        {
            "id": "pl-commute",
            "name": "Commute",
            "track_ids": ["tr-2", "tr-3"],
        },
    ],
    "now_playing": None,
}


class MusicPlayerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(FIXTURE, self._tmp)
        self._tmp.close()
        os.environ["MUSIC_PLAYER_FIXTURES"] = self._tmp.name
        self.svc = create_service("music_player")
        self.svc.load_data()
        app = FastAPI()
        app.include_router(self.svc.router, prefix="/music_player")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("MUSIC_PLAYER_FIXTURES", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_profiles_and_contracts_cover_all_actions(self) -> None:
        expected = {
            "search_tracks",
            "play_track",
            "list_playlists",
            "add_to_playlist",
        }
        self.assertEqual(
            set(MusicPlayerService.CAPABILITY_PROFILES["standard-v1"]), expected
        )
        self.assertEqual(
            set(MusicPlayerService.CAPABILITY_PROFILES["full-v1"]), expected
        )
        self.assertEqual(set(MusicPlayerService.ACTION_CONTRACTS), expected)
        for name in MusicPlayerService._READ:
            contract = MusicPlayerService.ACTION_CONTRACTS[name]
            self.assertEqual(contract.state_effects, ())
            self.assertNotIn("state_change", contract.capability_tags)
        for name in MusicPlayerService._MUTATE:
            contract = MusicPlayerService.ACTION_CONTRACTS[name]
            self.assertTrue(contract.state_effects)
            self.assertIn("state_change", contract.capability_tags)

    def test_search_list_play_and_add(self) -> None:
        r = self.client.post(
            "/music_player/tracks/search",
            json={"query": "rivera"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual([t["id"] for t in body["tracks"]], ["tr-1", "tr-3"])

        r = self.client.post("/music_player/playlists/list")
        self.assertEqual(r.status_code, 200)
        names = [p["name"] for p in r.json()["playlists"]]
        self.assertEqual(names, ["Commute", "Focus"])

        r = self.client.post(
            "/music_player/tracks/play",
            json={"track_id": "tr-2"},
        )
        self.assertEqual(r.status_code, 200)
        play = r.json()
        self.assertTrue(play["ok"])
        self.assertEqual(play["now_playing"]["track_id"], "tr-2")
        self.assertTrue(play["now_playing"]["started_at"])

        r = self.client.post(
            "/music_player/playlists/add",
            json={"playlist_id": "pl-focus", "track_id": "tr-2"},
        )
        self.assertEqual(r.status_code, 200)
        playlist = r.json()["playlist"]
        self.assertEqual(playlist["track_ids"], ["tr-1", "tr-2"])
        self.assertEqual(playlist["track_count"], 2)

        state = self.client.get("/music_player/state").json()["state"]
        self.assertEqual(state["now_playing"]["track_id"], "tr-2")
        focus = next(p for p in state["playlists"] if p["id"] == "pl-focus")
        self.assertEqual(focus["track_ids"], ["tr-1", "tr-2"])

        audit = self.client.get("/music_player/audit").json()["actions"]
        self.assertEqual(
            [e["action"] for e in audit],
            ["search_tracks", "list_playlists", "play_track", "add_to_playlist"],
        )
        self.assertEqual(audit[-1]["status"], 200)

    def test_missing_ids_are_logged(self) -> None:
        r = self.client.post(
            "/music_player/tracks/play",
            json={"track_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TRACK_NOT_FOUND")

        r = self.client.post(
            "/music_player/playlists/add",
            json={"playlist_id": "missing", "track_id": "tr-1"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "PLAYLIST_NOT_FOUND")

        r = self.client.post(
            "/music_player/playlists/add",
            json={"playlist_id": "pl-focus", "track_id": "missing"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "TRACK_NOT_FOUND")

        audit = self.client.get("/music_player/audit").json()["actions"]
        self.assertEqual(audit[-1]["action"], "add_to_playlist")
        self.assertEqual(audit[-1]["status"], 404)


if __name__ == "__main__":
    unittest.main()
