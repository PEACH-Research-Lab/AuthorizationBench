"""music_player service — fixture-backed music search, playback, and playlists.

Reduced from AppWorld Spotify to a non-copy surface:
    search_tracks, play_track, list_playlists, add_to_playlist

Deterministic fixture state; no network, OAuth, wall-clock, or LLM.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class SearchTracksRequest(BaseModel):
    query: str = Field("", description="Substring search over title, artist, album.")


class PlayTrackRequest(BaseModel):
    track_id: str = Field(..., description="Track id from search_tracks.")


class AddToPlaylistRequest(BaseModel):
    playlist_id: str = Field(..., description="Playlist id from list_playlists.")
    track_id: str = Field(..., description="Track id to append.")


class MusicPlayerService(ServiceBase):
    _READ = ("search_tracks", "list_playlists")
    _MUTATE = ("play_track", "add_to_playlist")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("music", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["play_track"] = action_contract(
        "core", output={"ok": "boolean", "now_playing": "object"},
        effects=("set now playing to one track",),
        errors=(("TRACK_NOT_FOUND", 404, "track id is absent"),),
        audit=("action", "status", "track_id"),
        tags=("music", "playback", "state_change"),
    )
    ACTION_CONTRACTS["add_to_playlist"] = action_contract(
        "core", output={"ok": "boolean", "playlist": "object"},
        effects=("append one track id to a playlist",),
        errors=(
            ("PLAYLIST_NOT_FOUND", 404, "playlist id is absent"),
            ("TRACK_NOT_FOUND", 404, "track id is absent"),
        ),
        audit=("action", "status", "playlist_id", "track_id"),
        tags=("music", "playlist", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="music_player")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "user_name": "str",
        "tracks": [{
            "id": "str",
            "title": "str",
            "artist": "str",
            "album": "str",
            "duration_seconds": "int",
        }],
        "playlists": [{
            "id": "str",
            "name": "str",
            "track_ids": "list",
        }],
        "now_playing": {
            "track_id": "str",
            "started_at": "str",
        },
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.user_name = data.get("user_name", "")
        self.tracks: list[dict] = []
        for raw in list(data.get("tracks", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("track"))
            entry.setdefault("title", "")
            entry.setdefault("artist", "")
            entry.setdefault("album", "")
            entry.setdefault("duration_seconds", 0)
            self.tracks.append(entry)
        self.playlists: list[dict] = []
        for raw in list(data.get("playlists", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("playlist"))
            entry.setdefault("name", "")
            entry.setdefault("track_ids", [])
            entry["track_ids"] = [str(t) for t in list(entry.get("track_ids") or [])]
            self.playlists.append(entry)
        np = data.get("now_playing")
        if isinstance(np, dict) and np.get("track_id"):
            self.now_playing: Optional[dict] = {
                "track_id": str(np.get("track_id")),
                "started_at": str(np.get("started_at") or ""),
            }
        else:
            self.now_playing = None

    def get_state(self) -> dict:
        return {
            "user_name": self.user_name,
            "tracks": self.tracks,
            "playlists": self.playlists,
            "now_playing": self.now_playing,
        }

    def _find_track(self, track_id: str) -> Optional[dict]:
        return next(
            (t for t in self.tracks if str(t.get("id")) == str(track_id)),
            None,
        )

    def _find_playlist(self, playlist_id: str) -> Optional[dict]:
        return next(
            (p for p in self.playlists if str(p.get("id")) == str(playlist_id)),
            None,
        )

    def _ordered_playlists(self) -> list[dict]:
        return sorted(
            self.playlists,
            key=lambda p: (str(p.get("name", "")).lower(), str(p.get("id", ""))),
        )

    def _search_tracks(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        if not q:
            return sorted(
                self.tracks,
                key=lambda t: (
                    str(t.get("title", "")).lower(),
                    str(t.get("id", "")),
                ),
            )
        terms = [t for t in re.split(r"\s+", q) if t]
        scored: list[tuple[int, dict]] = []
        for track in self.tracks:
            haystack = " ".join([
                str(track.get("title", "")),
                str(track.get("artist", "")),
                str(track.get("album", "")),
            ]).lower()
            score = sum(haystack.count(term) for term in terms)
            if score > 0:
                scored.append((score, track))
        scored.sort(
            key=lambda item: (
                -item[0],
                str(item[1].get("title", "")).lower(),
                str(item[1].get("id", "")),
            ),
        )
        return [t for _, t in scored]

    def _serialize_playlist(self, playlist: dict) -> dict:
        track_ids = list(playlist.get("track_ids") or [])
        return {
            **playlist,
            "track_ids": track_ids,
            "track_count": len(track_ids),
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/tracks/search", operation_id="search_tracks",
                          summary="Search the fixture track catalog")
        def search_tracks(req: SearchTracksRequest) -> dict:
            params = req.model_dump()
            tracks = svc._search_tracks(req.query)
            svc.log_action(
                "search_tracks",
                params=params,
                result_count=len(tracks),
                track_ids=[t.get("id") for t in tracks],
            )
            return {
                "ok": True,
                "query": req.query,
                "tracks": tracks,
                "tracks_total": len(svc.tracks),
            }

        @self.router.post("/tracks/play", operation_id="play_track",
                          summary="Set the now-playing track")
        def play_track(req: PlayTrackRequest):
            params = req.model_dump()
            track = svc._find_track(req.track_id)
            if not track:
                svc.log_action(
                    "play_track", params=params, status=404, track_id=req.track_id,
                )
                return svc.error_response(
                    code="TRACK_NOT_FOUND",
                    message="Track not found.",
                    status=404,
                    details={"track_id": req.track_id},
                )
            entry = {
                "track_id": str(track.get("id")),
                "started_at": svc.now_iso(),
            }
            svc.now_playing = entry
            svc.log_action(
                "play_track",
                params=params,
                effect={"track_id": entry["track_id"]},
                track_id=entry["track_id"],
                title=track.get("title", ""),
                artist=track.get("artist", ""),
            )
            return {"ok": True, "now_playing": entry, "track": track}

        @self.router.post("/playlists/list", operation_id="list_playlists",
                          summary="List playlists owned in the fixture")
        def list_playlists() -> dict:
            playlists = [svc._serialize_playlist(p) for p in svc._ordered_playlists()]
            svc.log_action(
                "list_playlists",
                params={},
                playlist_count=len(playlists),
            )
            return {
                "ok": True,
                "user_name": svc.user_name,
                "playlists": playlists,
            }

        @self.router.post("/playlists/add", operation_id="add_to_playlist",
                          summary="Append a track to a playlist")
        def add_to_playlist(req: AddToPlaylistRequest):
            params = req.model_dump()
            playlist = svc._find_playlist(req.playlist_id)
            if not playlist:
                svc.log_action(
                    "add_to_playlist",
                    params=params, status=404, playlist_id=req.playlist_id,
                )
                return svc.error_response(
                    code="PLAYLIST_NOT_FOUND",
                    message="Playlist not found.",
                    status=404,
                    details={"playlist_id": req.playlist_id},
                )
            track = svc._find_track(req.track_id)
            if not track:
                svc.log_action(
                    "add_to_playlist",
                    params=params, status=404, track_id=req.track_id,
                )
                return svc.error_response(
                    code="TRACK_NOT_FOUND",
                    message="Track not found.",
                    status=404,
                    details={"track_id": req.track_id},
                )
            track_id = str(track.get("id"))
            playlist.setdefault("track_ids", []).append(track_id)
            serialized = svc._serialize_playlist(playlist)
            svc.log_action(
                "add_to_playlist",
                params=params,
                effect={
                    "playlist_id": req.playlist_id,
                    "track_id": track_id,
                    "track_count": serialized["track_count"],
                },
                playlist_id=req.playlist_id,
                track_id=track_id,
            )
            return {"ok": True, "playlist": serialized, "track": track}


def create_service(service_id: str) -> MusicPlayerService:
    return MusicPlayerService(service_id)
