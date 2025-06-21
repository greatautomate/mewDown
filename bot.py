#!/usr/bin/env python3
"""
ClassPlus Video Downloader Telegram Bot with Pyrogram (2GB Support)

A comprehensive Telegram bot using Pyrogram that downloads videos from ClassPlus HLS streams,
merges segments using FFmpeg, and uploads them to Telegram with 2GB file support.

Author: Your Name
Version: 2.0.0
License: MIT
"""

import os
import sys
import logging
import tempfile
import time
import subprocess
import shutil
import asyncio
import signal
import json
from pathlib import Path
from urllib.parse import quote, urlparse
from typing import List, Dict, Optional, Tuple, Union
from datetime import datetime, timedelta
import re

# Third-party imports
import aiohttp
import aiofiles
import structlog
from pyrogram import Client, filters, types
from pyrogram.errors import FloodWait, MessageNotModified
from dotenv import load_dotenv
import psutil
from tqdm import tqdm

# Load environment variables
load_dotenv()

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

class ConfigManager:
    """Configuration management class"""

    def __init__(self):
        self.BOT_TOKEN = os.getenv('BOT_TOKEN')
        self.API_ID = int(os.getenv('API_ID', '0'))
        self.API_HASH = os.getenv('API_HASH')
        self.MAX_FILE_SIZE_MB = int(os.getenv('MAX_FILE_SIZE_MB', '2048'))  # 2GB
        self.INPUT_FILE_SIZE_MB = int(os.getenv('INPUT_FILE_SIZE_MB', '2048'))
        self.MAX_CONCURRENT_DOWNLOADS = int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '1'))
        self.DOWNLOAD_TIMEOUT = int(os.getenv('DOWNLOAD_TIMEOUT', '1800'))  # 30 minutes
        self.UPLOAD_TIMEOUT = int(os.getenv('UPLOAD_TIMEOUT', '3600'))  # 1 hour
        self.FFMPEG_LOGLEVEL = os.getenv('FFMPEG_LOGLEVEL', 'error')
        self.TEMP_DIR = os.getenv('TEMP_DIR', '/tmp/videos')
        self.PROGRESS_UPDATE_INTERVAL = int(os.getenv('PROGRESS_UPDATE_INTERVAL', '10'))

        # Validate required config
        if not all([self.BOT_TOKEN, self.API_ID, self.API_HASH]):
            raise ValueError("BOT_TOKEN, API_ID, and API_HASH environment variables are required")

        # Create temp directory
        Path(self.TEMP_DIR).mkdir(parents=True, exist_ok=True)

