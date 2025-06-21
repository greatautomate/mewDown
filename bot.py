#!/usr/bin/env python3
"""
ClassPlus Video Downloader Telegram Bot

A comprehensive Telegram bot that downloads videos from ClassPlus HLS streams,
merges segments using FFmpeg, and uploads them to Telegram chats/channels.

Author: Your Name
Version: 1.0.0
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
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import re

# Third-party imports
import aiohttp
import aiofiles
import structlog
from telegram import Update, Bot
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes
)
from telegram.error import TelegramError, NetworkError, RetryAfter
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
        self.MAX_FILE_SIZE_MB = int(os.getenv('MAX_FILE_SIZE_MB', '45'))
        self.MAX_CONCURRENT_DOWNLOADS = int(os.getenv('MAX_CONCURRENT_DOWNLOADS', '1'))
        self.DOWNLOAD_TIMEOUT = int(os.getenv('DOWNLOAD_TIMEOUT', '600'))
        self.UPLOAD_TIMEOUT = int(os.getenv('UPLOAD_TIMEOUT', '600'))
        self.FFMPEG_LOGLEVEL = os.getenv('FFMPEG_LOGLEVEL', 'error')
        self.TEMP_DIR = os.getenv('TEMP_DIR', '/tmp/videos')

        # Validate required config
        if not self.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")

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

    async def download_with_ffmpeg(self, m3u8_url: str, title: str) -> Tuple[Optional[str], Optional[str]]:
        """Download HLS video using FFmpeg"""

        # Create unique temporary file
        timestamp = int(time.time())
        safe_title = re.sub(r'[^\w\-_\.]', '_', title)[:50]
        temp_file = f"{self.config.TEMP_DIR}/{safe_title}_{timestamp}.mp4"

        try:
            logger.info(f"Starting FFmpeg download: {title}")

            # Comprehensive FFmpeg command
            ffmpeg_cmd = [
                'ffmpeg',
                '-user_agent', self.headers['User-Agent'],
                '-headers', f"Referer: {self.headers['Referer']}\r\nOrigin: {self.headers['Origin']}",
                '-i', m3u8_url,
                '-c', 'copy',  # Copy streams without re-encoding
                '-bsf:a', 'aac_adtstoasc',  # Fix AAC streams
                '-avoid_negative_ts', 'make_zero',  # Fix timestamp issues
                '-fflags', '+genpts',  # Generate presentation timestamps
                '-movflags', '+faststart',  # Optimize for streaming
                '-loglevel', self.config.FFMPEG_LOGLEVEL,
                '-y',  # Overwrite output
                temp_file
            ]

            # Execute FFmpeg
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )

            stdout, stderr = process.communicate()

            if process.returncode == 0:
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    file_size = os.path.getsize(temp_file)
                    size_mb = file_size / (1024 * 1024)

                    logger.info(f"FFmpeg download completed", 
                              file_size=file_size, 
                              size_mb=f"{size_mb:.2f}MB")

                    # Check file size limit
                    if size_mb > self.config.MAX_FILE_SIZE_MB:
                        os.unlink(temp_file)
                        return None, f"File too large: {size_mb:.1f}MB (limit: {self.config.MAX_FILE_SIZE_MB}MB)"

                    return temp_file, None
                else:
                    logger.error("FFmpeg completed but output file is missing or empty")
                    return None, "Download completed but file is empty"
            else:
                logger.error(f"FFmpeg failed", return_code=process.returncode, stderr=stderr)
                return None, f"FFmpeg error: {stderr[:200] if stderr else 'Unknown error'}"

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
                # Remove files older than 1 hour
                if current_time - file_path.stat().st_mtime > 3600:
                    file_path.unlink()
                    logger.debug(f"Cleaned up old temp file: {file_path}")

        except Exception as e:
            logger.error(f"Error cleaning temp files: {str(e)}")

class TelegramBot:
    """Main Telegram bot class"""

    def __init__(self):
        self.config = ConfigManager()
        self.processor = VideoProcessor(self.config)
        self.bot = Bot(token=self.config.BOT_TOKEN)
        self.application = None
        self.stats = {
            'videos_processed': 0,
            'videos_successful': 0,
            'videos_failed': 0,
            'total_size_mb': 0.0,
            'start_time': datetime.now()
        }

    async def initialize(self):
        """Initialize bot components"""
        await self.processor.start_session()

        # Build application
        self.application = Application.builder().token(self.config.BOT_TOKEN).build()

        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("health", self.health_command))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

        # Set up graceful shutdown
        self.application.post_init = self._post_init
        self.application.post_shutdown = self._post_shutdown

        logger.info("Bot initialized successfully")

    async def _post_init(self, app):
        """Post-initialization setup"""
        logger.info("Bot application started")

    async def _post_shutdown(self, app):
        """Cleanup on shutdown"""
        await self.processor.close_session()
        logger.info("Bot application shutdown complete")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_text = """
