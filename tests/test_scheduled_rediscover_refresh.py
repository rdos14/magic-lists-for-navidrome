import importlib.util
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

if "apscheduler" not in sys.modules and importlib.util.find_spec("apscheduler") is None:
    apscheduler = types.ModuleType("apscheduler")
    apscheduler.__path__ = []
    schedulers = types.ModuleType("apscheduler.schedulers")
    schedulers.__path__ = []
    scheduler_asyncio = types.ModuleType("apscheduler.schedulers.asyncio")
    setattr(scheduler_asyncio, "AsyncIOScheduler", Mock)
    sys.modules.update({
        "apscheduler": apscheduler,
        "apscheduler.schedulers": schedulers,
        "apscheduler.schedulers.asyncio": scheduler_asyncio,
    })

if "aiosqlite" not in sys.modules and importlib.util.find_spec("aiosqlite") is None:
    sys.modules["aiosqlite"] = types.ModuleType("aiosqlite")

from backend import main


class ScheduledRediscoverRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def run_refresh(self, refresh_function):
        playlist_id = "scheduled-playlist"
        tracks = [
            {
                "id": f"track-{i}",
                "title": f"Track {i}",
                "ai_curated": True,
                "ai_reasoning": "Phase 2 selected these tracks.",
            }
            for i in range(50)
        ]
        playlist = {
            "navidrome_playlist_id": playlist_id,
            "playlist_length": 50,
            "songs": [],
        }
        scheduled = SimpleNamespace(
            id=1,
            navidrome_playlist_id=playlist_id,
            refresh_frequency="monthly",
            library_id=None,
        )
        db = Mock()
        db.get_all_playlists_with_schedule_info = AsyncMock(return_value=[playlist])
        db.get_or_create_user_id = AsyncMock(return_value="user-id")
        db.update_playlist_content = AsyncMock()
        db.update_scheduled_playlist_next_refresh = AsyncMock()
        nav_client = Mock(base_url="http://navidrome.test")
        nav_client.update_playlist = AsyncMock()
        processor = Mock()
        processor.generate_playlist = AsyncMock(return_value={
            "tracks": tracks,
            "reasoning": "Phase 1 strategy summary.",
        })

        with (
            patch.object(main, "get_navidrome_client", return_value=nav_client),
            patch.object(main, "get_ai_client", return_value=Mock()),
            patch.object(main, "ReDiscoverV2Processor", return_value=processor),
            patch.object(main, "reconcile_scheduled_playlist_id", new_callable=AsyncMock),
            patch.object(main, "calculate_next_refresh", return_value=datetime(2026, 12, 1)),
            patch.object(main.scheduler_logger, "info") as info,
            patch.object(main.scheduler_logger, "warning") as warning,
        ):
            await refresh_function(scheduled, db)

        return processor, info, warning

    async def test_scheduled_refresh_passes_saved_playlist_length(self):
        for refresh_function in (
            main.refresh_rediscover_playlist,
            main.refresh_rediscover_v2_playlist,
        ):
            with self.subTest(refresh_function=refresh_function.__name__):
                processor, _, _ = await self.run_refresh(refresh_function)
                processor.generate_playlist.assert_awaited_once_with(
                    "user-id", "http://navidrome.test", None, track_count=50
                )

    async def test_phase_two_ai_tracks_are_not_logged_as_algorithmic_fallback(self):
        for refresh_function in (
            main.refresh_rediscover_playlist,
            main.refresh_rediscover_v2_playlist,
        ):
            with self.subTest(refresh_function=refresh_function.__name__):
                _, info, warning = await self.run_refresh(refresh_function)
                messages = [str(call.args[0]) for call in info.call_args_list + warning.call_args_list]
                self.assertTrue(any("AI curation applied" in message for message in messages))
                self.assertFalse(any("algorithmic selection" in message.lower() or "fallback/algorithmic" in message.lower() for message in messages))


if __name__ == "__main__":
    unittest.main()