class VideoProcessor:
    """Handles video downloading and processing"""

    def __init__(self, config: ConfigManager):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None

        # Browser-like headers for ClassPlus API
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'cross-site',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'Referer': 'https://web.classplusapp.com/',
            'Origin': 'https://web.classplusapp.com'
        }

    async def start_session(self):
        """Initialize HTTP session"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=self.config.DOWNLOAD_TIMEOUT)
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers=self.headers
            )
            logger.info("HTTP session initialized")

    async def close_session(self):
        """Close HTTP session"""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("HTTP session closed")

    def parse_video_file(self, content: str) -> List[Dict]:
        """Parse video file content and extract video information"""
        videos = []
        lines = content.strip().split('\n')

        logger.info(f"Parsing video file with {len(lines)} lines")

        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            if ':https://' in line:
                parts = line.split(':https://', 1)
                if len(parts) == 2:
                    title = parts[0].strip()
                    url = 'https://' + parts[1].strip()

                    # Validate ClassPlus URL
                    if any(domain in url for domain in ['classplusapp.com', 'classplus.co']):
                        videos.append({
                            'title': title,
                            'url': url,
                            'line_number': line_num,
                            'size_estimate': 0
                        })
                        logger.debug(f"Added video: {title}", line=line_num)
                    else:
                        logger.warning(f"Invalid URL on line {line_num}: {url}")
                else:
                    logger.warning(f"Invalid format on line {line_num}: {line}")
            else:
                logger.warning(f"No URL found on line {line_num}: {line}")

        logger.info(f"Parsed {len(videos)} valid videos")
        return videos

    async def get_processed_m3u8_url(self, original_url: str) -> Optional[str]:
        """Get processed M3U8 URL from UGX ClassPlus API"""
        try:
            encoded_url = quote(original_url, safe='')
            api_url = f"https://ugxclassplusapi.vercel.app/player?url={encoded_url}"

            logger.info(f"Calling UGX API: {api_url}")

            async with self.session.get(api_url) as response:
                if response.status == 200:
                    response_text = await response.text()

                    # Try JSON parsing first
                    try:
                        data = json.loads(response_text)

                        # Check various possible keys for the M3U8 URL
                        for key in ['url', 'playlist_url', 'master_url', 'stream_url']:
                            if key in data and data[key] and '.m3u8' in data[key]:
                                logger.info(f"Found M3U8 URL in JSON response: {key}")
                                return data[key]

                    except json.JSONDecodeError:
                        logger.debug("Response is not JSON, trying regex extraction")

                    # Try regex patterns for M3U8 URLs
                    patterns = [
                        r'https://[^\s"\'<>]+\.m3u8[^\s"\'<>]*',
                        r'https://[^\s"\'<>]+/playlist\.m3u8[^\s"\'<>]*',
                        r'https://[^\s"\'<>]+/master\.m3u8[^\s"\'<>]*'
                    ]

                    for pattern in patterns:
                        matches = re.findall(pattern, response_text)
                        if matches:
                            logger.info(f"Found M3U8 URL via regex: {pattern}")
                            return matches[0]

                    # Check if response itself is a URL
                    if response_text.strip().startswith('http') and '.m3u8' in response_text:
                        return response_text.strip()

                    logger.warning("No M3U8 URL found in API response")
                    return original_url  # Fallback to original

                else:
                    logger.error(f"API request failed", status=response.status)
                    return original_url

        except Exception as e:
            logger.error(f"Error calling UGX API: {str(e)}")
            return original_url

    async def download_with_ffmpeg(self, m3u8_url: str, title: str, progress_callback=None) -> Tuple[Optional[str], Optional[str]]:
        """Download HLS video using FFmpeg with progress tracking"""

        # Create unique temporary file
        timestamp = int(time.time())
        safe_title = re.sub(r'[^\w\-_\.]', '_', title)[:50]
        temp_file = f"{self.config.TEMP_DIR}/{safe_title}_{timestamp}.mp4"

        try:
            logger.info(f"Starting FFmpeg download: {title}")

            # Enhanced FFmpeg command for 2GB files
            ffmpeg_cmd = [
                'ffmpeg',
                '-user_agent', self.headers['User-Agent'],
                '-headers', f"Referer: {self.headers['Referer']}\r\nOrigin: {self.headers['Origin']}",
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-i', m3u8_url,
                '-c', 'copy',  # Copy streams without re-encoding
                '-bsf:a', 'aac_adtstoasc',
                '-avoid_negative_ts', 'make_zero',
                '-fflags', '+genpts',
                '-movflags', '+faststart',
                '-max_muxing_queue_size', '8192',  # Handle large files
                '-thread_queue_size', '1024',
                '-loglevel', self.config.FFMPEG_LOGLEVEL,
                '-stats',  # Enable stats for progress
                '-y',
                temp_file
            ]

            # Execute FFmpeg with real-time progress tracking
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1
            )

            # Monitor progress
            while True:
                output = process.stderr.readline()
                if output == '' and process.poll() is not None:
                    break

                if output and progress_callback:
                    # Extract progress information from FFmpeg output
                    if 'time=' in output:
                        try:
                            time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2})', output)
                            if time_match:
                                hours, minutes, seconds = map(int, time_match.groups())
                                current_time = hours * 3600 + minutes * 60 + seconds
                                await progress_callback(current_time)
                        except Exception:
                            pass

            return_code = process.poll()

            if return_code == 0:
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    file_size = os.path.getsize(temp_file)
                    size_mb = file_size / (1024 * 1024)

                    logger.info(f"FFmpeg download completed", 
                              file_size=file_size, 
                              size_mb=f"{size_mb:.2f}MB")

                    # Check 2GB file size limit
                    if size_mb > self.config.MAX_FILE_SIZE_MB:
                        os.unlink(temp_file)
                        return None, f"File too large: {size_mb:.1f}MB (limit: {self.config.MAX_FILE_SIZE_MB}MB)"

                    return temp_file, None
                else:
                    logger.error("FFmpeg completed but output file is missing or empty")
                    return None, "Download completed but file is empty"
            else:
                stderr_output = process.stderr.read()
                logger.error(f"FFmpeg failed", return_code=return_code, stderr=stderr_output)
                return None, f"FFmpeg error: {stderr_output[:200] if stderr_output else 'Unknown error'}"

        except Exception as e:
            logger.error(f"FFmpeg download error: {str(e)}")
            if os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except:
                    pass
            return None, f"Download error: {str(e)}"

    def cleanup_temp_files(self):
        """Clean up old temporary files"""
        try:
            temp_dir = Path(self.config.TEMP_DIR)
            current_time = time.time()

            for file_path in temp_dir.glob("*.mp4"):
                # Remove files older than 2 hours
                if current_time - file_path.stat().st_mtime > 7200:
                    file_path.unlink()
                    logger.debug(f"Cleaned up old temp file: {file_path}")

        except Exception as e:
            logger.error(f"Error cleaning temp files: {str(e)}")

class PyrogramBot:
    """Main Telegram bot class using Pyrogram"""

    def __init__(self):
        self.config = ConfigManager()
        self.processor = VideoProcessor(self.config)

        # Initialize Pyrogram client
        self.app = Client(
            "classplus_bot",
            api_id=self.config.API_ID,
            api_hash=self.config.API_HASH,
            bot_token=self.config.BOT_TOKEN,
            workdir="./sessions"
        )

        self.stats = {
            'videos_processed': 0,
            'videos_successful': 0,
            'videos_failed': 0,
            'total_size_mb': 0.0,
            'start_time': datetime.now()
        }

        # Progress tracking
        self.current_progress = {}

    async def initialize(self):
        """Initialize bot components"""
        await self.processor.start_session()

        # Create sessions directory
        Path("./sessions").mkdir(exist_ok=True)

        # Register handlers
        self.register_handlers()

        logger.info("Pyrogram bot initialized successfully")

    def register_handlers(self):
        """Register message handlers"""

        @self.app.on_message(filters.command("start"))
        async def start_command(client, message):
            await self.handle_start(client, message)

        @self.app.on_message(filters.command("help"))
        async def help_command(client, message):
            await self.handle_help(client, message)

        @self.app.on_message(filters.command("stats"))
        async def stats_command(client, message):
            await self.handle_stats(client, message)

        @self.app.on_message(filters.command("health"))
        async def health_command(client, message):
            await self.handle_health(client, message)

        @self.app.on_message(filters.document)
        async def document_handler(client, message):
            await self.handle_document(client, message)

        @self.app.on_message(filters.text & ~filters.command([
            "start", "help", "stats", "health"
        ]))
        async def text_handler(client, message):
            await self.handle_text(client, message)

    async def handle_start(self, client, message):
        """Handle /start command"""
        welcome_text = """
