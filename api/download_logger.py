import sqlite3
import os
import logging
from datetime import datetime
from fastapi import Request

logger = logging.getLogger(__name__)

# Store DB in the backend directory
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "download_logs.db")

def init_db():
    """Ensure the download_logs table exists in SQLite database."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS download_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                page TEXT NOT NULL,
                filename TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                device TEXT NOT NULL,
                location_type TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to initialize SQLite database: {e}", exc_info=True)

def get_client_ip(request: Request) -> str:
    """Extract the real client IP address, handling proxy headers."""
    # Check X-Forwarded-For (standard header for multiple proxies)
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    
    # Check X-Real-IP
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
        
    # Direct fallback
    if request.client:
        return request.client.host
    return "Unknown IP"

def is_private_ip(ip: str) -> bool:
    """Check if the given IP address is a private/local IP."""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
        
    # IPv4 Private Subnets
    # 10.0.0.0/8
    # 172.16.0.0/12 (172.16.0.0 – 172.31.255.255)
    # 192.168.0.0/16
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
        
    if ip.startswith("172."):
        parts = ip.split(".")
        if len(parts) >= 2:
            try:
                second_octet = int(parts[1])
                if 16 <= second_octet <= 31:
                    return True
            except ValueError:
                pass
                
    return False

def parse_device_info(user_agent: str) -> str:
    """Parse user agent to extract clean OS and browser names."""
    if not user_agent:
        return "Unknown Device"
        
    # 1. OS parsing
    os_info = "Unknown OS"
    ua_lower = user_agent.lower()
    if "windows" in ua_lower:
        os_info = "Windows"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        os_info = "Mac OS"
    elif "iphone" in ua_lower:
        os_info = "iPhone"
    elif "ipad" in ua_lower:
        os_info = "iPad"
    elif "android" in ua_lower:
        os_info = "Android"
    elif "linux" in ua_lower:
        os_info = "Linux"
        
    # 2. Browser parsing
    browser_info = "Unknown Browser"
    if "edg/" in ua_lower or "edge" in ua_lower:
        browser_info = "Edge"
    elif "chrome" in ua_lower:
        # Chrome is also included in Safari's UA, so check safari/chrome order
        if "safari" in ua_lower and "chrome" not in ua_lower:
            browser_info = "Safari"
        else:
            browser_info = "Chrome"
    elif "safari" in ua_lower:
        browser_info = "Safari"
    elif "firefox" in ua_lower:
        browser_info = "Firefox"
    elif "trident" in ua_lower or "msie" in ua_lower:
        browser_info = "Internet Explorer"
        
    return f"{os_info} ({browser_info})"

def log_download(page: str, filename: str, request: Request):
    """Log a download event by extracting details from Request."""
    init_db()
    
    from datetime import timezone
    timestamp = datetime.now(timezone.utc).isoformat()
    ip_address = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    device = parse_device_info(user_agent)
    
    # Classify whether the download is from local network/PC or external client
    if is_private_ip(ip_address):
        location_type = "Local PC / LAN Network"
    else:
        location_type = f"External Client (IP: {ip_address})"
        
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO download_logs (timestamp, page, filename, ip_address, user_agent, device, location_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (timestamp, page, filename, ip_address, user_agent, device, location_type))
        conn.commit()
        conn.close()
        logger.info(f"Log written: {page} - {filename} by {ip_address}")
    except Exception as e:
        logger.error(f"Failed to save download log: {e}", exc_info=True)

def get_logs() -> list:
    """Retrieve the last 500 download log entries."""
    init_db()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM download_logs ORDER BY timestamp DESC LIMIT 500")
        rows = cursor.fetchall()
        logs = [dict(row) for row in rows]
        conn.close()
        return logs
    except Exception as e:
        logger.error(f"Failed to fetch download logs: {e}", exc_info=True)
        return []

def clear_logs():
    """Clear all entries from the download_logs table."""
    init_db()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM download_logs")
        conn.commit()
        conn.close()
        logger.info("All download logs have been cleared.")
    except Exception as e:
        logger.error(f"Failed to clear download logs: {e}", exc_info=True)