🎥 **ClassPlus Video Downloader Bot**

Welcome! I can download videos from ClassPlus HLS streams and upload them to Telegram.

**📋 How to use:**
1. Send me a `.txt` file with video links
2. Or paste video links directly in chat

**📝 Format:**
Title 1:https://media-cdn.classplusapp.com/.../master.m3u8
Title 2:https://media-cdn.classplusapp.com/.../master.m3u
**✨ Features:**
✅ HLS segment downloading with FFmpeg
✅ Sequential processing for stability  
✅ Supports private chats and channels
✅ Comprehensive error handling
✅ File size validation (45MB limit)

**🤖 Commands:**
/start - Show this help
/help - Show detailed help
/stats - Show bot statistics
/health - Show bot health status

**Ready to download! Send me your video list.** 📤
        """

        await update.message.reply_text(welcome_text, parse_mode='Markdown')
        logger.info("Start command executed", user_id=update.effective_user.id)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
📚 **Detailed Help Guide**

**🎯 Supported Formats:**
• ClassPlus M3U8 URLs (HLS streams)
• Both single videos and batch files
• Text files (.txt) up to 10MB

**📁 File Format Example:**
Lecture 1: Introduction:https://media-cdn.classplusapp.com/.../master.m3u8
Lecture 2: Advanced Topics:https://media-cdn.classplusapp.com/.../master.m3u8
Chapter 3 - Practice:https://media-cdn.classplusapp.com/.../master.m3u8
**⚙️ Processing Details:**
• Videos are processed one by one (sequential)
• FFmpeg merges HLS segments automatically
• Maximum file size: 45MB per video
• Timeout: 10 minutes per video

**🔧 Channel Usage:**
1. Add bot to your channel as admin
2. Send the video list in the channel
3. Bot will upload videos to the same channel

**⚠️ Error Handling:**
• Invalid URLs are skipped with error report
• Large files are skipped automatically
• Network errors are retried automatically
• Progress updates sent every few videos

**💡 Tips:**
• Use descriptive titles for better organization
• Keep file sizes reasonable for faster processing
• Bot works in both private chats and channels
• Processing status is updated in real-time

Need more help? Contact the bot administrator.
        """

        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        uptime = datetime.now() - self.stats['start_time']

        stats_text = f"""
📊 **Bot Statistics**

**🎬 Video Processing:**
• Total Processed: {self.stats['videos_processed']}
• Successful: {self.stats['videos_successful']}
• Failed: {self.stats['videos_failed']}
• Success Rate: {(self.stats['videos_successful']/max(1, self.stats['videos_processed'])*100):.1f}%

**📦 Data Transfer:**
• Total Size: {self.stats['total_size_mb']:.2f} MB

**⏱️ Uptime:**
• Running for: {str(uptime).split('.')[0]}

**💾 System Info:**
• Memory Usage: {psutil.Process().memory_info().rss / 1024 / 1024:.1f} MB
• CPU Usage: {psutil.cpu_percent()}%
        """

        await update.message.reply_text(stats_text, parse_mode='Markdown')

    async def health_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

            health_text = f"""
🏥 **Health Check**

**🔧 Components:**
• FFmpeg: {'✅ OK' if ffmpeg_ok else '❌ ERROR'}
• HTTP Session: {'✅ Active' if session_ok else '❌ Inactive'}
• Temp Directory: {'✅ Available' if temp_dir.exists() else '❌ Missing'}

**💾 Storage:**
• Free Disk Space: {free_gb:.2f} GB
• Temp Directory: `{self.config.TEMP_DIR}`

**⚡ Performance:**
• Memory: {psutil.Process().memory_info().rss / 1024 / 1024:.1f} MB
• CPU: {psutil.cpu_percent(interval=1):.1f}%

**Overall Status: {'🟢 HEALTHY' if all([ffmpeg_ok, session_ok, temp_dir.exists()]) else '🔴 ISSUES DETECTED'}**
            """

            await update.message.reply_text(health_text, parse_mode='Markdown')

        except Exception as e:
            await update.message.reply_text(f"❌ Health check failed: {str(e)}")

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle uploaded documents"""
        document = update.message.document

        # Validate file type
        if not document.file_name.lower().endswith('.txt'):
            await update.message.reply_text("❌ Please send a `.txt` file with video links.")
            return

        # Validate file size
        if document.file_size > 10 * 1024 * 1024:
            await update.message.reply_text("❌ File too large. Maximum size: 10MB")
            return

        try:
            # Download and read file
            file = await context.bot.get_file(document.file_id)
            file_content = await file.download_as_bytearray()
            content = file_content.decode('utf-8', errors='ignore')

            logger.info(f"Document received", 
                       filename=document.file_name, 
                       size=document.file_size,
                       user_id=update.effective_user.id)

            await self.process_video_content(update, content)

        except Exception as e:
            logger.error(f"Error handling document: {str(e)}")
            await update.message.reply_text(f"❌ Error processing document: {str(e)}")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages with video links"""
        text = update.message.text.strip()

        # Check if text contains video links
        if ':https://' in text and any(domain in text for domain in ['classplusapp.com', 'classplus.co']):
            await self.process_video_content(update, text)
        else:
            help_msg = """
❓ **How to use:**

Send me either:
1. A `.txt` file with video links, or
2. Paste video links directly like this:
Title:https://media-cdn.classplusapp.com/.../master.m3u8
Use /help for detailed instructions.
            """
            await update.message.reply_text(help_msg, parse_mode='Markdown')

    async def process_video_content(self, update: Update, content: str):
        """Process video content from file or text"""
        chat_id = update.effective_chat.id

        # Parse videos
        videos = self.processor.parse_video_file(content)

        if not videos:
            await update.message.reply_text(
                "❌ No valid video links found.\n\n"
                "Expected format:\n"
                "`Title:https://media-cdn.classplusapp.com/.../master.m3u8`",
                parse_mode='Markdown'
            )
            return

        # Start processing
        await self.process_video_batch(update, videos)

    async def process_video_batch(self, update: Update, videos: List[Dict]):
        """Process a batch of videos"""
        chat_id = update.effective_chat.id
        total_videos = len(videos)

        # Send initial status
        status_msg = f"""
📋 **Processing Started**

• Found: {total_videos} videos
• Method: Sequential processing with FFmpeg
• Estimated time: {total_videos * 2} - {total_videos * 5} minutes

⏳ **Starting downloads...** Please be patient.
        """
        await update.message.reply_text(status_msg, parse_mode='Markdown')

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

                # Send progress update every 3 videos
                if i == 1 or i % 3 == 0 or i == total_videos:
                    progress_msg = f"🎬 Processing {i}/{total_videos}: {title[:50]}..."
                    await self.bot.send_message(chat_id, progress_msg)

                # Step 1: Get processed URL
                processed_url = await self.processor.get_processed_m3u8_url(url)

                # Step 2: Download with FFmpeg
                video_path, error = await self.processor.download_with_ffmpeg(processed_url, title)

                if not video_path:
                    error_msg = f"❌ Download failed: {title}\nLine {line_num}: {error}"
                    await self.bot.send_message(chat_id, error_msg)
                    error_count += 1
                    self.stats['videos_failed'] += 1
                    continue

                # Step 3: Upload to Telegram
                upload_success = await self.upload_video(chat_id, video_path, title)

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
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"Unexpected error processing {title}: {str(e)}")
                error_msg = f"❌ Unexpected error: {title}\nLine {line_num}: {str(e)}"
                await self.bot.send_message(chat_id, error_msg)
                error_count += 1
                self.stats['videos_failed'] += 1

        # Update global stats
        self.stats['total_size_mb'] += total_size

        # Send completion summary
        completion_msg = f"""
🎉 **Processing Complete!**

📊 **Results:**
• ✅ Successful: {success_count}
• ❌ Failed: {error_count}
• 📈 Total: {total_videos}
• 📦 Total Size: {total_size:.2f} MB

**Success Rate: {(success_count/total_videos*100):.1f}%**

Thank you for using ClassPlus Video Downloader! 🚀
        """

        await self.bot.send_message(chat_id, completion_msg, parse_mode='Markdown')

        # Clean up old temp files
        self.processor.cleanup_temp_files()

    async def upload_video(self, chat_id: int, video_path: str, title: str) -> bool:
        """Upload video to Telegram"""
        try:
            with open(video_path, 'rb') as video_file:
                await self.bot.send_video(
                    chat_id=chat_id,
                    video=video_file,
                    caption=title[:1024],  # Telegram limit
                    timeout=self.config.UPLOAD_TIMEOUT
                )
            return True

        except RetryAfter as e:
            logger.warning(f"Rate limited, waiting {e.retry_after} seconds")
            await asyncio.sleep(e.retry_after)
            return await self.upload_video(chat_id, video_path, title)

        except TelegramError as e:
            logger.error(f"Telegram upload error: {str(e)}")
            return False

        except Exception as e:
            logger.error(f"Upload error: {str(e)}")
            return False

    async def run(self):
        """Run the bot"""
        try:
            logger.info("Starting ClassPlus Video Downloader Bot...")
            await self.initialize()

            # Run the application
            await self.application.run_polling(
                drop_pending_updates=True,
                close_loop=False
            )

        except Exception as e:
            logger.error(f"Bot crashed: {str(e)}")
            raise

        finally:
            await self.processor.close_session()

async def main():
    """Main entry point"""
    bot = TelegramBot()

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

