🎥 **ClassPlus Video Downloader Bot v2.0**
*Powered by Pyrogram - Now with 2GB file support!*

**📋 How to use:**
1. Send me a `.txt` file with video links
2. Or paste video links directly in chat

**📝 Format:**
Title 1:https://media-cdn.classplusapp.com/.../master.m3u8
Title 2:https://media-cdn.classplusapp.com/.../master.m3u8
**✨ New Features:**
🚀 **2GB file upload support** with Pyrogram
✅ Enhanced HLS segment downloading
✅ Real-time progress tracking
✅ Improved error handling
✅ Support for private chats and channels

**🤖 Commands:**
/start - Show this help
/help - Detailed guide  
/stats - Bot statistics
/health - System health check

**Ready to download up to 2GB videos!** 📤
        """

        await message.reply_text(welcome_text)
        logger.info("Start command executed", user_id=message.from_user.id)

    async def handle_help(self, client, message):
        """Handle /help command"""
        help_text = """
📚 **Detailed Help Guide - Pyrogram Edition**

**🎯 Supported Formats:**
• ClassPlus M3U8 URLs (HLS streams)
• Single videos and batch files
• Text files (.txt) up to 2GB
• **Video files up to 2GB** ⭐

**📁 File Format Example:**
Lecture 1:https://media-cdn.classplusapp.com/.../master.m3u8
Lecture 2:https://media-cdn.classplusapp.com/.../master.m3u8
Advanced Topics:https://media-cdn.classplusapp.com/.../master.m3u8
**⚙️ Processing Details:**
• Videos processed sequentially for stability
• FFmpeg merges HLS segments automatically
• **Maximum file size: 2GB per video** 🚀
• Enhanced timeout: 1 hour for large files
• Real-time progress updates

