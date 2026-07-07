from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi import Query
import uvicorn
import os
import logging
import logging.handlers
from typing import List, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio

# Load environment variables first
load_dotenv()

# Get log level from environment (ERROR=minimal, INFO=normal, DEBUG=verbose)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Configure logging for scheduler activities with rotation
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            'scheduler.log',
            maxBytes=5*1024*1024,  # 5MB per file
            backupCount=2,         # Keep 2 old files (total ~10MB)
            encoding='utf-8'
        ),
        logging.StreamHandler()  # Also log to console
    ]
)

# Create a specific logger for scheduler activities
scheduler_logger = logging.getLogger('scheduler')

# Reduce httpx logging verbosity to avoid cluttering scheduler.log
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

from .navidrome_client import NavidromeClient
from .ai_client import AIClient
from .database import DatabaseManager, get_db
from .schemas import CreatePlaylistRequest, CreateGenrePlaylistRequest, Playlist, RediscoverWeeklyResponse, RediscoverWeeklyV2Response, CreateRediscoverPlaylistRequest, PlaylistWithScheduleInfo
from .recipe_manager import recipe_manager
from .rediscover import RediscoverWeekly, ReDiscoverV2Processor
from .track_scoring import filter_tracks_for_this_is_playlist
# SYSTEM CHECK FEATURE - START
from .services.health_check_service import HealthCheckService
# SYSTEM CHECK FEATURE - END

app = FastAPI(title="MagicLists Navidrome MVP")

@app.on_event("startup")
async def startup_event():
    """Initialize scheduler on app startup"""
    global scheduler, system_check_passed, system_check_results
    scheduler = AsyncIOScheduler()
    scheduler.start()
    scheduler_logger.info("✅ Scheduler started successfully")
    # Auto-start the cron job
    await start_scheduler_job()
    scheduler_logger.info("✅ Cron job auto-started on application startup")
    
    # SYSTEM CHECK FEATURE - START
    # Run system checks on startup
    try:
        health_service = HealthCheckService()
        system_check_results = await health_service.run_checks()
        system_check_passed = system_check_results.get("all_passed", False)
        
        if system_check_passed:
            scheduler_logger.info("✅ System health checks passed on startup")
        else:
            scheduler_logger.warning("⚠️ System health checks failed on startup - user will be redirected to system check page")
            
        # Log individual check results with enhanced AI provider logging
        for check in system_check_results.get("checks", []):
            status_emoji = "✅" if check["status"] == "success" else "⚠️" if check["status"] == "warning" else "ℹ️" if check["status"] == "info" else "❌"
            
            # Enhanced logging for AI Provider checks
            if "AI Provider" in check["name"]:
                ai_provider = os.getenv("AI_PROVIDER", "openrouter")
                if check["status"] == "success":
                    # Extract model from success message (e.g., "service reachable (model: llama3.2)")
                    if "model:" in check["message"]:
                        model_part = check["message"].split("model: ")[1].rstrip(")")
                        scheduler_logger.info(f"🤖 AI Provider: {ai_provider.title()} with model '{model_part}' - Ready")
                    else:
                        scheduler_logger.info(f"🤖 AI Provider: {ai_provider.title()} - Ready")
                elif check["status"] == "warning":
                    if "not set" in check["message"]:
                        scheduler_logger.info(f"🤖 AI Provider: {ai_provider.title()} - No API key (using fallback algorithms)")
                    else:
                        scheduler_logger.warning(f"🤖 AI Provider: {ai_provider.title()} - {check['message']}")
                elif check["status"] == "error":
                    scheduler_logger.error(f"🤖 AI Provider: {ai_provider.title()} - {check['message']}")
            else:
                # Standard logging for other checks
                scheduler_logger.info(f"{status_emoji} {check['name']}: {check['status']}")
            
    except Exception as e:
        scheduler_logger.error(f"❌ Failed to run system checks on startup: {e}")
        system_check_passed = False
        system_check_results = {
            "all_passed": False,
            "checks": [{
                "name": "System Check Service",
                "status": "error", 
                "message": f"Failed to run health checks: {str(e)}",
                "suggestion": "Check application logs and restart the service"
            }]
        }
    # SYSTEM CHECK FEATURE - END

@app.on_event("shutdown") 
async def shutdown_event():
    """Cleanup scheduler on app shutdown"""
    global scheduler
    if scheduler:
        scheduler.shutdown()
        scheduler_logger.info("🛑 Scheduler shutdown completed")

# Mount static files
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# Templates
templates = Jinja2Templates(directory="frontend/templates")

# Initialize clients (lazy loading)
navidrome_client = None
ai_client = None

# Initialize scheduler (will be started on app startup)
scheduler = None

# SYSTEM CHECK FEATURE - START
# App state to track system check results
system_check_passed = False
system_check_results = None
# SYSTEM CHECK FEATURE - END

def get_navidrome_client():
    global navidrome_client
    if navidrome_client is None:
        navidrome_client = NavidromeClient()
    return navidrome_client

