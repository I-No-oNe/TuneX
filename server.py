"""
🎵 RaspberryPi Music Server
- Streams audio from YouTube via yt-dlp
- Per-user search cache, playlist support, and feed suggestions
- Audio URL caching with TTL + background prefetch
- Request deduplication (coalesces concurrent fetches for same video)
- Private access via API key
"""

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Depends, Query, Body
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
import yt_dlp
from yt_dlp.utils import DownloadError

# ── Config ──────────────────────────────────────────────────────────────────
API_KEYS_FILE   = Path("api_keys.json")
USER_DATA_DIR   = Path("user_data")
Path("cache").mkdir(exist_ok=True)
USER_DATA_DIR.mkdir(exist_ok=True)

SEARCH_CACHE_TTL  = 60 * 30        # 30 min
AUDIO_URL_TTL     = 60 * 60 * 4   # 4 h  (YouTube signed URLs last ~6 h)
RELATED_CACHE_TTL = 60 * 60       # 1 h

if not API_KEYS_FILE.exists():
    key = secrets.token_urlsafe(16)
    API_KEYS_FILE.write_text(json.dumps({"keys": {"admin": key}}, indent=2))
    print(f"⚠️  Created api_keys.json — admin key: {key}")


def load_keys() -> dict[str, str]:
    return cast(dict[str, str], json.loads(API_KEYS_FILE.read_text())["keys"])


# ── In-memory caches ─────────────────────────────────────────────────────────
# { video_id: {"url": str, "ts": float} }
_audio_url_cache: dict[str, dict[str, Any]] = {}

# { video_id: asyncio.Event }  — deduplicates concurrent fetches
_audio_url_inflight: dict[str, asyncio.Event] = {}

# { video_id: {"info": dict, "ts": float} }
_track_info_cache: dict[str, dict[str, Any]] = {}

# { video_id: {"related": list, "ts": float} }
_related_cache: dict[str, dict[str, Any]] = {}


# ── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(title="🎵 Pi Music Server", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

security = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_user(api_key: str = Depends(security)) -> str:
    keys = load_keys()
    if api_key not in keys.values():
        raise HTTPException(status_code=403, detail="Invalid API key")
    return next(u for u, k in keys.items() if k == api_key)


# ── User data helpers ────────────────────────────────────────────────────────
def user_file(username: str) -> Path:
    return USER_DATA_DIR / f"{username}.json"


def load_user(username: str) -> dict[str, Any]:
    f = user_file(username)
    if f.exists():
        return cast(dict[str, Any], json.loads(f.read_text()))
    return {"history": [], "search_cache": {}, "liked": [], "playlists": {}}


def save_user(username: str, data: dict[str, Any]) -> None:
    user_file(username).write_text(json.dumps(data, indent=2))


def record_play(username: str, track: dict[str, Any]) -> None:
    data = load_user(username)
    data["history"] = [t for t in data["history"] if t.get("id") != track.get("id")]
    data["history"].insert(0, track)
    data["history"] = data["history"][:200]
    save_user(username, data)


# ── yt-dlp param builders ────────────────────────────────────────────────────
def _search_params() -> Any:
    return cast(Any, {"quiet": True, "no_warnings": True, "extract_flat": True})

def _info_params() -> Any:
    return cast(Any, {"quiet": True, "no_warnings": True, "format": "bestaudio/best"})

def _related_params() -> Any:
    return cast(Any, {"quiet": True, "no_warnings": True, "extract_flat": True, "playlistend": 15})


# ── yt-dlp sync helpers (run in thread pool) ─────────────────────────────────
def _entry_to_track(e: dict[str, Any]) -> dict[str, Any]:
    vid_id: str = e.get("id") or ""
    return {
        "id": vid_id,
        "title": e.get("title"),
        "channel": e.get("uploader") or e.get("channel"),
        "duration": e.get("duration"),
        "thumbnail": e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
        "url": f"https://www.youtube.com/watch?v={vid_id}",
    }