**🔧 Large File Features:**
• Progress tracking during download
• Automatic resume on connection issues
• Optimized FFmpeg settings for 2GB files
• Smart cleanup of temporary files

**📺 Channel Usage:**
1. Add bot to your channel as admin
2. Send video list in the channel
3. Bot uploads videos to the same channel
4. **Now supports 2GB videos in channels!**

**💡 Pro Tips:**
• Use descriptive titles for organization
• Bot handles large files automatically
• Progress updates every 10% during processing
• Works perfectly with premium Telegram accounts

**Powered by Pyrogram for maximum file size support!** 🚀
        """

        await message.reply_text(help_text)

    async def handle_stats(self, client, message):
        """Handle /stats command"""
        uptime = datetime.now() - self.stats['start_time']

        stats_text = f"""
📊 **Bot Statistics - Pyrogram Edition**

**🎬 Video Processing:**
• Total Processed: {self.stats['videos_processed']}
• Successful: {self.stats['videos_successful']}
• Failed: {self.stats['videos_failed']}
• Success Rate: {(self.stats['videos_successful']/max(1, self.stats['videos_processed'])*100):.1f}%

**📦 Data Transfer:**
• Total Size: {self.stats['total_size_mb']:.2f} MB
• **2GB Support**: ✅ Active

**⏱️ System Status:**
• Uptime: {str(uptime).split('.')[0]}
• Memory Usage: {psutil.Process().memory_info().rss / 1024 / 1024:.1f} MB
• CPU Usage: {psutil.cpu_percent()}%

**🚀 Performance:**
• Max File Size: {self.config.MAX_FILE_SIZE_MB} MB
• Download Timeout: {self.config.DOWNLOAD_TIMEOUT}s
• Upload Timeout: {self.config.UPLOAD_TIMEOUT}s

**Pyrogram Power: Unlimited by Telegram bot restrictions!** ⚡
        """

        await message.reply_text(stats_text)

    async def handle_health(self, client, message):
        """Handle /health command"""
        try:
            # Check FFmpeg
            ffmpeg_check = subprocess.run(['ffmpeg', '-version'], 
                                        capture_output=True, text=True, timeout=5)
            ffmpeg_ok = ffmpeg_check.returncode == 0

            # Check disk space
            temp_dir = Path(self.config.TEMP_DIR)
            disk_usage = shutil.disk_usage(temp_dir)
            free_gb = disk_usage.free / (1024**3)

            # Check session
            session_ok = self.processor.session is not None

            # Check Pyrogram client
            pyrogram_ok = hasattr(self.app, 'session') and self.app.is_connected

            health_text = f"""
🏥 **Health Check - Pyrogram Edition**

**🔧 Core Components:**
• FFmpeg: {'✅ OK' if ffmpeg_ok else '❌ ERROR'}
• HTTP Session: {'✅ Active' if session_ok else '❌ Inactive'}
• Pyrogram Client: {'✅ Connected' if pyrogram_ok else '❌ Disconnected'}
• Temp Directory: {'✅ Available' if temp_dir.exists() else '❌ Missing'}

**💾 Storage:**
• Free Disk Space: {free_gb:.2f} GB
• Temp Directory: `{self.config.TEMP_DIR}`
• **2GB File Support**: ✅ Ready

**⚡ Performance:**
• Memory: {psutil.Process().memory_info().rss / 1024 / 1024:.1f} MB
• CPU: {psutil.cpu_percent(interval=1):.1f}%
• Max Upload: {self.config.MAX_FILE_SIZE_MB} MB

**🚀 Pyrogram Features:**
• Large File Upload: ✅ Up to 2GB
• Progress Tracking: ✅ Real-time
• Resume Downloads: ✅ Automatic

