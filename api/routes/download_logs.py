from fastapi import APIRouter
from api.download_logger import get_logs, clear_logs

router = APIRouter(prefix="/api/download-logs", tags=["Download Logs"])

@router.get("")
async def get_download_logs():
    """Retrieve all recorded download events."""
    logs = get_logs()
    return {"status": "success", "data": logs}

@router.post("/clear")
async def clear_download_logs():
    """Clear all recorded download logs."""
    clear_logs()
    return {"status": "success", "message": "Logs cleared successfully"}