def _sync_search(query: str) -> list[dict[str, Any]]:
    with yt_dlp.YoutubeDL(_search_params()) as ydl:
        raw = ydl.extract_info(f"ytsearch10:{query}", download=False)
        info: dict[str, Any] = raw if isinstance(raw, dict) else {}
        entries: list[dict[str, Any]] = info.get("entries") or []
        return [_entry_to_track(e) for e in entries if e.get("id")]


def _sync_audio_url(video_id: str) -> tuple[str, dict[str, Any]]:
    """Returns (audio_url, track_info) together to avoid a second network call."""
    with yt_dlp.YoutubeDL(_info_params()) as ydl:
        raw = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        info: dict[str, Any] = raw if isinstance(raw, dict) else {}
        formats: list[dict[str, Any]] = info.get("formats") or []
        audio_url = ""
        for fmt in sorted(formats, key=lambda x: x.get("abr") or 0, reverse=True):
            if fmt.get("acodec") != "none" and fmt.get("url"):
                audio_url = str(fmt["url"])
                break
        if not audio_url:
            raise ValueError("No audio format found")
        desc: str = info.get("description") or ""
        track_info: dict[str, Any] = {
            "id": info.get("id"),
            "title": info.get("title"),
            "channel": info.get("uploader"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "description": desc[:300],
        }
        return audio_url, track_info


def _sync_related(video_id: str) -> list[dict[str, Any]]:
    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    with yt_dlp.YoutubeDL(_related_params()) as ydl:
        raw = ydl.extract_info(mix_url, download=False)
        info: dict[str, Any] = raw if isinstance(raw, dict) else {}
        entries: list[dict[str, Any]] = info.get("entries") or []
        return [
            _entry_to_track(e)
            for e in entries
            if e.get("id") and e.get("id") != video_id
        ][:12]


# ── Async wrappers with caching ──────────────────────────────────────────────
async def _run(fn: Any, *args: Any) -> Any:
    """Run a sync function in the default thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


async def fetch_stream(video_id: str) -> tuple[str, dict[str, Any]]:
    """
    Returns (audio_url, track_info).
    - Checks in-memory audio URL cache first (TTL = 4 h)
    - Deduplicates concurrent requests for the same video_id via asyncio.Event
    - Populates track_info cache as a side-effect
    """
    # Cache hit
    cached = _audio_url_cache.get(video_id)
    if cached and (time.time() - cached["ts"]) < AUDIO_URL_TTL:
        track = _track_info_cache.get(video_id, {}).get("info") or {}
        return cached["url"], track

    # Deduplication: if another coroutine is already fetching this id, wait for it
    if video_id in _audio_url_inflight:
        await _audio_url_inflight[video_id].wait()
        cached = _audio_url_cache.get(video_id)
        if cached:
            track = _track_info_cache.get(video_id, {}).get("info") or {}
            return cached["url"], track

    event = asyncio.Event()
    _audio_url_inflight[video_id] = event
    try:
        audio_url, track_info = await _run(_sync_audio_url, video_id)
        _audio_url_cache[video_id] = {"url": audio_url, "ts": time.time()}
        _track_info_cache[video_id] = {"info": track_info, "ts": time.time()}
        return audio_url, track_info
    finally:
        event.set()
        _audio_url_inflight.pop(video_id, None)


async def fetch_related(video_id: str) -> list[dict[str, Any]]:
    cached = _related_cache.get(video_id)
    if cached and (time.time() - cached["ts"]) < RELATED_CACHE_TTL:
        return cast(list[dict[str, Any]], cached["related"])
    try:
        related = await _run(_sync_related, video_id)
    except DownloadError:
        related = []
    _related_cache[video_id] = {"related": related, "ts": time.time()}
    return related


async def fetch_track_info(video_id: str) -> dict[str, Any]:
    cached = _track_info_cache.get(video_id)
    if cached and (time.time() - cached["ts"]) < AUDIO_URL_TTL:
        return cast(dict[str, Any], cached["info"])
    # Re-use fetch_stream to avoid double network call
    _, info = await fetch_stream(video_id)
    return info


# ── Search cache (per-user, persisted) ───────────────────────────────────────
async def cached_search(username: str, query: str) -> list[dict[str, Any]]:
    data = load_user(username)
    cache: dict[str, Any] = data.get("search_cache", {})
    key = query.lower().strip()
    entry: dict[str, Any] | None = cache.get(key)
    if entry and (time.time() - entry["ts"]) < SEARCH_CACHE_TTL:
        return cast(list[dict[str, Any]], entry["results"])

    results = await _run(_sync_search, query)
    cache[key] = {"ts": time.time(), "results": results}
    if len(cache) > 50:
        cache = dict(sorted(cache.items(), key=lambda x: x[1]["ts"])[-50:])
    data["search_cache"] = cache
    save_user(username, data)
    return results


# ── Background prefetch ───────────────────────────────────────────────────────
async def prefetch_next(video_ids: list[str]) -> None:
    """Fire-and-forget: warm up audio URL cache for upcoming tracks."""
    for vid in video_ids[:3]:
        if vid not in _audio_url_cache:
            asyncio.create_task(_prefetch_one(vid))


async def _prefetch_one(video_id: str) -> None:
    try:
        await fetch_stream(video_id)
    except Exception:  # noqa: BLE001 — prefetch failures are silent
        pass


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def web_ui() -> HTMLResponse:
    return HTMLResponse(Path("player.html").read_text())


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1),
    username: str = Depends(get_user),
) -> dict[str, Any]:
    results = await cached_search(username, q)
    return {"results": results}


@app.get("/api/stream/{video_id}")
async def stream_audio(
    video_id: str,
    next_ids: str = Query(default=""),   # comma-separated next track IDs for prefetch
    username: str = Depends(get_user),
) -> dict[str, Any]:
    try:
        audio_url, track_info = await fetch_stream(video_id)
        record_play(username, track_info)
        # Prefetch next tracks in background
        if next_ids:
            await prefetch_next([v for v in next_ids.split(",") if v])
        return {"stream_url": audio_url, "track": track_info}
    except (ValueError, DownloadError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/suggestions")
async def suggestions(username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    history: list[dict[str, Any]] = data.get("history", [])
    if not history:
        results = await cached_search(username, "trending music 2025")
        return {"suggestions": results, "based_on": "trending"}
    seed = history[0]
    related = await fetch_related(seed["id"])
    if not related:
        channel: str = seed.get("channel") or "music"
        related = await cached_search(username, f"{channel} music")
    return {"suggestions": related, "based_on": seed.get("title", "recent")}


@app.get("/api/history")
async def get_history(username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    return {"history": data.get("history", [])[:50]}


@app.post("/api/like/{video_id}")
async def like_track(video_id: str, username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    liked: list[str] = data.get("liked", [])
    if video_id not in liked:
        liked.insert(0, video_id)
    data["liked"] = liked[:500]
    save_user(username, data)
    return {"liked": True}


@app.delete("/api/like/{video_id}")
async def unlike_track(video_id: str, username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    data["liked"] = [v for v in data.get("liked", []) if v != video_id]
    save_user(username, data)
    return {"liked": False}


@app.get("/api/liked")
async def get_liked(username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    return {"liked": data.get("liked", [])}


@app.get("/api/track/{video_id}")
async def track_info(
    video_id: str,
    username: str = Depends(get_user),  # noqa: ARG001
) -> dict[str, Any]:
    return await fetch_track_info(video_id)


@app.get("/api/me")
async def me(username: str = Depends(get_user)) -> dict[str, str]:
    return {"username": username}


# ── Cache stats (debug) ───────────────────────────────────────────────────────
@app.get("/api/cache/stats")
async def cache_stats(username: str = Depends(get_user)) -> dict[str, Any]:  # noqa: ARG001
    return {
        "audio_url_cache": len(_audio_url_cache),
        "track_info_cache": len(_track_info_cache),
        "related_cache": len(_related_cache),
        "inflight": list(_audio_url_inflight.keys()),
    }


# ── Playlist routes ───────────────────────────────────────────────────────────

@app.get("/api/playlists")
async def list_playlists(username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    playlists: dict[str, Any] = data.get("playlists", {})
    # Return metadata only (id, name, count, thumbnail of first track)
    summary = []
    for pid, pl in playlists.items():
        tracks: list[dict[str, Any]] = pl.get("tracks", [])
        summary.append({
            "id": pid,
            "name": pl.get("name", "Untitled"),
            "count": len(tracks),
            "thumbnail": tracks[0].get("thumbnail") if tracks else None,
            "created_at": pl.get("created_at", 0),
        })
    summary.sort(key=lambda x: x["created_at"], reverse=True)
    return {"playlists": summary}


@app.post("/api/playlists")
async def create_playlist(
    name: str = Body(..., embed=True),
    username: str = Depends(get_user),
) -> dict[str, Any]:
    data = load_user(username)
    playlists: dict[str, Any] = data.setdefault("playlists", {})
    pid = secrets.token_urlsafe(8)
    playlists[pid] = {"name": name, "tracks": [], "created_at": time.time()}
    save_user(username, data)
    return {"id": pid, "name": name}


@app.get("/api/playlists/{playlist_id}")
async def get_playlist(playlist_id: str, username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    pl = data.get("playlists", {}).get(playlist_id)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"id": playlist_id, **pl}


@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: str, username: str = Depends(get_user)) -> dict[str, Any]:
    data = load_user(username)
    playlists: dict[str, Any] = data.get("playlists", {})
    if playlist_id not in playlists:
        raise HTTPException(status_code=404, detail="Playlist not found")
    del playlists[playlist_id]
    save_user(username, data)
    return {"deleted": True}


@app.patch("/api/playlists/{playlist_id}")
async def rename_playlist(
    playlist_id: str,
    name: str = Body(..., embed=True),
    username: str = Depends(get_user),
) -> dict[str, Any]:
    data = load_user(username)
    pl = data.get("playlists", {}).get(playlist_id)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    pl["name"] = name
    save_user(username, data)
    return {"id": playlist_id, "name": name}


@app.post("/api/playlists/{playlist_id}/tracks")
async def add_to_playlist(
    playlist_id: str,
    track: dict[str, Any] = Body(...),
    username: str = Depends(get_user),
) -> dict[str, Any]:
    data = load_user(username)
    pl = data.get("playlists", {}).get(playlist_id)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    tracks: list[dict[str, Any]] = pl.setdefault("tracks", [])
    # Avoid duplicates
    if not any(t.get("id") == track.get("id") for t in tracks):
        tracks.append(track)
    save_user(username, data)
    return {"count": len(tracks)}


@app.delete("/api/playlists/{playlist_id}/tracks/{video_id}")
async def remove_from_playlist(
    playlist_id: str,
    video_id: str,
    username: str = Depends(get_user),
) -> dict[str, Any]:
    data = load_user(username)
    pl = data.get("playlists", {}).get(playlist_id)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    pl["tracks"] = [t for t in pl.get("tracks", []) if t.get("id") != video_id]
    save_user(username, data)
    return {"count": len(pl["tracks"])}


@app.put("/api/playlists/{playlist_id}/tracks")
async def reorder_playlist(
    playlist_id: str,
    track_ids: list[str] = Body(...),
    username: str = Depends(get_user),
) -> dict[str, Any]:
    """Reorder tracks by providing an ordered list of video IDs."""
    data = load_user(username)
    pl = data.get("playlists", {}).get(playlist_id)
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    tracks: list[dict[str, Any]] = pl.get("tracks", [])
    track_map = {t["id"]: t for t in tracks}
    pl["tracks"] = [track_map[tid] for tid in track_ids if tid in track_map]
    save_user(username, data)
    return {"count": len(pl["tracks"])}


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("🎵 Pi Music Server v2 starting on http://0.0.0.0:8080")
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