**Overall Status: {'🟢 HEALTHY' if all([ffmpeg_ok, session_ok, temp_dir.exists()]) else '🔴 ISSUES DETECTED'}**
            """

            await message.reply_text(health_text)

        except Exception as e:
            await message.reply_text(f"❌ Health check failed: {str(e)}")

    async def handle_document(self, client, message):
        """Handle uploaded documents"""
        document = message.document

        # Validate file type
        if not document.file_name.lower().endswith('.txt'):
            await message.reply_text("❌ Please send a `.txt` file with video links.")
            return

        # Updated file size validation - 2GB limit
        max_input_size = self.config.INPUT_FILE_SIZE_MB * 1024 * 1024
        if document.file_size > max_input_size:
            size_mb = document.file_size / (1024 * 1024)
            await message.reply_text(
                f"❌ File too large: {size_mb:.1f}MB\n"
                f"Maximum size: {self.config.INPUT_FILE_SIZE_MB}MB (2GB)"
            )
            return

        try:
            # Download and read file with Pyrogram
            await message.reply_text("📥 Downloading file... Please wait.")

            # Download file using Pyrogram's efficient download
            file_path = await message.download(
                file_name=f"{self.config.TEMP_DIR}/input_{int(time.time())}.txt"
            )

            # Read file content
            async with aiofiles.open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()

            # Clean up input file
            os.unlink(file_path)

            logger.info(f"Document received", 
                       filename=document.file_name, 
                       size_mb=f"{document.file_size/(1024*1024):.2f}MB",
                       user_id=message.from_user.id)

            await self.process_video_content(client, message, content)

        except Exception as e:
            logger.error(f"Error handling document: {str(e)}")
            await message.reply_text(f"❌ Error processing document: {str(e)}")

    async def handle_text(self, client, message):
        """Handle text messages with video links"""
        text = message.text.strip()

        # Check if text contains video links
        if ':https://' in text and any(domain in text for domain in ['classplusapp.com', 'classplus.co']):
            await self.process_video_content(client, message, text)
        else:
            help_msg = """
❓ **How to use:**

Send me either:
1. A `.txt` file with video links (up to 2GB), or
2. Paste video links directly like this:
Title:https://media-cdn.classplusapp.com/.../master.m3u8
**Now supporting 2GB video uploads with Pyrogram!** 🚀

Use /help for detailed instructions.
            """
            await message.reply_text(help_msg)

    async def process_video_content(self, client, message, content: str):
        """Process video content from file or text"""
        # Parse videos
        videos = self.processor.parse_video_file(content)

        if not videos:
            await message.reply_text(
                "❌ No valid video links found.\n\n"
                "Expected format:\n"
                "`Title:https://media-cdn.classplusapp.com/.../master.m3u8`"
            )
            return

        # Start processing
        await self.process_video_batch(client, message, videos)

    async def process_video_batch(self, client, message, videos: List[Dict]):
        """Process a batch of videos with 2GB support"""
        chat_id = message.chat.id
        total_videos = len(videos)

        # Send initial status
        status_msg = f"""
📋 **Processing Started - Pyrogram Edition**

• Found: {total_videos} videos
• Method: Sequential processing with FFmpeg
• **Max file size: 2GB per video** 🚀
• Estimated time: {total_videos * 3} - {total_videos * 10} minutes

⏳ **Starting downloads...** Large files supported!
        """
        await message.reply_text(status_msg)

        # Process videos
        success_count = 0
        error_count = 0
        total_size = 0.0

        for i, video in enumerate(videos, 1):
            title = video['title']
            url = video['url']
            line_num = video['line_number']

            try:
                logger.info(f"Processing video {i}/{total_videos}: {title}")

                # Send progress update
                progress_msg = f"🎬 Processing {i}/{total_videos}: {title[:50]}..."
                progress_message = await client.send_message(chat_id, progress_msg)

                # Progress callback for real-time updates
                last_progress = [0]  # Use list to modify from inner function

                async def progress_callback(current_time):
                    try:
                        # Update progress every 30 seconds to avoid spam
                        if time.time() - last_progress[0] > 30:
                            await progress_message.edit_text(
                                f"🎬 Processing {i}/{total_videos}: {title[:50]}...\n"
                                f"⏱️ Downloaded: {current_time//60}:{current_time%60:02d}"
                            )
                            last_progress[0] = time.time()
                    except MessageNotModified:
                        pass
                    except Exception:
                        pass

                # Step 1: Get processed URL
                processed_url = await self.processor.get_processed_m3u8_url(url)

                # Step 2: Download with FFmpeg
                video_path, error = await self.processor.download_with_ffmpeg(
                    processed_url, title, progress_callback
                )

                if not video_path:
                    error_msg = f"❌ Download failed: {title}\nLine {line_num}: {error}"
                    await client.send_message(chat_id, error_msg)
                    error_count += 1
                    self.stats['videos_failed'] += 1
                    continue

                # Step 3: Upload with Pyrogram (2GB support)
                upload_success = await self.upload_large_video(
                    client, chat_id, video_path, title, i, total_videos
                )

                # Get file size for stats
                if os.path.exists(video_path):
                    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
                    total_size += file_size_mb

                    # Clean up
                    os.unlink(video_path)

                if upload_success:
                    success_count += 1
                    self.stats['videos_successful'] += 1
                    logger.info(f"✅ Successfully processed: {title}")
                else:
                    error_count += 1
                    self.stats['videos_failed'] += 1

                self.stats['videos_processed'] += 1

                # Brief delay between videos
                await asyncio.sleep(3)

            except Exception as e:
                logger.error(f"Unexpected error processing {title}: {str(e)}")
                error_msg = f"❌ Unexpected error: {title}\nLine {line_num}: {str(e)}"
                await client.send_message(chat_id, error_msg)
                error_count += 1
                self.stats['videos_failed'] += 1

        # Update global stats
        self.stats['total_size_mb'] += total_size

        # Send completion summary
        completion_msg = f"""