def get_ai_client():
    global ai_client
    if ai_client is None:
        ai_client = AIClient()
    return ai_client

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the main HTML page"""
    # SYSTEM CHECK FEATURE - START
    # Redirect to system check if checks haven't passed
    if not system_check_passed:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/system-check", status_code=302)
    # SYSTEM CHECK FEATURE - END
    
    return templates.TemplateResponse("index.html", {"request": request})

# SYSTEM CHECK FEATURE - START
@app.get("/system-check", response_class=HTMLResponse)
async def system_check_page(request: Request):
    """Serve the system check page"""
    return templates.TemplateResponse("index.html", {"request": request})
# SYSTEM CHECK FEATURE - END

@app.get("/api/artists")
async def get_artists(library_id: List[str] = Query(None)):
    """Get list of artists from Navidrome"""
    try:
        client = get_navidrome_client()
        artists = await client.get_artists(library_id)
        return artists
    except Exception as e:
        error_msg = str(e)
        if "Invalid username or password" in error_msg or "No authentication method available" in error_msg:
            raise HTTPException(status_code=401, detail=error_msg)
        elif "Network error" in error_msg or "connecting to Navidrome" in error_msg:
            raise HTTPException(status_code=503, detail=f"Cannot connect to Navidrome server: {error_msg}")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to fetch artists: {error_msg}")

@app.get("/api/genres")
async def get_genres(library_id: List[str] = Query(None)):
    """Get list of genres from Navidrome"""
    try:
        client = get_navidrome_client()
        genres = await client.get_genres(library_id)
        return genres
    except Exception as e:
        error_msg = str(e)
        if "Invalid username or password" in error_msg or "No authentication method available" in error_msg:
            raise HTTPException(status_code=401, detail=error_msg)
        elif "Network error" in error_msg or "connecting to Navidrome" in error_msg:
            raise HTTPException(status_code=503, detail=f"Cannot connect to Navidrome server: {error_msg}")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to fetch genres: {error_msg}")

@app.get("/api/music-folders")
async def get_music_folders():
    """Get list of music folders/libraries from Navidrome"""
    try:
        client = get_navidrome_client()
        folders = await client.get_music_folders()
        return folders
    except Exception as e:
        error_msg = str(e)
        if "Invalid username or password" in error_msg or "No authentication method available" in error_msg:
            raise HTTPException(status_code=401, detail=error_msg)
        elif "Network error" in error_msg or "connecting to Navidrome" in error_msg:
            raise HTTPException(status_code=503, detail=f"Cannot connect to Navidrome server: {error_msg}")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to fetch music folders: {error_msg}")


# SYSTEM CHECK FEATURE - START
@app.get("/api/health-check")
async def get_health_check():
    """Get system health check results"""
    global system_check_passed, system_check_results
    
    try:
        health_service = HealthCheckService()
        fresh_results = await health_service.run_checks()
        
        system_check_passed = fresh_results.get("all_passed", False)
        system_check_results = fresh_results
        
        if system_check_passed:
            scheduler_logger.info("✅ System health checks passed via API")
        else:
            scheduler_logger.warning("⚠️ System health checks failed via API")
        
        return fresh_results
        
    except Exception as e:
        scheduler_logger.error(f"❌ Failed to run health checks via API: {e}")
        error_results = {
            "all_passed": False,
            "checks": [{
                "name": "System Check Service",
                "status": "error",
                "message": f"Failed to run health checks: {str(e)}",
                "suggestion": "Check application logs and restart the service"
            }]
        }
        return error_results
# SYSTEM CHECK FEATURE - END


@app.post("/api/create_playlist", response_model=Playlist)
async def create_playlist(
    request: CreatePlaylistRequest,
    db: DatabaseManager = Depends(get_db)
):
    """Create an AI-curated 'This Is' playlist for a single artist"""
    try:
        nav_client = get_navidrome_client()
        ai_client_instance = get_ai_client()
        
        all_artists = await nav_client.get_artists()
        selected_artists = [a for a in all_artists if a["id"] in request.artist_ids]
        
        if not selected_artists:
            raise HTTPException(status_code=404, detail="Artists not found")
        
        if request.artist_ids:
            first_artist_id = request.artist_ids[0]
            selected_artists = [a for a in all_artists if a["id"] == first_artist_id]
            artist_names = [a["name"] for a in selected_artists]
        else:
            raise HTTPException(status_code=400, detail="At least one artist must be selected")

        playlist_name = request.playlist_name or f"This Is: {artist_names[0]}"
        
        all_tracks = []
        tracks = await nav_client.get_tracks_by_artist(first_artist_id, request.library_ids)
        if tracks:
            all_tracks.extend(tracks)
        
        if not all_tracks:
            raise HTTPException(status_code=404, detail="No tracks found for the selected artists")
        
        library_stats = await nav_client.get_library_stats()
        
        filtered_tracks, filter_metadata = filter_tracks_for_this_is_playlist(
            source_tracks=all_tracks,
            target_playlist_size=request.playlist_length,
            library_stats=library_stats
        )
        
        if filter_metadata['filtered']:
            scheduler_logger.info(f"🎯 Smart filtering applied: {filter_metadata['source_count']} → {filter_metadata['sent_count']} tracks (multiplier: {filter_metadata['threshold_multiplier']}x)")
            scheduler_logger.info(f"📊 Score range: {filter_metadata['score_range']['highest']:.1f} - {filter_metadata['score_range']['lowest']:.1f} (cutoff: {filter_metadata['score_range']['cutoff']:.1f})")
        else:
            scheduler_logger.info(f"✅ No filtering needed: {filter_metadata['source_count']} tracks below threshold")
        
        tracks_for_llm = filtered_tracks
        
        curation_result = await ai_client_instance.curate_this_is(
            artist_name=', '.join(artist_names),
            tracks_json=tracks_for_llm,
            num_tracks=request.playlist_length,
            include_reasoning=True
        )
        
        if isinstance(curation_result, tuple):
            curated_track_ids, reasoning = curation_result
        else:
            curated_track_ids = curation_result
            reasoning = ""

        if not curated_track_ids:
            if reasoning and "Playlist generation failed" in reasoning:
                scheduler_logger.error(f"❌ Playlist creation aborted: {reasoning}")
                raise HTTPException(status_code=400, detail=f"Playlist generation failed: {reasoning}")
            else:
                scheduler_logger.error(f"❌ AI curation returned no tracks for {', '.join(artist_names)}")
                raise HTTPException(status_code=500, detail="AI curation failed to return any tracks")

        if reasoning:
            reasoning_preview = reasoning[:200] + "..." if len(reasoning) > 200 else reasoning
            scheduler_logger.info(f"🎵 AI curation applied for {', '.join(artist_names)} (reasoning length: {len(reasoning)} chars): {reasoning_preview}")
        else:
            scheduler_logger.info(f"⚠️ No AI reasoning provided for {', '.join(artist_names)}")

        comment_to_use = reasoning if reasoning else None
        comment_preview = comment_to_use[:200] + "..." if comment_to_use and len(comment_to_use) > 200 else comment_to_use
        scheduler_logger.info(f"💬 Creating playlist with comment (length: {len(comment_to_use) if comment_to_use else 0}): {comment_preview}")

        navidrome_playlist_id = await nav_client.create_playlist(
            name=playlist_name,
            track_ids=curated_track_ids,
            comment=comment_to_use
        )
        
        track_titles = []
        track_id_to_title = {track["id"]: track["title"] for track in all_tracks}
        for track_id in curated_track_ids:
            if track_id in track_id_to_title:
                track_titles.append(track_id_to_title[track_id])
        
        playlist = await db.create_playlist(
            artist_id=request.artist_ids[0],
            playlist_name=playlist_name,
            songs=track_titles,
            reasoning=reasoning,
            navidrome_playlist_id=navidrome_playlist_id,
            playlist_length=request.playlist_length,
            library_ids=request.library_ids
        )
        
        if request.refresh_frequency not in ["none", "never"]:
            next_refresh = calculate_next_refresh(request.refresh_frequency)
            
            await db.create_scheduled_playlist(
                playlist_type="this_is",
                navidrome_playlist_id=navidrome_playlist_id,
                refresh_frequency=request.refresh_frequency,
                next_refresh=next_refresh
            )
            
            schedule_playlist_refresh()
            scheduler_logger.info(f"📅 Scheduled {request.refresh_frequency} refresh for This Is playlist: {playlist_name}")
        
        playlist_dict = playlist.dict() if hasattr(playlist, 'dict') else playlist.__dict__
        playlist_dict["navidrome_playlist_id"] = navidrome_playlist_id
        playlist_dict["refresh_frequency"] = request.refresh_frequency
        
        if request.refresh_frequency != "none":
            playlist_dict["next_refresh"] = calculate_next_refresh(request.refresh_frequency).isoformat()
        
        return playlist_dict
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create playlist: {str(e)}")

@app.post("/api/create_playlist_with_reasoning")
async def create_playlist_with_reasoning(
    request: CreatePlaylistRequest,
    db: DatabaseManager = Depends(get_db)
):
    """Create an AI-curated 'This Is' playlist with AI reasoning explanation"""
    try:
        nav_client = get_navidrome_client()
        ai_client_instance = get_ai_client()
        
        artists = await nav_client.get_artists()
        if not request.artist_ids or len(request.artist_ids) == 0:
            raise HTTPException(status_code=400, detail="At least one artist must be selected")
        first_artist_id = request.artist_ids[0]
        artist = next((a for a in artists if a["id"] == first_artist_id), None)
        
        if not artist:
            raise HTTPException(status_code=404, detail="Artist not found")
        
        artist_name = artist["name"]
        playlist_name = getattr(request, 'playlist_name', None) or f"This Is: {artist_name}"
        
        tracks = await nav_client.get_tracks_by_artist(first_artist_id)
        
        if not tracks:
            raise HTTPException(status_code=404, detail="No tracks found for this artist")
        
        curated_track_ids, reasoning = await ai_client_instance.curate_this_is(
            artist_name=artist_name,
            tracks_json=tracks,
            num_tracks=20,
            include_reasoning=True
        )

        navidrome_playlist_id = await nav_client.create_playlist(
            name=playlist_name,
            track_ids=curated_track_ids,
            comment=reasoning if reasoning else None
        )
        
        track_titles = []
        track_id_to_title = {track["id"]: track["title"] for track in tracks}
        for track_id in curated_track_ids:
            if track_id in track_id_to_title:
                track_titles.append(track_id_to_title[track_id])
        
        playlist = await db.create_playlist(
            artist_id=first_artist_id,
            playlist_name=playlist_name,
            songs=track_titles,
            navidrome_playlist_id=navidrome_playlist_id
        )
        
        playlist_dict = playlist.dict() if hasattr(playlist, 'dict') else playlist.__dict__
        playlist_dict["navidrome_playlist_id"] = navidrome_playlist_id
        playlist_dict["ai_reasoning"] = reasoning
        
        return playlist_dict
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create playlist with reasoning: {str(e)}")

@app.post("/api/create_genre_playlist", response_model=Playlist)
async def create_genre_playlist(
    request: CreateGenrePlaylistRequest,
    db: DatabaseManager = Depends(get_db)
):
    """Create an AI-curated 'Genre Mix' playlist for a specific genre"""
    try:
        nav_client = get_navidrome_client()
        ai_client_instance = get_ai_client()

        playlist_name = request.playlist_name or f"Genre Mix: {request.genre}"

        all_tracks = await nav_client.get_tracks_by_genre(request.genre, request.library_ids)
        scheduler_logger.info(f"🎵 Found {len(all_tracks)} total tracks for genre '{request.genre}'")

        if not all_tracks:
            raise HTTPException(status_code=404, detail=f"No tracks found for genre: {request.genre}")

        library_stats = await nav_client.get_library_stats()

        filtered_tracks, filter_metadata = filter_tracks_for_this_is_playlist(
            source_tracks=all_tracks,
            target_playlist_size=request.playlist_length,
            library_stats=library_stats
        )

        if filter_metadata['filtered']:
            scheduler_logger.info(f"🎯 Smart filtering applied: {filter_metadata['source_count']} → {filter_metadata['sent_count']} tracks (multiplier: {filter_metadata['threshold_multiplier']}x)")
            scheduler_logger.info(f"📊 Score range: {filter_metadata['score_range']['highest']:.1f} - {filter_metadata['score_range']['lowest']:.1f} (cutoff: {filter_metadata['score_range']['cutoff']:.1f})")
        else:
            scheduler_logger.info(f"✅ No filtering needed: {filter_metadata['source_count']} tracks below threshold")

        tracks_for_llm = filtered_tracks

        curation_result = await ai_client_instance.curate_genre_mix(
            genre=request.genre,
            tracks_json=tracks_for_llm,
            num_tracks=request.playlist_length,
            include_reasoning=True
        )

        if isinstance(curation_result, tuple):
            curated_track_ids, reasoning = curation_result
        else:
            curated_track_ids = curation_result
            reasoning = ""

        if not curated_track_ids:
            if reasoning and "Playlist generation failed" in reasoning:
                scheduler_logger.error(f"❌ Playlist creation aborted: {reasoning}")
                raise HTTPException(status_code=400, detail=f"Playlist generation failed: {reasoning}")
            else:
                scheduler_logger.error(f"❌ AI curation returned no tracks for {request.genre}")
                raise HTTPException(status_code=500, detail="AI curation failed to return any tracks")

        if reasoning:
            reasoning_preview = reasoning[:200] + "..." if len(reasoning) > 200 else reasoning
            scheduler_logger.info(f"🎵 AI curation applied for {request.genre} (reasoning length: {len(reasoning)} chars): {reasoning_preview}")
        else:
            scheduler_logger.info(f"⚠️ No AI reasoning provided for {request.genre}")

        comment_to_use = reasoning if reasoning else None
        comment_preview = comment_to_use[:200] + "..." if comment_to_use and len(comment_to_use) > 200 else comment_to_use
        scheduler_logger.info(f"💬 Creating playlist with comment (length: {len(comment_to_use) if comment_to_use else 0}): {comment_preview}")

        navidrome_playlist_id = await nav_client.create_playlist(
            name=playlist_name,
            track_ids=curated_track_ids,
            comment=comment_to_use
        )

        track_titles = []
        track_id_to_title = {track["id"]: track["title"] for track in all_tracks}
        for track_id in curated_track_ids:
            if track_id in track_id_to_title:
                track_titles.append(track_id_to_title[track_id])

        playlist = await db.create_playlist(
            artist_id=request.genre,
            playlist_name=playlist_name,
            songs=track_titles,
            reasoning=reasoning,
            navidrome_playlist_id=navidrome_playlist_id,
            playlist_length=request.playlist_length,
            library_ids=request.library_ids
        )

        if request.refresh_frequency not in ["none", "never"]:
            next_refresh = calculate_next_refresh(request.refresh_frequency)

            await db.create_scheduled_playlist(
                playlist_type="genre_mix",
                navidrome_playlist_id=navidrome_playlist_id,
                refresh_frequency=request.refresh_frequency,
                next_refresh=next_refresh
            )

        return playlist

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create genre playlist: {str(e)}")

@app.get("/api/rediscover-weekly", response_model=RediscoverWeeklyResponse)
async def get_rediscover_weekly():
    """Generate Re-Discover Weekly playlist based on listening history"""
    try:
        nav_client = get_navidrome_client()
        rediscover = RediscoverWeekly(nav_client)
        tracks = await rediscover.generate_rediscover_weekly(use_ai=True)
        
        ai_curated = tracks[0].get("ai_curated", False) if tracks else False
        message = f"Generated Re-Discover Weekly with {len(tracks)} tracks"
        if ai_curated:
            message += " (AI curated)"
        else:
            message += " (algorithmic selection)"
        
        return RediscoverWeeklyResponse(
            tracks=tracks,
            total_tracks=len(tracks),
            message=message
        )
        
    except Exception as e:
        error_msg = str(e)
        if "No listening history found" in error_msg:
            raise HTTPException(status_code=404, detail="No listening history found. Make sure you've played some music in Navidrome.")
        elif "No tracks found for re-discovery" in error_msg:
            raise HTTPException(status_code=404, detail="No tracks found for re-discovery. Try listening to more music first.")
        elif "Invalid username or password" in error_msg or "No authentication method available" in error_msg:
            raise HTTPException(status_code=401, detail=error_msg)
        elif "Network error" in error_msg or "connecting to Navidrome" in error_msg:
            raise HTTPException(status_code=503, detail=f"Cannot connect to Navidrome server: {error_msg}")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to generate Re-Discover Weekly: {error_msg}")

@app.get("/api/rediscover-weekly-v2", response_model=RediscoverWeeklyV2Response)
async def get_rediscover_weekly_v2(library_ids: Optional[List[str]] = Query(None), db: DatabaseManager = Depends(get_db)):
    """Generate Re-Discover Weekly v2.0 playlist using temporal analysis and two-phase AI"""
    try:
        nav_client = get_navidrome_client()
        ai_client = get_ai_client()

        user_id = await db.get_or_create_user_id()
        server_id = nav_client.base_url or "unknown_server"

        processor = ReDiscoverV2Processor(nav_client, ai_client, db)
        result = await processor.generate_playlist(user_id, server_id, library_ids)

        return RediscoverWeeklyV2Response(**result)

    except Exception as e:
        error_msg = str(e)
        if "Insufficient listening history" in error_msg:
            raise HTTPException(status_code=404, detail="Insufficient listening history. Star favorites and listen regularly. Check back in 2-3 weeks!")
        elif "Invalid username or password" in error_msg or "No authentication method available" in error_msg:
            raise HTTPException(status_code=401, detail=error_msg)
        elif "Network error" in error_msg or "connecting to Navidrome" in error_msg:
            raise HTTPException(status_code=503, detail=f"Cannot connect to Navidrome server: {error_msg}")
        else:
            raise HTTPException(status_code=500, detail=f"Failed to generate Re-Discover Weekly v2.0: {error_msg}")

@app.post("/api/create-rediscover-playlist-v2")
async def create_rediscover_playlist_v2(
    request: CreateRediscoverPlaylistRequest,
    db: DatabaseManager = Depends(get_db)
):
    """Create a Re-Discover Weekly v2.0 playlist in Navidrome"""
    try:
        scheduler_logger.info(f"🎵 Starting Re-Discover v2.0 playlist creation with length {request.playlist_length}, library_ids: {request.library_ids}")

        nav_client = get_navidrome_client()
        ai_client = get_ai_client()

        user_id = await db.get_or_create_user_id()
        server_id = nav_client.base_url or "unknown_server"

        processor = ReDiscoverV2Processor(nav_client, ai_client, db)

        playlist_data = await processor.generate_playlist(user_id, server_id, request.library_ids, request.playlist_length)
        tracks = playlist_data.get("tracks", [])

        if not tracks:
            scheduler_logger.error("❌ No tracks generated for Re-Discover Weekly v2.0")
            raise HTTPException(status_code=404, detail="No tracks found for Re-Discover Weekly v2.0")

        scheduler_logger.info(f"✅ Generated {len(tracks)} tracks for Re-Discover Weekly v2.0")

        # Read reasoning at result level first, fall back to per-track
        ai_reasoning = playlist_data.get("reasoning", "")
        ai_curated = any(track.get("ai_curated", False) for track in tracks)

        if not ai_reasoning:
            ai_reasoning = next(
                (track.get("ai_reasoning", "") for track in tracks if track.get("ai_curated", False) and track.get("ai_reasoning")),
                ""
            )

        scheduler_logger.info(f"🎵 AI curated: {ai_curated}, reasoning length: {len(ai_reasoning)}")

        if ai_reasoning and ai_curated:
            reasoning_preview = ai_reasoning[:200] + "..." if len(ai_reasoning) > 200 else ai_reasoning
            scheduler_logger.info(f"🎵 AI curation applied for Re-Discover Weekly v2.0 (reasoning length: {len(ai_reasoning)} chars): {reasoning_preview}")
        else:
            scheduler_logger.info(f"⚠️ Re-Discover Weekly v2.0 used fallback strategy")

        frequency_names = {
            "daily": "Re-Discover Daily ✨",
            "weekly": "Re-Discover Weekly ✨",
            "monthly": "Re-Discover Monthly ✨",
            "never": "Re-Discover ✨"
        }
        playlist_name = frequency_names.get(request.refresh_frequency, "Re-Discover Weekly ✨")
        if playlist_data.get("is_fallback"):
            playlist_name += " (Fallback)"
        scheduler_logger.info(f"📝 Creating playlist: {playlist_name}")

        track_ids = [track["id"] for track in tracks]
        scheduler_logger.info(f"🎵 Track IDs: {track_ids[:5]}... (total: {len(track_ids)})")

        comment_to_use = ai_reasoning if ai_reasoning else f"Theme: {playlist_data.get('theme', 'Mixed')}"
        comment_preview = comment_to_use[:200] + "..." if len(comment_to_use) > 200 else comment_to_use
        scheduler_logger.info(f"💬 Creating Re-Discover v2.0 playlist with comment (length: {len(comment_to_use)}): {comment_preview}")

        scheduler_logger.info("🎵 Calling nav_client.create_playlist...")
        navidrome_playlist_id = await nav_client.create_playlist(
            name=playlist_name,
            track_ids=track_ids,
            comment=comment_to_use
        )
        scheduler_logger.info(f"✅ Navidrome playlist created: {navidrome_playlist_id}")

        track_titles = [track.get("title", "Unknown") for track in tracks]
        scheduler_logger.info(f"📊 Storing {len(track_titles)} track titles in database")

        playlist_record = await db.create_playlist(
            artist_id="rediscover_v2",
            playlist_name=playlist_name,
            songs=track_titles,
            reasoning=ai_reasoning,
            navidrome_playlist_id=navidrome_playlist_id,
            playlist_length=len(tracks),
            library_ids=request.library_ids
        )
        scheduler_logger.info(f"💾 Database playlist created: {playlist_record}")

        if request.refresh_frequency != "never":
            scheduler_logger.info(f"⏰ Setting up {request.refresh_frequency} refresh schedule")
            scheduled_playlist = await db.create_scheduled_playlist(
                playlist_type="rediscover_weekly_v2",
                navidrome_playlist_id=navidrome_playlist_id,
                refresh_frequency=request.refresh_frequency,
                next_refresh=calculate_next_refresh(request.refresh_frequency)
            )
            scheduler_logger.info(f"✅ Scheduled playlist created: {scheduled_playlist}")
        else:
            scheduler_logger.info("⏰ No scheduling requested (refresh_frequency='never')")

        return {
            "message": f"Re-Discover Weekly v2.0 playlist created successfully with {len(tracks)} tracks",
            "playlist_id": navidrome_playlist_id,
            "track_count": len(tracks),
            "theme": playlist_data.get("theme", "Mixed"),
            "mode": playlist_data.get("mode", "Unknown"),
            "is_fallback": playlist_data.get("is_fallback", False)
        }

    except HTTPException:
        raise
    except Exception as e:
        scheduler_logger.error(f"❌ Failed to create Re-Discover Weekly v2.0 playlist: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create Re-Discover Weekly v2.0 playlist: {str(e)}")

@app.post("/api/create-rediscover-playlist")
async def create_rediscover_playlist(
    request: CreateRediscoverPlaylistRequest,
    db: DatabaseManager = Depends(get_db)
):
    """Create a Re-Discover Weekly playlist in Navidrome"""
    try:
        scheduler_logger.info(f"🎵 Starting Re-Discover playlist creation with length {request.playlist_length}, library_ids: {request.library_ids}")

        nav_client = get_navidrome_client()
        rediscover = RediscoverWeekly(nav_client)

        scheduler_logger.info("🎵 Generating rediscover tracks...")
        tracks = await rediscover.generate_rediscover_weekly(max_tracks=request.playlist_length, use_ai=True, library_id=request.library_ids[0] if request.library_ids else "", variety_context="")
        scheduler_logger.info(f"🎵 Generated {len(tracks) if tracks else 0} tracks")
        
        if not tracks:
            scheduler_logger.error("❌ No tracks generated for Re-Discover Weekly")
            raise HTTPException(status_code=404, detail="No tracks found for Re-Discover Weekly")

        scheduler_logger.info(f"✅ Generated {len(tracks)} tracks for Re-Discover Weekly")

        ai_reasoning = ""
        ai_curated = False
        if tracks:
            first_track = tracks[0]
            ai_reasoning = first_track.get("ai_reasoning", "")
            ai_curated = first_track.get("ai_curated", False)
            scheduler_logger.info(f"🎵 AI curated: {ai_curated}, reasoning length: {len(ai_reasoning)}")
        
        if ai_reasoning and ai_curated:
            reasoning_preview = ai_reasoning[:200] + "..." if len(ai_reasoning) > 200 else ai_reasoning
            scheduler_logger.info(f"🎵 AI curation applied for Re-Discover Weekly (reasoning length: {len(ai_reasoning)} chars): {reasoning_preview}")
        else:
            scheduler_logger.info(f"⚠️ Re-Discover Weekly used algorithmic selection (no AI reasoning)")
        
        frequency_names = {
            "daily": "Re-Discover Daily ✨",
            "weekly": "Re-Discover Weekly ✨",
            "monthly": "Re-Discover Monthly ✨",
            "never": "Re-Discover ✨"
        }
        playlist_name = frequency_names.get(request.refresh_frequency, "Re-Discover Weekly ✨")
        scheduler_logger.info(f"📝 Creating playlist: {playlist_name}")

        track_ids = [track["id"] for track in tracks]
        scheduler_logger.info(f"🎵 Track IDs: {track_ids[:5]}... (total: {len(track_ids)})")

        comment_to_use = ai_reasoning if (ai_reasoning and ai_curated) else None
        comment_preview = comment_to_use[:200] + "..." if comment_to_use and len(comment_to_use) > 200 else comment_to_use
        scheduler_logger.info(f"💬 Creating Re-Discover playlist with comment (length: {len(comment_to_use) if comment_to_use else 0}): {comment_preview}")

        scheduler_logger.info("🎵 Calling nav_client.create_playlist...")
        navidrome_playlist_id = await nav_client.create_playlist(
            name=playlist_name,
            track_ids=track_ids,
            comment=comment_to_use
        )
        scheduler_logger.info(f"✅ Navidrome playlist created: {navidrome_playlist_id}")
        
        track_titles = [track["title"] for track in tracks]
        scheduler_logger.info(f"📊 Storing {len(track_titles)} track titles in database")

        scheduler_logger.info("💾 Creating playlist in database...")
        playlist = await db.create_playlist(
            artist_id="rediscover",
            playlist_name=playlist_name,
            songs=track_titles,
            reasoning=ai_reasoning if ai_curated else "Algorithmic selection",
            navidrome_playlist_id=navidrome_playlist_id,
            playlist_length=request.playlist_length
        )
        scheduler_logger.info(f"✅ Database playlist created: {playlist}")
        
        if request.refresh_frequency != "never":
            next_refresh = calculate_next_refresh(request.refresh_frequency)
            
            await db.create_scheduled_playlist(
                playlist_type="rediscover",
                navidrome_playlist_id=navidrome_playlist_id,
                refresh_frequency=request.refresh_frequency,
                next_refresh=next_refresh
            )
            
            schedule_playlist_refresh()
            scheduler_logger.info(f"📅 Scheduled {request.refresh_frequency} refresh for playlist: {playlist_name}")
        else:
            scheduler_logger.info(f"📅 No scheduling for playlist: {playlist_name} (refresh frequency: never)")
        
        playlist_dict = playlist.dict() if hasattr(playlist, 'dict') else playlist.__dict__
        playlist_dict["navidrome_playlist_id"] = navidrome_playlist_id
        playlist_dict["tracks"] = tracks
        playlist_dict["refresh_frequency"] = request.refresh_frequency
        playlist_dict["next_refresh"] = calculate_next_refresh(request.refresh_frequency).isoformat()
        
        return playlist_dict
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create Re-Discover Weekly playlist: {str(e)}")

def calculate_next_refresh(frequency: str) -> datetime:
    """Calculate the next refresh time based on frequency"""
    now = datetime.now()
    if frequency == "daily":
        next_day = now + timedelta(days=1)
        return next_day.replace(hour=1, minute=0, second=0, microsecond=0)
    elif frequency == "weekly":
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0 and now.hour >= 1:
            days_until_monday = 7
        next_monday = now + timedelta(days=days_until_monday)
        return next_monday.replace(hour=1, minute=0, second=0, microsecond=0)
    elif frequency == "monthly":
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1, hour=1, minute=0, second=0, microsecond=0)
        else:
            next_month = now.replace(month=now.month + 1, day=1, hour=1, minute=0, second=0, microsecond=0)
        return next_month
    else:
        return now  # Fallback

def schedule_playlist_refresh():
    """Schedule the playlist refresh job to run every 12 hours"""
    if not scheduler.get_job('playlist_refresh'):
        scheduler.add_job(
            refresh_scheduled_playlists,
            'cron',
            hour='1,13',
            minute=1,
            id='playlist_refresh',
            replace_existing=True
        )
        scheduler_logger.info("🔄 Playlist refresh job scheduled to run every 12 hours (1:01 AM and 1:01 PM)")

async def refresh_scheduled_playlists():
    """Check for and refresh scheduled playlists that are due"""
    try:
        current_time = datetime.now()
        
        if LOG_LEVEL == "DEBUG":
            scheduler_logger.debug(f"🔄 Scheduler auto-run initiated at {current_time.strftime('%H:%M:%S')}")
            scheduler_logger.debug("🔍 Checking for playlists due for refresh...")
        else:
            scheduler_logger.info("🔍 Checking for playlists due for refresh...")
        
        default_path = "/app/data/magiclists.db" if os.path.exists("/app/data") else "./magiclists.db"
        db_path = os.getenv("DATABASE_PATH", default_path)
        db = DatabaseManager(db_path)
        current_time = datetime.now()
        
        scheduled_playlists = await db.get_scheduled_playlists_due(current_time, grace_hours=168)
        
        if not scheduled_playlists:
            if LOG_LEVEL == "DEBUG":
                scheduler_logger.debug("✅ No playlists due for refresh at this time")
            return
        
        # Group by navidrome_playlist_id to prevent duplicate processing
        unique_playlists = {}
        for playlist in scheduled_playlists:
            playlist_id = playlist.navidrome_playlist_id
            if playlist_id not in unique_playlists:
                unique_playlists[playlist_id] = playlist
            else:
                existing = datetime.fromisoformat(unique_playlists[playlist_id].next_refresh)
                current = datetime.fromisoformat(playlist.next_refresh)
                if current > existing:
                    unique_playlists[playlist_id] = playlist
        
        final_playlists = list(unique_playlists.values())
        
        scheduler_logger.info(f"📋 Found {len(final_playlists)} playlist(s) due for refresh (deduplicated from {len(scheduled_playlists)} total)")
        
        # Get playlist names for better logging
        all_playlists = await db.get_all_playlists_with_schedule_info()
        playlist_names = {p["navidrome_playlist_id"]: p["playlist_name"] for p in all_playlists}
        
        for scheduled_playlist in final_playlists:
            try:
                playlist_name = playlist_names.get(scheduled_playlist.navidrome_playlist_id, "Unknown")
                scheduled_time = datetime.fromisoformat(scheduled_playlist.next_refresh)
                if scheduled_time < current_time:
                    overdue_hours = (current_time - scheduled_time).total_seconds() / 3600
                    scheduler_logger.info(f"🕐 Catching up on overdue playlist '{playlist_name}' [{scheduled_playlist.navidrome_playlist_id}] (missed by {overdue_hours:.1f} hours)")
                
                # ── DISPATCH BY PLAYLIST TYPE ──────────────────────────────────
                scheduler_logger.info(f"🔄 Starting refresh for '{playlist_name}' (type: {scheduled_playlist.playlist_type})")
                
                if scheduled_playlist.playlist_type == "rediscover":
                    await refresh_rediscover_playlist(scheduled_playlist, db)
                elif scheduled_playlist.playlist_type == "rediscover_weekly_v2":   # ← FIX: was silently missing
                    await refresh_rediscover_v2_playlist(scheduled_playlist, db)
                elif scheduled_playlist.playlist_type == "genre_mix":
                    await refresh_genre_mix_playlist(scheduled_playlist, db)
                elif scheduled_playlist.playlist_type == "this_is":
                    await refresh_this_is_playlist(scheduled_playlist, db)
                else:
                    scheduler_logger.warning(f"⚠️ Unknown playlist type '{scheduled_playlist.playlist_type}' for '{playlist_name}' — skipping")
            except Exception as refresh_error:
                scheduler_logger.error(f"❌ Failed to refresh playlist '{playlist_name}' [{scheduled_playlist.navidrome_playlist_id}]: {refresh_error}")
                import traceback
                scheduler_logger.error(f"📋 Traceback: {traceback.format_exc()}")
                
    except Exception as e:
        scheduler_logger.error(f"❌ Error checking scheduled playlists: {e}")

async def refresh_rediscover_playlist(scheduled_playlist, db: DatabaseManager):
    """Refresh a specific Re-Discover Weekly (v1) playlist"""
    try:
        scheduler_logger.info(f"🔄 Starting refresh for playlist ID: {scheduled_playlist.navidrome_playlist_id} (frequency: {scheduled_playlist.refresh_frequency})")
        
        nav_client = get_navidrome_client()
        
        playlists = await db.get_all_playlists_with_schedule_info()
        original_playlist = next((p for p in playlists if p.get("navidrome_playlist_id") == scheduled_playlist.navidrome_playlist_id), None)
        
        if not original_playlist:
            scheduler_logger.error(f"❌ Could not find original playlist data for {scheduled_playlist.navidrome_playlist_id}")
            return
        
        original_length = original_playlist.get("playlist_length", 20)
        scheduler_logger.info(f"🎯 Using original playlist length: {original_length}")
        
        previous_songs = original_playlist.get("songs", [])[:10]
        variety_instruction = f"REFRESH CHALLENGE: The current playlist opens with these tracks in this order: {', '.join(previous_songs[:5])}. Your goal is to create a FRESH arrangement that tells a different musical story. You may include some of the same excellent tracks if they're rediscovery-worthy, but avoid replicating the same opening sequence or overall flow. Think creatively about re-ordering, substituting, or finding better transitions to ensure a genuinely refreshed listening experience." if previous_songs else ""
        
        ai_client = get_ai_client()
        user_id = await db.get_or_create_user_id()
        server_id = nav_client.base_url or "unknown_server"
        processor = ReDiscoverV2Processor(nav_client, ai_client, db)
        library_ids = [scheduled_playlist.library_id] if hasattr(scheduled_playlist, 'library_id') and scheduled_playlist.library_id else None

        scheduler_logger.info(f"🔄 Re-Discover v2.0 refresh context - Previous tracks: {len(previous_songs)}, Library IDs: {library_ids}")

        result = await processor.generate_playlist(user_id, server_id, library_ids)
        tracks = result.get("tracks", [])
        
        if tracks:
            scheduler_logger.info(f"🎵 Generated {len(tracks)} new tracks for refresh")
            
            if len(tracks) != original_length:
                scheduler_logger.warning(f"⚠️ Generated {len(tracks)} tracks but user requested {original_length}")
            else:
                scheduler_logger.info(f"✅ Generated exact number of requested tracks: {len(tracks)}")
            
            # Read reasoning at result level first, fall back to per-track
            ai_reasoning = result.get("reasoning", "")
            ai_curated = result.get("ai_curated", False)
            if not ai_reasoning:
                ai_reasoning = next((t.get("ai_reasoning", "") for t in tracks if t.get("ai_reasoning")), "")
                ai_curated = any(t.get("ai_curated", False) for t in tracks)
            
            if ai_reasoning and ai_curated:
                reasoning_preview = ai_reasoning[:200] + "..." if len(ai_reasoning) > 200 else ai_reasoning
                scheduler_logger.info(f"🎵 AI curation applied for scheduled Re-Discover refresh (reasoning length: {len(ai_reasoning)} chars): {reasoning_preview}")
            else:
                scheduler_logger.info(f"⚠️ Scheduled Re-Discover refresh used algorithmic selection")
            
            track_ids = [track["id"] for track in tracks]
            comment_to_use = ai_reasoning if (ai_reasoning and ai_curated) else "Re-Discover Weekly v2.0 - Automatically refreshed"
            await nav_client.update_playlist(
                playlist_id=scheduled_playlist.navidrome_playlist_id,
                track_ids=track_ids,
                comment=comment_to_use
            )
            
            track_titles = [track["title"] for track in tracks]
            reasoning_to_store = ai_reasoning if ai_curated else "Algorithmic selection"
            await db.update_playlist_content(
                navidrome_playlist_id=scheduled_playlist.navidrome_playlist_id,
                songs=track_titles,
                reasoning=reasoning_to_store
            )
            
            next_refresh = calculate_next_refresh(scheduled_playlist.refresh_frequency)
            await db.update_scheduled_playlist_next_refresh(scheduled_playlist.id, next_refresh)
            
            scheduler_logger.info(f"✅ Successfully refreshed playlist {scheduled_playlist.navidrome_playlist_id}. Next refresh: {next_refresh.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            scheduler_logger.warning(f"⚠️ No tracks generated for playlist {scheduled_playlist.navidrome_playlist_id}")
        
    except Exception as e:
        scheduler_logger.error(f"❌ Error refreshing playlist {scheduled_playlist.navidrome_playlist_id}: {e}")


async def refresh_rediscover_v2_playlist(scheduled_playlist, db: DatabaseManager):
    """Refresh a specific Re-Discover Weekly v2.0 playlist"""
    try:
        scheduler_logger.info(f"🔄 Starting v2.0 refresh for playlist {scheduled_playlist.navidrome_playlist_id} (frequency: {scheduled_playlist.refresh_frequency})")

        nav_client = get_navidrome_client()
        ai_client = get_ai_client()

        library_ids = [scheduled_playlist.library_id] if hasattr(scheduled_playlist, 'library_id') and scheduled_playlist.library_id else None

        user_id = await db.get_or_create_user_id()
        server_id = nav_client.base_url or "unknown_server"

        processor = ReDiscoverV2Processor(nav_client, ai_client, db)
        result = await processor.generate_playlist(user_id, server_id, library_ids)
        tracks = result.get("tracks", [])

        if not tracks:
            scheduler_logger.warning(f"⚠️ No tracks generated for v2.0 refresh of {scheduled_playlist.navidrome_playlist_id}")
            return

        scheduler_logger.info(f"🎵 Generated {len(tracks)} tracks for v2.0 refresh")

        # Read reasoning at result level first, fall back to per-track
        ai_reasoning = result.get("reasoning", "")
        ai_curated = result.get("ai_curated", False)
        if not ai_reasoning:
            ai_reasoning = next(
                (t.get("ai_reasoning", "") for t in tracks if t.get("ai_reasoning")),
                ""
            )
            ai_curated = any(t.get("ai_curated", False) for t in tracks)

        if ai_reasoning and ai_curated:
            reasoning_preview = ai_reasoning[:200] + "..." if len(ai_reasoning) > 200 else ai_reasoning
            scheduler_logger.info(f"🎵 AI curation applied for v2.0 refresh (reasoning length: {len(ai_reasoning)} chars): {reasoning_preview}")
        else:
            scheduler_logger.info(f"⚠️ v2.0 refresh used fallback/algorithmic selection")

        comment = ai_reasoning if ai_reasoning else "Re-Discover v2.0 - Automatically refreshed"
        track_ids = [track["id"] for track in tracks]

        await nav_client.update_playlist(
            playlist_id=scheduled_playlist.navidrome_playlist_id,
            track_ids=track_ids,
            comment=comment
        )

        track_titles = [track.get("title", "Unknown") for track in tracks]
        await db.update_playlist_content(
            navidrome_playlist_id=scheduled_playlist.navidrome_playlist_id,
            songs=track_titles,
            reasoning=ai_reasoning or "Algorithmic selection"
        )

        next_refresh = calculate_next_refresh(scheduled_playlist.refresh_frequency)
        await db.update_scheduled_playlist_next_refresh(scheduled_playlist.id, next_refresh)

        scheduler_logger.info(f"✅ v2.0 refresh complete for {scheduled_playlist.navidrome_playlist_id}. Next: {next_refresh.strftime('%Y-%m-%d %H:%M:%S')}")

    except Exception as e:
        scheduler_logger.error(f"❌ Error in v2.0 refresh for {scheduled_playlist.navidrome_playlist_id}: {e}")


async def refresh_genre_mix_playlist(scheduled_playlist, db: DatabaseManager):
    """Refresh a scheduled Genre Mix playlist"""
    try:
        scheduler_logger.info(f"🔄 Starting refresh for Genre Mix playlist {scheduled_playlist.navidrome_playlist_id} (frequency: {scheduled_playlist.refresh_frequency})")

        nav_client = get_navidrome_client()
        ai_client_instance = get_ai_client()

        playlists = await db.get_all_playlists_with_schedule_info()
        original_playlist = next(
            (p for p in playlists if p.get("navidrome_playlist_id") == scheduled_playlist.navidrome_playlist_id),
            None
        )

        if not original_playlist:
            scheduler_logger.error(f"❌ Could not find original playlist data for {scheduled_playlist.navidrome_playlist_id}")
            return

        genre = original_playlist.get("artist_id")
        if not genre:
            scheduler_logger.error(f"❌ Missing genre metadata for playlist {scheduled_playlist.navidrome_playlist_id}")
            return

        playlist_length = original_playlist.get("playlist_length") or 25
        library_ids = original_playlist.get("library_ids") or None

        all_tracks = await nav_client.get_tracks_by_genre(genre, library_ids)

        if not all_tracks:
            scheduler_logger.warning(f"⚠️ No tracks found for genre '{genre}' during refresh of {scheduled_playlist.navidrome_playlist_id}")
            return

        library_stats = await nav_client.get_library_stats()
        filtered_tracks, filter_metadata = filter_tracks_for_this_is_playlist(
            source_tracks=all_tracks,
            target_playlist_size=playlist_length,
            library_stats=library_stats
        )

        if filter_metadata["filtered"]:
            scheduler_logger.info(f"🎯 Smart filtering applied: {filter_metadata['source_count']} → {filter_metadata['sent_count']} tracks (multiplier: {filter_metadata['threshold_multiplier']}x)")
            scheduler_logger.info(f"📊 Score range: {filter_metadata['score_range']['highest']:.1f} - {filter_metadata['score_range']['lowest']:.1f} (cutoff: {filter_metadata['score_range']['cutoff']:.1f})")
        else:
            scheduler_logger.info(f"✅ No filtering needed: {filter_metadata['source_count']} tracks below threshold")

        curation_result = await ai_client_instance.curate_genre_mix(
            genre=genre,
            tracks_json=filtered_tracks,
            num_tracks=playlist_length,
            include_reasoning=True
        )

        if isinstance(curation_result, tuple):
            curated_track_ids, reasoning = curation_result
        else:
            curated_track_ids = curation_result
            reasoning = ""

        if not curated_track_ids:
            scheduler_logger.warning(f"⚠️ AI curation returned no tracks for Genre Mix refresh of {scheduled_playlist.navidrome_playlist_id}")
            return

        if reasoning:
            reasoning_preview = reasoning[:200] + "..." if len(reasoning) > 200 else reasoning
            scheduler_logger.info(f"🎵 AI curation applied for genre '{genre}' (reasoning length: {len(reasoning)} chars): {reasoning_preview}")
        else:
            scheduler_logger.info(f"⚠️ No AI reasoning provided for genre refresh '{genre}'")

        comment_to_use = reasoning if reasoning else None
        comment_preview = comment_to_use[:200] + "..." if comment_to_use and len(comment_to_use) > 200 else comment_to_use
        scheduler_logger.info(f"💬 Updating playlist with comment (length: {len(comment_to_use) if comment_to_use else 0}): {comment_preview}")

        await nav_client.update_playlist(
            playlist_id=scheduled_playlist.navidrome_playlist_id,
            track_ids=curated_track_ids,
            comment=comment_to_use
        )

        track_titles = []
        track_id_to_title = {track["id"]: track["title"] for track in all_tracks}
        for track_id in curated_track_ids:
            if track_id in track_id_to_title:
                track_titles.append(track_id_to_title[track_id])

        await db.update_playlist_content(
            navidrome_playlist_id=scheduled_playlist.navidrome_playlist_id,
            songs=track_titles,
            reasoning=reasoning or "Genre Mix refresh"
        )

        next_refresh = calculate_next_refresh(scheduled_playlist.refresh_frequency)
        await db.update_scheduled_playlist_next_refresh(scheduled_playlist.id, next_refresh)

        scheduler_logger.info(f"✅ Successfully refreshed Genre Mix playlist {scheduled_playlist.navidrome_playlist_id}. Next refresh: {next_refresh.strftime('%Y-%m-%d %H:%M:%S')}")

    except Exception as e:
        scheduler_logger.error(f"❌ Error refreshing Genre Mix playlist {scheduled_playlist.navidrome_playlist_id}: {e}")


async def refresh_this_is_playlist(scheduled_playlist, db: DatabaseManager):
    """Refresh a specific This Is playlist"""
    try:
        scheduler_logger.info(f"🔄 Starting refresh for This Is playlist ID: {scheduled_playlist.navidrome_playlist_id} (frequency: {scheduled_playlist.refresh_frequency})")
        
        nav_client = get_navidrome_client()
        ai_client_instance = get_ai_client()
        
        playlists = await db.get_all_playlists_with_schedule_info()
        original_playlist = next((p for p in playlists if p.get("navidrome_playlist_id") == scheduled_playlist.navidrome_playlist_id), None)
        
        if not original_playlist:
            scheduler_logger.error(f"❌ Could not find original playlist data for {scheduled_playlist.navidrome_playlist_id}")
            return
        
        artist_id = original_playlist["artist_id"]
        
        all_artists = await nav_client.get_artists()
        artist = next((a for a in all_artists if a["id"] == artist_id), None)
        
        if not artist:
            scheduler_logger.error(f"❌ Could not find artist data for ID: {artist_id}")
            return
        
        artist_name = artist["name"]
        
        tracks = await nav_client.get_tracks_by_artist(artist_id)
        
        if tracks:
            scheduler_logger.info(f"🎵 Found {len(tracks)} tracks for artist: {artist_name} (fresh data)")
            
            original_length = original_playlist.get("playlist_length", 25)
            scheduler_logger.info(f"🎯 ENFORCING original playlist length: {original_length}")
            
            if len(tracks) < original_length:
                scheduler_logger.warning(f"⚠️ Artist only has {len(tracks)} tracks, but user requested {original_length}. Using all available tracks.")
                original_length = len(tracks)
            
            previous_songs = original_playlist.get("songs", [])
            variety_instruction = f"REFRESH CONSTRAINT: This is a REFRESH, not a copy. Previous playlist had these tracks: {', '.join(previous_songs[:10])}. Create a completely different track selection and arrangement. Prioritize tracks NOT in the previous list. Tell a fresh musical story. Avoid identical opening sequences." if previous_songs else "Create a fresh, engaging playlist arrangement."
            
            tracks_for_ai = tracks.copy()
            
            curation_result = await ai_client_instance.curate_this_is(
                artist_name=artist_name,
                tracks_json=tracks_for_ai,
                num_tracks=original_length,
                include_reasoning=True,
                variety_context=variety_instruction
            )
            
            if isinstance(curation_result, tuple):
                curated_track_ids, reasoning = curation_result
            else:
                curated_track_ids = curation_result
                reasoning = ""
            
            if curated_track_ids:
                if len(curated_track_ids) < original_length and len(tracks) >= original_length:
                    scheduler_logger.warning(f"⚠️ AI returned only {len(curated_track_ids)} tracks but user requested {original_length}. Using fallback to fill gap.")
                    used_ids = set(curated_track_ids)
                    remaining_tracks = [t for t in tracks if t["id"] not in used_ids]
                    additional_needed = original_length - len(curated_track_ids)
                    additional_tracks = remaining_tracks[:additional_needed]
                    curated_track_ids.extend([t["id"] for t in additional_tracks])
                
                scheduler_logger.info(f"🎯 Final track count: {len(curated_track_ids)} (requested: {original_length})")
                
                await nav_client.update_playlist(
                    playlist_id=scheduled_playlist.navidrome_playlist_id,
                    track_ids=curated_track_ids,
                    comment=reasoning if reasoning else None
                )
                
                track_titles = []
                track_id_to_title = {track["id"]: track["title"] for track in tracks}
                for track_id in curated_track_ids:
                    if track_id in track_id_to_title:
                        track_titles.append(track_id_to_title[track_id])
                
                await db.update_playlist_content(
                    navidrome_playlist_id=scheduled_playlist.navidrome_playlist_id,
                    songs=track_titles,
                    reasoning=reasoning
                )
                
                next_refresh = calculate_next_refresh(scheduled_playlist.refresh_frequency)
                await db.update_scheduled_playlist_next_refresh(scheduled_playlist.id, next_refresh)
                
                scheduler_logger.info(f"✅ Successfully refreshed This Is playlist {scheduled_playlist.navidrome_playlist_id}. Next refresh: {next_refresh.strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                scheduler_logger.warning(f"⚠️ No curated tracks generated for This Is playlist {scheduled_playlist.navidrome_playlist_id}")
        else:
            scheduler_logger.warning(f"⚠️ No tracks found for artist {artist_name} in playlist {scheduled_playlist.navidrome_playlist_id}")
        
    except Exception as e:
        scheduler_logger.error(f"❌ Error refreshing This Is playlist {scheduled_playlist.navidrome_playlist_id}: {e}")

@app.get("/api/playlists")
async def get_all_playlists(db: DatabaseManager = Depends(get_db)):
    """Get all playlists with scheduling information"""
    try:
        playlists = await db.get_all_playlists_with_schedule_info()
        for playlist in playlists:
            songs = playlist.get("songs", [])
            playlist["track_count"] = len(songs) if isinstance(songs, list) else 0
        return playlists
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch playlists: {str(e)}")

@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int, db: DatabaseManager = Depends(get_db)):
    """Delete a playlist from both local database and Navidrome"""
    try:
        playlist = await db.get_playlist_by_id_with_schedule_info(playlist_id)
        
        if not playlist:
            raise HTTPException(status_code=404, detail="Playlist not found")
        
        navidrome_playlist_id = playlist.get("navidrome_playlist_id")
        if navidrome_playlist_id:
            nav_client = get_navidrome_client()
            try:
                print(f"🗑️ Deleting playlist {playlist_id} from Navidrome (Navidrome ID: {navidrome_playlist_id})")
                deletion_result = await nav_client.delete_playlist(navidrome_playlist_id)
                print(f"✅ Navidrome deletion result: {deletion_result}")
            except Exception as e:
                print(f"❌ Warning: Failed to delete playlist from Navidrome: {e}")
        else:
            print(f"⚠️ No Navidrome playlist ID found for local playlist {playlist_id}, skipping Navidrome deletion")
        
        if navidrome_playlist_id:
            await db.delete_scheduled_playlist_by_navidrome_id(navidrome_playlist_id)
        
        success = await db.delete_playlist(playlist_id)
        
        if not success:
            raise HTTPException(status_code=404, detail="Playlist not found in database")
        
        return {"message": "Playlist deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete playlist: {str(e)}")

@app.get("/api/recipes")
async def get_available_recipes():
    """Get information about available playlist generation recipes"""
    try:
        recipes_info = recipe_manager.list_available_recipes()
        return recipes_info
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load recipes: {str(e)}")

@app.get("/api/recipes/validate")
async def validate_recipes():
    """Validate all recipe files and return any errors"""
    try:
        registry = recipe_manager._load_registry()
        validation_results = {}
        
        for playlist_type, recipe_filename in registry.items():
            errors = recipe_manager.validate_recipe(recipe_filename)
            validation_results[playlist_type] = {
                "recipe_file": recipe_filename,
                "valid": len(errors) == 0,
                "errors": errors
            }
        
        return validation_results
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to validate recipes: {str(e)}")

@app.get("/api/scheduler/status")
async def get_scheduler_status():
    """Get scheduler status and active jobs"""
    try:
        global scheduler
        if scheduler:
            jobs = list(scheduler.get_jobs())
            job_info = []
            for job in jobs:
                job_info.append({
                    "id": job.id,
                    "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                    "func": job.func.__name__ if hasattr(job, 'func') else str(job.func)
                })
            
            return {
                "scheduler_running": scheduler.running,
                "active_jobs": len(jobs),
                "jobs": job_info,
                "scheduler_state": str(scheduler.state)
            }
        else:
            return {
                "scheduler_running": False,
                "error": "Scheduler not initialized"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get scheduler status: {str(e)}")

@app.post("/api/scheduler/trigger")
async def trigger_scheduler_check():
    """Manually trigger the scheduler to check for playlists due for refresh"""
    try:
        scheduler_logger.info("🧪 Manual scheduler trigger requested via API")
        await refresh_scheduled_playlists()
        return {"message": "Scheduler check completed successfully"}
    except Exception as e:
        scheduler_logger.error(f"❌ Error in manual scheduler trigger: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to trigger scheduler: {str(e)}")

@app.post("/api/playlists/{navidrome_playlist_id}/refresh")
async def force_refresh_playlist(navidrome_playlist_id: str, db: DatabaseManager = Depends(get_db)):
    """Force an immediate refresh of a specific playlist, regardless of its schedule"""
    try:
        scheduler_logger.info(f"🧪 Manual refresh requested for playlist {navidrome_playlist_id}")
        
        # Get all scheduled playlists
        scheduled_playlists = await db.get_scheduled_playlists()
        
        # Find the requested playlist
        target_playlist = next((p for p in scheduled_playlists if p.navidrome_playlist_id == navidrome_playlist_id), None)
        
        if not target_playlist:
            raise HTTPException(status_code=404, detail="Playlist not found or not scheduled for refresh")
            
        all_playlists = await db.get_all_playlists_with_schedule_info()
        playlist_name = next((p["playlist_name"] for p in all_playlists if p["navidrome_playlist_id"] == navidrome_playlist_id), "Unknown")
        
        scheduler_logger.info(f"🔄 Force starting refresh for '{playlist_name}' (type: {target_playlist.playlist_type})")
        
        # Dispatch to the correct handler
        if target_playlist.playlist_type == "rediscover":
            await refresh_rediscover_playlist(target_playlist, db)
        elif target_playlist.playlist_type == "rediscover_weekly_v2":
            await refresh_rediscover_v2_playlist(target_playlist, db)
        elif target_playlist.playlist_type == "genre_mix":
            await refresh_genre_mix_playlist(target_playlist, db)
        elif target_playlist.playlist_type == "this_is":
            await refresh_this_is_playlist(target_playlist, db)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown playlist type '{target_playlist.playlist_type}'")
            
        # Update the next refresh time since we just refreshed it
        current_time = datetime.now()
        await db.update_scheduled_playlist_time(
            navidrome_playlist_id=navidrome_playlist_id,
            next_refresh=current_time  # Will be pushed forward by the refresh functions
        )
            
        return {"message": f"Successfully refreshed playlist '{playlist_name}'"}
        
    except HTTPException:
        raise
    except Exception as e:
        scheduler_logger.error(f"❌ Error forcing playlist refresh: {e}")
        import traceback
        scheduler_logger.error(f"📋 Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to refresh playlist: {str(e)}")

@app.post("/api/scheduler/start")
async def start_scheduler_job():
    """Manually start the recurring scheduler job"""
    try:
        schedule_playlist_refresh()
        global scheduler
        jobs = list(scheduler.get_jobs()) if scheduler else []
        scheduler_logger.info(f"🔄 Scheduler job registration requested. Active jobs: {len(jobs)}")
        return {
            "message": "Scheduler job started",
            "active_jobs": len(jobs),
            "jobs": [{"id": job.id, "next_run": job.next_run_time.isoformat() if job.next_run_time else None} for job in jobs]
        }
    except Exception as e:
        scheduler_logger.error(f"❌ Error starting scheduler job: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start scheduler job: {str(e)}")

@app.get("/api/ai-model-info")
async def get_ai_model_info():
    """Get current AI model information for analytics"""
    try:
        ai_client_instance = get_ai_client()
        return {
            "provider": ai_client_instance.provider.provider_type,
            "model": ai_client_instance.model or "unknown",
            "has_api_key": bool(ai_client_instance.api_key)
        }
    except Exception as e:
        return {
            "provider": "unknown",
            "model": "unknown", 
            "has_api_key": False
        }

@app.post("/api/track-library-size")
async def track_library_size(db: DatabaseManager = Depends(get_db)):
    """Track library size for analytics (called post-launch)"""
    try:
        should_track = await db.should_track_library_size()
        if not should_track:
            return {"message": "Library size tracking not needed yet", "tracked": False}
        
        nav_client = get_navidrome_client()
        song_count = await nav_client.get_total_song_count()
        
        user_id = await db.get_or_create_user_id()
        await db.record_library_size(song_count)
        
        scheduler_logger.info(f"📊 Library size tracked: {song_count} songs for user {user_id}")
        
        return {
            "message": "Library size tracked successfully",
            "tracked": True,
            "song_count": song_count,
            "user_id": user_id
        }
        
    except Exception as e:
        scheduler_logger.error(f"❌ Error tracking library size: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to track library size: {str(e)}")

# SPA ROUTING - Smart catch-all for client-side routing (MUST be last route)
@app.get("/{path:path}", response_class=HTMLResponse)
async def spa_router(request: Request, path: str):
    """Handle SPA routing - serve app for known paths, redirect unknown paths"""
    spa_paths = ["this-is", "re-discover", "playlists", "terms"]
    
    if path in spa_paths:
        if not system_check_passed:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/system-check", status_code=302)
        return templates.TemplateResponse("index.html", {"request": request})
    
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=302)

if __name__ == "__main__":
    import uvicorn.config
    
    class FilteredUvicornFormatter(uvicorn.formatters.DefaultFormatter):
        def format(self, record):
            if hasattr(record, 'args') and record.args:
                message = str(record.args[2]) if len(record.args) > 2 else ""
                if 'GET / HTTP' in message:
                    return ""
            return super().format(record)
    
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["()"] = FilteredUvicornFormatter
    
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8000,
        log_config=log_config
    )