🎉 **Processing Complete - Pyrogram Edition!**

📊 **Results:**
• ✅ Successful: {success_count}
• ❌ Failed: {error_count}
• 📈 Total: {total_videos}
• 📦 Total Size: {total_size:.2f} MB

**Success Rate: {(success_count/total_videos*100):.1f}%**

🚀 **Powered by Pyrogram - 2GB files supported!**
Thank you for using ClassPlus Video Downloader! ⚡
        """

        await client.send_message(chat_id, completion_msg)

        # Clean up old temp files
        self.processor.cleanup_temp_files()

    async def upload_large_video(self, client, chat_id: int, video_path: str, title: str, current: int, total: int) -> bool:
        """Upload video using Pyrogram with 2GB support and progress tracking"""
        try:
            file_size = os.path.getsize(video_path)
            size_mb = file_size / (1024 * 1024)

            logger.info(f"Uploading {size_mb:.1f}MB video with Pyrogram")

            # Progress callback for upload
            last_progress = [0, 0]  # [last_update_time, last_percentage]

            def progress_callback(current, total):
                try:
                    if total > 0:
                        percentage = (current / total) * 100
                        current_time = time.time()

                        # Update progress every 10% or every 30 seconds
                        if (percentage - last_progress[1] >= 10) or (current_time - last_progress[0] > 30):
                            asyncio.create_task(client.send_message(
                                chat_id,
                                f"📤 Uploading {current}/{total}: {title[:50]}...\n"
                                f"📊 Progress: {percentage:.1f}% ({current / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB)"
                            ))
                            last_progress[0] = current_time
                            last_progress[1] = percentage
                except Exception:
                    pass

            # Upload with Pyrogram (supports up to 2GB)
            await client.send_video(
                chat_id=chat_id,
                video=video_path,
                caption=f"**{title}**\n\n📦 Size: {size_mb:.1f}MB\n🚀 Uploaded via Pyrogram",
                progress=progress_callback,
                thumb=None,  # You can add thumbnail generation here
                duration=0,  # Can be extracted from video if needed
                width=0,
                height=0,
                supports_streaming=True
            )

            logger.info(f"✅ Successfully uploaded {size_mb:.1f}MB video")
            return True

        except FloodWait as e:
            logger.warning(f"Rate limited, waiting {e.x} seconds")
            await asyncio.sleep(e.x)
            return await self.upload_large_video(client, chat_id, video_path, title, current, total)

        except Exception as e:
            logger.error(f"Upload error: {str(e)}")
            await client.send_message(
                chat_id,
                f"❌ Upload failed: {title}\nError: {str(e)}"
            )
            return False

    async def run(self):
        """Run the Pyrogram bot"""
        try:
            logger.info("Starting ClassPlus Video Downloader Bot with Pyrogram...")
            await self.initialize()

            # Start the Pyrogram client
            await self.app.start()
            logger.info("🚀 Pyrogram bot started successfully with 2GB support!")

            # Keep the bot running
            await asyncio.Event().wait()

        except Exception as e:
            logger.error(f"Bot crashed: {str(e)}")
            raise

        finally:
            await self.processor.close_session()
            await self.app.stop()

async def main():
    """Main entry point"""
    bot = PyrogramBot()

    # Set up signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot shutdown requested")
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())
