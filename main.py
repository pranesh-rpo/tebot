BOT_TOKEN = '8498452597:AAF0SZHC0_ONB-vFV90eD6PU-gp-HEaSKVM'

import logging
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes
from telegram.error import TelegramError, NetworkError, TimedOut, BadRequest, Forbidden
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError, 
    AuthKeyUnregisteredError, PhoneNumberInvalidError, PhoneCodeExpiredError,
    UserDeactivatedError, UserDeactivatedBanError, PhoneNumberBannedError,
    SlowModeWaitError, ChannelPrivateError, ChatWriteForbiddenError
)
from telethon.tl.functions.account import UpdateProfileRequest
import asyncio
import random
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Set
import traceback
import re

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = '8237657814:AAFdY8o-lGsdzKZstQtOSGslXl-5PcQzP88'
API_ID = '27941881'
API_HASH = 'a349f6fd32565f894e90e76470cc786d'
REQUIRED_CHANNEL = '@ziddyswallet'
OWNER_USERNAME = '@ziddysbot'

# Conversation states
VERIFY_JOIN, PHONE, OTP, TWO_FA, MESSAGE, ACCOUNT_SLOTS, ACCOUNT_DASHBOARD, SELECT_SLOT, SETTINGS_MENU = range(9)

# Global state management
user_sessions: Dict[int, Dict[str, Any]] = {}
auto_send_tasks: Dict[int, Dict[str, asyncio.Task]] = {}
user_data_store: Dict[int, Dict[str, Any]] = {}
message_cleanup_tasks: Dict[int, asyncio.Task] = {}
broadcast_status: Dict[str, bool] = {}
status_locks: Dict[str, asyncio.Lock] = {}

# Anti-spam tracking
channel_send_history: Dict[str, Dict[int, datetime]] = {}
account_daily_sends: Dict[str, Dict[str, int]] = {}
failed_channels: Dict[str, Set[int]] = {}

# Constants
MAX_ACCOUNTS = 5
MAX_MESSAGE_LENGTH = 4000
MAX_PHONE_LENGTH = 20
MIN_PHONE_LENGTH = 10
CLEANUP_INTERVAL = 3600
MESSAGE_RETENTION_HOURS = 24
BROADCAST_BATCHES = 6
CONNECTION_TIMEOUT = 20
OPERATION_TIMEOUT = 30

# Anti-spam settings
MIN_DELAY_BETWEEN_MESSAGES = 5
MAX_DELAY_BETWEEN_MESSAGES = 12
MIN_DELAY_SAME_CHANNEL = 3600
MAX_DAILY_MESSAGES_PER_ACCOUNT = 100
BATCH_INTERVAL_MIN = 20 * 60
BATCH_INTERVAL_MAX = 30 * 60
CYCLE_WAIT = 3 * 60 * 60
MAX_RETRY_ATTEMPTS = 2
WARMUP_PERIOD_DAYS = 3
WARMUP_MAX_DAILY = 30
RANDOM_SKIP_PROBABILITY = 0.15
SLOW_MODE_WAIT_MAX = 300


def get_status_key(user_id: int, slot_id: str) -> str:
    """Generate unique key for broadcast status"""
    return f"{user_id}_{slot_id}"


def is_broadcast_running(user_id: int, slot_id: str) -> bool:
    """Check if broadcast is running"""
    key = get_status_key(user_id, slot_id)
    return broadcast_status.get(key, False)


def set_broadcast_running(user_id: int, slot_id: str, running: bool):
    """Set broadcast status"""
    key = get_status_key(user_id, slot_id)
    broadcast_status[key] = running
    logger.info(f"Broadcast status for {key}: {'RUNNING' if running else 'STOPPED'}")


async def get_status_lock(user_id: int, slot_id: str) -> asyncio.Lock:
    """Get or create a lock for status updates"""
    key = get_status_key(user_id, slot_id)
    if key not in status_locks:
        status_locks[key] = asyncio.Lock()
    return status_locks[key]


class AntiSpamManager:
    """Manages anti-spam measures"""
    
    @staticmethod
    def should_skip_channel() -> bool:
        return random.random() < RANDOM_SKIP_PROBABILITY
    
    @staticmethod
    def get_natural_delay() -> float:
        delay = random.gauss(
            (MIN_DELAY_BETWEEN_MESSAGES + MAX_DELAY_BETWEEN_MESSAGES) / 2,
            2
        )
        return max(MIN_DELAY_BETWEEN_MESSAGES, min(MAX_DELAY_BETWEEN_MESSAGES, delay))
    
    @staticmethod
    def can_send_to_channel(slot_id: str, channel_id: int) -> bool:
        if slot_id not in channel_send_history:
            channel_send_history[slot_id] = {}
        
        last_send = channel_send_history[slot_id].get(channel_id)
        if not last_send:
            return True
        
        elapsed = (datetime.now() - last_send).total_seconds()
        return elapsed >= MIN_DELAY_SAME_CHANNEL
    
    @staticmethod
    def record_send(slot_id: str, channel_id: int):
        if slot_id not in channel_send_history:
            channel_send_history[slot_id] = {}
        channel_send_history[slot_id][channel_id] = datetime.now()
    
    @staticmethod
    def can_send_today(slot_id: str, account_created: datetime) -> tuple:
        today = datetime.now().date().isoformat()
        
        if slot_id not in account_daily_sends:
            account_daily_sends[slot_id] = {}
        
        account_daily_sends[slot_id] = {
            date: count for date, count in account_daily_sends[slot_id].items()
            if date == today
        }
        
        current_count = account_daily_sends[slot_id].get(today, 0)
        
        account_age = (datetime.now() - account_created).days
        if account_age < WARMUP_PERIOD_DAYS:
            max_sends = WARMUP_MAX_DAILY
        else:
            max_sends = MAX_DAILY_MESSAGES_PER_ACCOUNT
        
        return current_count < max_sends, max_sends - current_count
    
    @staticmethod
    def increment_daily_count(slot_id: str):
        today = datetime.now().date().isoformat()
        if slot_id not in account_daily_sends:
            account_daily_sends[slot_id] = {}
        account_daily_sends[slot_id][today] = account_daily_sends[slot_id].get(today, 0) + 1
    
    @staticmethod
    def mark_failed_channel(slot_id: str, channel_id: int):
        if slot_id not in failed_channels:
            failed_channels[slot_id] = set()
        failed_channels[slot_id].add(channel_id)
    
    @staticmethod
    def is_channel_failed(slot_id: str, channel_id: int) -> bool:
        if slot_id not in failed_channels:
            return False
        return channel_id in failed_channels[slot_id]
    
    @staticmethod
    def sanitize_message(message: str) -> str:
        message = re.sub(r'([\U0001F600-\U0001F64F]){4,}', r'\1\1\1', message)
        message = re.sub(r'!{3,}', '!!', message)
        
        words = message.split()
        for i, word in enumerate(words):
            if len(word) > 3 and word.isupper():
                words[i] = word.capitalize()
        message = ' '.join(words)
        
        message = re.sub(r'\s+', ' ', message).strip()
        return message


class DatabaseManager:
    """Database manager"""
    
    def __init__(self, db_path: str = 'ziddys_ads.db'):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA journal_mode=WAL')
                return conn
            except sqlite3.Error as e:
                logger.error(f"Database connection error: {e}")
                if attempt == max_retries - 1:
                    raise
                asyncio.sleep(0.5)
    
    def init_db(self):
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    verified INTEGER DEFAULT 0,
                    premium INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_cleanup TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    slot_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    display_name TEXT,
                    is_active INTEGER DEFAULT 1,
                    last_broadcast TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS analytics (
                    analytics_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    slot_id TEXT NOT NULL,
                    sent_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    total_groups INTEGER DEFAULT 0,
                    broadcast_type TEXT DEFAULT 'text',
                    duration_seconds INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_messages (
                    message_id INTEGER,
                    user_id INTEGER,
                    chat_id INTEGER,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(message_id, user_id)
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_analytics_user_slot ON analytics(user_id, slot_id)')
            
            conn.commit()
            logger.info("Database initialized successfully")
        except sqlite3.Error as e:
            logger.error(f"Database initialization error: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def create_user(self, user_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error creating user: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    def verify_user(self, user_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET verified = 1 WHERE user_id = ?', (user_id,))
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error verifying user: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    def is_verified(self, user_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT verified FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            return result and result['verified'] == 1
        except sqlite3.Error as e:
            logger.error(f"Error checking verification: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    def is_premium(self, user_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT premium FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            return result and result['premium'] == 1
        except sqlite3.Error as e:
            return False
        finally:
            if conn:
                conn.close()
    
    def add_account(self, user_id: int, phone: str, display_name: str, slot_id: str) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO accounts (slot_id, user_id, phone, display_name, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            ''', (slot_id, user_id, phone, display_name))
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Error adding account: {e}")
            return False
        finally:
            if conn:
                conn.close()
    
    def get_user_accounts(self, user_id: int) -> list:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM accounts WHERE user_id = ? AND is_active = 1', (user_id,))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            return []
        finally:
            if conn:
                conn.close()
    
    def get_account_by_slot(self, user_id: int, slot_id: str) -> Optional[Dict]:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM accounts WHERE user_id = ? AND slot_id = ? AND is_active = 1', 
                         (user_id, slot_id))
            result = cursor.fetchone()
            return dict(result) if result else None
        except sqlite3.Error as e:
            return None
        finally:
            if conn:
                conn.close()
    
    def get_account_count(self, user_id: int) -> int:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as count FROM accounts WHERE user_id = ? AND is_active = 1', (user_id,))
            return cursor.fetchone()['count']
        except sqlite3.Error as e:
            return 0
        finally:
            if conn:
                conn.close()
    
    def add_analytics(self, user_id: int, slot_id: str, sent_count: int, failed_count: int, 
                     skipped_count: int, total_groups: int, broadcast_type: str = 'text', 
                     duration: int = 0, error_message: str = None) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO analytics (user_id, slot_id, sent_count, failed_count, skipped_count,
                                     total_groups, broadcast_type, duration_seconds, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, slot_id, sent_count, failed_count, skipped_count, total_groups, 
                  broadcast_type, duration, error_message))
            conn.commit()
            return True
        except sqlite3.Error as e:
            return False
        finally:
            if conn:
                conn.close()
    
    def get_slot_analytics(self, user_id: int, slot_id: str) -> Dict:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    COUNT(*) as total_sends,
                    COALESCE(SUM(sent_count), 0) as total_sent,
                    COALESCE(SUM(failed_count), 0) as total_failed,
                    COALESCE(SUM(skipped_count), 0) as total_skipped,
                    COALESCE(MAX(total_groups), 0) as total_groups
                FROM analytics
                WHERE user_id = ? AND slot_id = ?
            ''', (user_id, slot_id))
            
            result = cursor.fetchone()
            return {
                'total_sends': result['total_sends'] or 0,
                'total_sent': result['total_sent'] or 0,
                'total_failed': result['total_failed'] or 0,
                'total_skipped': result['total_skipped'] or 0,
                'total_groups': result['total_groups'] or 0
            }
        except sqlite3.Error as e:
            return {'total_sends': 0, 'total_sent': 0, 'total_failed': 0, 'total_skipped': 0, 'total_groups': 0}
        finally:
            if conn:
                conn.close()
    
    def delete_account(self, slot_id: str) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE accounts SET is_active = 0 WHERE slot_id = ?', (slot_id,))
            conn.commit()
            return True
        except sqlite3.Error as e:
            return False
        finally:
            if conn:
                conn.close()
    
    def store_message(self, message_id: int, user_id: int, chat_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO user_messages (message_id, user_id, chat_id)
                VALUES (?, ?, ?)
            ''', (message_id, user_id, chat_id))
            conn.commit()
            return True
        except sqlite3.Error as e:
            return False
        finally:
            if conn:
                conn.close()
    
    def get_old_messages(self, user_id: int, hours: int = MESSAGE_RETENTION_HOURS) -> list:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cutoff = datetime.now() - timedelta(hours=hours)
            cursor.execute('''
                SELECT message_id, chat_id FROM user_messages
                WHERE user_id = ? AND timestamp < ?
            ''', (user_id, cutoff))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            return []
        finally:
            if conn:
                conn.close()
    
    def delete_message_record(self, message_id: int, user_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM user_messages WHERE message_id = ? AND user_id = ?', 
                         (message_id, user_id))
            conn.commit()
            return True
        except sqlite3.Error as e:
            return False
        finally:
            if conn:
                conn.close()
    
    def update_last_cleanup(self, user_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET last_cleanup = ? WHERE user_id = ?', 
                         (datetime.now(), user_id))
            conn.commit()
            return True
        except sqlite3.Error as e:
            return False
        finally:
            if conn:
                conn.close()
    
    def update_last_active(self, user_id: int) -> bool:
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET last_active = ? WHERE user_id = ?', 
                         (datetime.now(), user_id))
            conn.commit()
            return True
        except sqlite3.Error as e:
            return False
        finally:
            if conn:
                conn.close()


class ZiddysAdsBot:
    """Main bot class"""
    
    def __init__(self):
        self.db = DatabaseManager()
        self.anti_spam = AntiSpamManager()
        os.makedirs('sessions', exist_ok=True)
        logger.info("ZiddysAdsBot initialized")
    
    def get_session_path(self, slot_id: str) -> str:
        return os.path.join('sessions', f'session_{slot_id}.session')
    
    async def cleanup_session(self, slot_id: str):
        try:
            session_path = self.get_session_path(slot_id)
            for ext in ['', '-journal', '-wal', '-shm']:
                file_path = f"{session_path}{ext}"
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error(f"Error cleaning session: {e}")
    
    async def cleanup_old_messages(self, user_id: int, context: ContextTypes.DEFAULT_TYPE):
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
                old_messages = self.db.get_old_messages(user_id)
                
                for msg in old_messages:
                    try:
                        await context.bot.delete_message(
                            chat_id=msg['chat_id'],
                            message_id=msg['message_id']
                        )
                    except:
                        pass
                    self.db.delete_message_record(msg['message_id'], user_id)
                
                self.db.update_last_cleanup(user_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
    
    async def auto_send_messages(self, user_id: int, slot_id: str, message: str, context: ContextTypes.DEFAULT_TYPE):
        """Auto-send with anti-spam"""
        logger.info(f"Starting broadcast for {slot_id}")
        start_time = datetime.now()
        
        message = self.anti_spam.sanitize_message(message)
        
        async with await get_status_lock(user_id, slot_id):
            set_broadcast_running(user_id, slot_id, True)
        
        try:
            while is_broadcast_running(user_id, slot_id):
                try:
                    if slot_id not in user_sessions.get(user_id, {}):
                        break
                    
                    account = self.db.get_account_by_slot(user_id, slot_id)
                    if not account:
                        break
                    
                    account_created = datetime.fromisoformat(account['created_at'])
                    
                    for batch in range(BROADCAST_BATCHES):
                        if not is_broadcast_running(user_id, slot_id):
                            return
                        
                        client = user_sessions[user_id][slot_id]['client']
                        batch_start = datetime.now()
                        
                        sent_total = 0
                        failed_total = 0
                        skipped_total = 0
                        channels = []
                        
                        try:
                            can_send, remaining = self.anti_spam.can_send_today(slot_id, account_created)
                            if not can_send:
                                logger.warning(f"Daily limit reached for {slot_id}")
                                break
                            
                            if not client.is_connected():
                                await asyncio.wait_for(client.connect(), timeout=CONNECTION_TIMEOUT)
                            
                            dialogs = await asyncio.wait_for(client.get_dialogs(), timeout=OPERATION_TIMEOUT)
                            channels = [d for d in dialogs if d.is_group or d.is_channel]
                            
                            for idx, channel in enumerate(channels):
                                if not is_broadcast_running(user_id, slot_id):
                                    return
                                
                                can_send, remaining = self.anti_spam.can_send_today(slot_id, account_created)
                                if not can_send:
                                    break
                                
                                channel_id = channel.id
                                
                                if self.anti_spam.is_channel_failed(slot_id, channel_id):
                                    skipped_total += 1
                                    continue
                                
                                if not self.anti_spam.can_send_to_channel(slot_id, channel_id):
                                    skipped_total += 1
                                    continue
                                
                                if self.anti_spam.should_skip_channel():
                                    skipped_total += 1
                                    continue
                                
                                try:
                                    await asyncio.wait_for(
                                        client.send_message(channel.id, message),
                                        timeout=10
                                    )
                                    sent_total += 1
                                    self.anti_spam.record_send(slot_id, channel_id)
                                    self.anti_spam.increment_daily_count(slot_id)
                                    
                                    delay = self.anti_spam.get_natural_delay()
                                    await asyncio.sleep(delay)
                                    
                                except FloodWaitError as e:
                                    await asyncio.sleep(min(e.seconds, 300))
                                    failed_total += 1
                                except (ChannelPrivateError, ChatWriteForbiddenError):
                                    self.anti_spam.mark_failed_channel(slot_id, channel_id)
                                    failed_total += 1
                                except:
                                    failed_total += 1
                                    
                        except Exception as e:
                            logger.error(f"Broadcast error: {e}")
                        
                        batch_duration = int((datetime.now() - batch_start).total_seconds())
                        
                        if sent_total > 0 or failed_total > 0:
                            self.db.add_analytics(
                                user_id, slot_id, sent_total, failed_total, skipped_total,
                                len(channels), 'text', batch_duration, None
                            )
                        
                        account = self.db.get_account_by_slot(user_id, slot_id)
                        if not account:
                            return
                        
                        can_send, remaining = self.anti_spam.can_send_today(slot_id, account_created)
                        
                        try:
                            msg = await context.bot.send_message(
                                chat_id=user_id,
                                text=f"✔️ Batch {batch + 1}/{BROADCAST_BATCHES}\n"
                                     f"✅ Sent: {sent_total}\n"
                                     f"⏭️ Skipped: {skipped_total}\n"
                                     f"❌ Failed: {failed_total}\n"
                                     f"📊 Remaining: {remaining}"
                            )
                            self.db.store_message(msg.message_id, user_id, msg.chat_id)
                        except:
                            pass
                        
                        if batch < BROADCAST_BATCHES - 1:
                            interval = random.randint(BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX)
                            for _ in range(interval):
                                if not is_broadcast_running(user_id, slot_id):
                                    return
                                await asyncio.sleep(1)
                    
                    if is_broadcast_running(user_id, slot_id):
                        for _ in range(CYCLE_WAIT):
                            if not is_broadcast_running(user_id, slot_id):
                                return
                            await asyncio.sleep(1)
                    
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Broadcast loop error: {e}")
                    await asyncio.sleep(300)
                    
        finally:
            async with await get_status_lock(user_id, slot_id):
                set_broadcast_running(user_id, slot_id, False)
            
            if user_id in auto_send_tasks and slot_id in auto_send_tasks[user_id]:
                del auto_send_tasks[user_id][slot_id]
    
    async def _notify_user(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, message: str):
        try:
            msg = await context.bot.send_message(chat_id=user_id, text=message)
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
        except:
            pass
    
    async def update_user_profile(self, client, user_id: int):
        try:
            me = await asyncio.wait_for(client.get_me(), timeout=10)
            current_first_name = me.first_name or ""
            current_last_name = me.last_name or ""
            
            if "| Ziddys Ads" not in current_first_name:
                new_first_name = f"{current_first_name} | Ziddys Ads"[:64]
                await asyncio.wait_for(
                    client(UpdateProfileRequest(
                        first_name=new_first_name,
                        last_name=current_last_name,
                        about="Powered by @Ziddysbot"
                    )),
                    timeout=10
                )
        except Exception as e:
            logger.error(f"Error updating profile: {e}")
    
    async def check_channel_membership(self, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        try:
            member = await asyncio.wait_for(
                context.bot.get_chat_member(REQUIRED_CHANNEL, user_id),
                timeout=10
            )
            return member.status in ['member', 'administrator', 'creator']
        except:
            return False
    
    def build_dashboard(self, user_id: int, slot_id: str, account: Dict, analytics: Dict, messages: Dict):
        message_set = "✅" if messages.get(slot_id) else "❌"
        is_running = is_broadcast_running(user_id, slot_id)
        broadcast_text = "🚀 Running" if is_running else "⏸️ Stopped"
        
        total_attempts = analytics['total_sent'] + analytics['total_failed']
        success_rate = (analytics['total_sent'] / total_attempts * 100) if total_attempts > 0 else 0
        
        dashboard = f"""📊 *ACCOUNT DASHBOARD*

👤 Account: {account['display_name']}
📱 Phone: {account['phone']}
🎰 Slot: {slot_id.split('_')[-1]}

📝 Message: {message_set}
📡 Status: {broadcast_text}

📊 *Analytics:*
✅ Sent: {analytics['total_sent']}
⏭️ Skipped: {analytics['total_skipped']}
❌ Failed: {analytics['total_failed']}
👥 Groups: {analytics['total_groups']}
📈 Rate: {success_rate:.1f}%"""
        
        keyboard = [
            [InlineKeyboardButton("📝 Set Message", callback_data=f"set_msg_{slot_id}"),
             InlineKeyboardButton("🚀 Start" if not is_running else "✅ Running", 
                                 callback_data=f"start_{slot_id}" if not is_running else f"noop_{slot_id}")],
            [InlineKeyboardButton("⏸️ Stop" if is_running else "⏹️ Stopped", 
                                 callback_data=f"stop_{slot_id}" if is_running else f"noop_{slot_id}"),
             InlineKeyboardButton("📊 Analytics", callback_data=f"analytics_{slot_id}")],
            [InlineKeyboardButton("🗑️ Logout", callback_data=f"logout_{slot_id}"),
             InlineKeyboardButton("🔙 Back", callback_data="back_to_slots")]
        ]
        
        return dashboard, keyboard
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """✨ IMPROVED: More welcoming start experience"""
        try:
            user_id = update.effective_user.id
            user_name = update.effective_user.first_name or "there"
            
            self.db.create_user(user_id)
            
            # Start cleanup task
            if user_id not in message_cleanup_tasks or message_cleanup_tasks[user_id].done():
                task = asyncio.create_task(self.cleanup_old_messages(user_id, context))
                message_cleanup_tasks[user_id] = task
            
            # Check channel membership
            is_member = await self.check_channel_membership(user_id, context)
            
            if is_member:
                # ✅ User is already verified
                self.db.verify_user(user_id)
                
                if update.message:
                    # ✨ Welcoming verified user message
                    welcome_msg = await update.message.reply_text(
                        f"👋 *Welcome back, {user_name}!*\n\n"
                        f"🔄 Loading your account dashboard...",
                        parse_mode='Markdown'
                    )
                    self.db.store_message(welcome_msg.message_id, user_id, welcome_msg.chat_id)
                    await asyncio.sleep(1)
                
                return await self.show_account_slots(update, context)
            else:
                # ✅ New user needs to join channel
                keyboard = [[
                    InlineKeyboardButton("📢 Join Our Channel", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@', '')}"),
                    InlineKeyboardButton("✅ I Joined", callback_data="verify_join")
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # ✨ MUCH MORE WELCOMING MESSAGE
                welcome_text = (
                    f"🎉 *Welcome to Ziddys Ads Bot, {user_name}!*\n\n"
                    f"🚀 *Your Ultimate Telegram Broadcasting Solution*\n\n"
                    f"✨ *What You Can Do:*\n"
                    f"• 📱 Manage up to 5 Telegram accounts\n"
                    f"• 🤖 Auto-broadcast to all your groups\n"
                    f"• 📊 Real-time analytics & tracking\n"
                    f"• 🛡️ Anti-spam protection built-in\n"
                    f"• ⚡ 24/7 automated messaging\n\n"
                    f"⚠️ *Quick Setup Required:*\n"
                    f"To get started, please join our official channel:\n"
                    f"👉 {REQUIRED_CHANNEL}\n\n"
                    f"🎯 *Why Join?*\n"
                    f"• Get important updates\n"
                    f"• Learn broadcasting tips\n"
                    f"• Access premium features\n"
                    f"• Connect with other users\n\n"
                    f"👇 *Click 'Join Our Channel' below, then press 'I Joined'*"
                )
                
                if update.message:
                    msg = await update.message.reply_text(
                        welcome_text,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    self.db.store_message(msg.message_id, user_id, msg.chat_id)
                
                return VERIFY_JOIN
        except Exception as e:
            logger.error(f"Start error: {e}")
            return ConversationHandler.END
    
    async def verify_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """✨ IMPROVED: More friendly verification"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        user_name = query.from_user.first_name or "there"
        
        if await self.check_channel_membership(user_id, context):
            self.db.verify_user(user_id)
            
            # ✨ Celebration message for successful verification
            await query.edit_message_text(
                f"✅ *Verification Successful!*\n\n"
                f"🎊 Awesome, {user_name}! You're all set!\n\n"
                f"🚀 *Getting your dashboard ready...*\n\n"
                f"💡 *Quick Tip:* You can manage up to 5 accounts\n"
                f"and broadcast to unlimited groups!",
                parse_mode='Markdown'
            )
            await asyncio.sleep(2)
            return await self.show_account_slots(update, context)
        else:
            # ✨ Friendly reminder to join
            await query.answer(
                f"❌ Oops! Please join {REQUIRED_CHANNEL} first, then click 'I Joined' again 😊",
                show_alert=True
            )
            return VERIFY_JOIN
    
    async def show_account_slots(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """✨ IMPROVED: Better slot display"""
        try:
            user_id = update.effective_user.id
            accounts = self.db.get_user_accounts(user_id)
            
            account_dict = {acc['slot_id']: acc for acc in accounts}
            
            keyboard = []
            for i in range(1, MAX_ACCOUNTS + 1):
                slot_id = f"{user_id}_slot_{i}"
                
                if i % 2 == 1:
                    row = []
                
                if slot_id in account_dict:
                    acc = account_dict[slot_id]
                    row.append(InlineKeyboardButton(
                        f"✅ Slot {i}: {acc['display_name'][:10]}", 
                        callback_data=f"open_slot_{slot_id}"
                    ))
                else:
                    row.append(InlineKeyboardButton(
                        f"➕ Slot {i}: Empty", 
                        callback_data=f"add_to_slot_{slot_id}"
                    ))
                
                if i % 2 == 0 or i == MAX_ACCOUNTS:
                    keyboard.append(row)
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # ✨ More informative slot view
            active_count = len(accounts)
            message_text = (
                f"📱 *YOUR ACCOUNT SLOTS*\n\n"
                f"👤 Active: {active_count}/{MAX_ACCOUNTS} slots\n"
                f"💼 Available: {MAX_ACCOUNTS - active_count} slots\n\n"
                f"💡 *Quick Guide:*\n"
                f"• ✅ = Active account (click to manage)\n"
                f"• ➕ = Empty slot (click to add account)\n\n"
                f"👇 Select a slot to continue:"
            )
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    message_text, 
                    reply_markup=reply_markup, 
                    parse_mode='Markdown'
                )
            else:
                msg = await update.message.reply_text(
                    message_text, 
                    reply_markup=reply_markup, 
                    parse_mode='Markdown'
                )
                self.db.store_message(msg.message_id, user_id, msg.chat_id)
            
            return ACCOUNT_SLOTS
        except Exception as e:
            logger.error(f"Show slots error: {e}")
            return ACCOUNT_SLOTS
    
    # [Rest of the methods remain exactly the same as previous code...]
    # Including: add_to_slot, receive_phone, receive_otp, receive_2fa, open_slot, 
    # set_message_handler, receive_message, start_ads, stop_ads, show_analytics,
    # logout_account, confirm_logout, callback_router, cancel

    async def add_to_slot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        slot_id = query.data.replace("add_to_slot_", "")
        slot_num = slot_id.split('_')[-1]
        context.user_data['current_slot'] = slot_id
        
        await query.edit_message_text(
            f"📱 *Add Account to Slot {slot_num}*\n\n"
            f"📞 Please enter your phone number:\n"
            f"(Include country code)\n\n"
            f"Example: +1234567890\n\n"
            f"💡 Type /cancel to abort",
            parse_mode='Markdown'
        )
        return PHONE
    
    async def receive_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        phone = update.message.text.strip()

        if not phone.startswith('+'):
            msg = await update.message.reply_text("❌ Please include the country code (e.g., +1234567890)")
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            return PHONE

        context.user_data['phone'] = phone
        status_msg = await update.message.reply_text("⏳ Connecting to Telegram servers...")

        try:
            slot_id = context.user_data.get('current_slot')
            if not slot_id:
                await status_msg.delete()
                return ConversationHandler.END
            
            session_path = self.get_session_path(slot_id)
            client = TelegramClient(session_path, API_ID, API_HASH)
            await asyncio.wait_for(client.connect(), timeout=20)
            sent_code = await asyncio.wait_for(client.send_code_request(phone), timeout=15)

            if user_id not in user_sessions:
                user_sessions[user_id] = {}

            user_sessions[user_id][slot_id] = {
                'client': client, 
                'phone': phone,
                'phone_code_hash': sent_code.phone_code_hash
            }

            await status_msg.delete()
            msg = await update.message.reply_text(
                "✅ *Code Sent!*\n\n"
                "📬 Check your Telegram app for the verification code\n\n"
                "🔢 Enter the 5-digit code below:",
                parse_mode='Markdown'
            )
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            return OTP

        except Exception as e:
            await status_msg.delete()
            msg = await update.message.reply_text(f"❌ Error: {str(e)}\n\nTry /start to restart")
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            return ConversationHandler.END

    async def receive_otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        otp = update.message.text.strip()
        phone = context.user_data.get('phone')
        slot_id = context.user_data.get('current_slot')
        
        if not slot_id or slot_id not in user_sessions.get(user_id, {}):
            return ConversationHandler.END
        
        try:
            client = user_sessions[user_id][slot_id]['client']
            await asyncio.wait_for(client.sign_in(phone, otp), timeout=20)
            me = await asyncio.wait_for(client.get_me(), timeout=10)
            display_name = me.first_name or "Account"
            
            user_sessions[user_id][slot_id]['display_name'] = display_name
            await self.update_user_profile(client, user_id)
            
            self.db.add_account(user_id, phone, display_name, slot_id)
            
            msg = await update.message.reply_text(
                f"✅ *Login Successful!*\n\n"
                f"👤 Account: {display_name}\n"
                f"📱 Phone: {phone}\n\n"
                f"🎉 Your account is now ready!",
                parse_mode='Markdown'
            )
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            
            await asyncio.sleep(2)
            return await self.show_account_slots(update, context)
            
        except SessionPasswordNeededError:
            msg = await update.message.reply_text(
                "🔐 *2FA Detected*\n\n"
                "Your account has Two-Factor Authentication enabled.\n\n"
                "🔑 Please enter your 2FA password:",
                parse_mode='Markdown'
            )
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            return TWO_FA
        except:
            msg = await update.message.reply_text("❌ Invalid code. Please try again:")
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            return OTP
    
    async def receive_2fa(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        password = update.message.text.strip()
        slot_id = context.user_data.get('current_slot')
        
        try:
            await update.message.delete()
        except:
            pass
        
        if not slot_id or slot_id not in user_sessions.get(user_id, {}):
            return ConversationHandler.END
        
        try:
            client = user_sessions[user_id][slot_id]['client']
            await asyncio.wait_for(client.sign_in(password=password), timeout=20)
            me = await asyncio.wait_for(client.get_me(), timeout=10)
            display_name = me.first_name or "Account"
            
            user_sessions[user_id][slot_id]['display_name'] = display_name
            await self.update_user_profile(client, user_id)
            
            phone = context.user_data.get('phone')
            self.db.add_account(user_id, phone, display_name, slot_id)
            
            msg = await update.effective_chat.send_message(
                f"✅ *Login Successful!*\n\n"
                f"🔐 2FA verified!\n"
                f"👤 Account: {display_name}",
                parse_mode='Markdown'
            )
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            
            await asyncio.sleep(2)
            return await self.show_account_slots(update, context)
        except:
            msg = await update.effective_chat.send_message("❌ Incorrect password. Try again:")
            self.db.store_message(msg.message_id, user_id, msg.chat_id)
            return TWO_FA
    
    async def open_slot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        slot_id = query.data.replace("open_slot_", "")
        user_id = query.from_user.id
        
        context.user_data['current_slot'] = slot_id
        
        account = self.db.get_account_by_slot(user_id, slot_id)
        if not account:
            return ACCOUNT_SLOTS
        
        analytics = self.db.get_slot_analytics(user_id, slot_id)
        messages = user_data_store.get(user_id, {}).get('messages', {})
        
        dashboard, keyboard = self.build_dashboard(user_id, slot_id, account, analytics, messages)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(dashboard, reply_markup=reply_markup, parse_mode='Markdown')
        return ACCOUNT_DASHBOARD
    
    async def set_message_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        slot_id = query.data.replace("set_msg_", "")
        context.user_data['current_slot'] = slot_id
        
        await query.delete_message()
        msg = await query.message.reply_text(
            "✍️ *Set Your Broadcast Message*\n\n"
            "Type the message you want to send to all your groups:\n\n"
            "💡 Tips:\n"
            "• Keep it clear and engaging\n"
            "• Add emojis for better engagement\n"
            "• Avoid spam words\n\n"
            "📝 Enter your message below:",
            parse_mode='Markdown'
        )
        self.db.store_message(msg.message_id, query.from_user.id, msg.chat_id)
        return MESSAGE
    
    async def receive_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        message = update.message.text.strip()
        slot_id = context.user_data.get('current_slot')
        
        if not slot_id:
            return await self.show_account_slots(update, context)
        
        if user_id not in user_data_store:
            user_data_store[user_id] = {'messages': {}}
        
        if 'messages' not in user_data_store[user_id]:
            user_data_store[user_id]['messages'] = {}
        
        user_data_store[user_id]['messages'][slot_id] = message
        
        account = self.db.get_account_by_slot(user_id, slot_id)
        
        msg = await update.message.reply_text(
            f"✅ *Message Saved Successfully!*\n\n"
            f"Your broadcast message is ready to go!",
            parse_mode='Markdown'
        )
        self.db.store_message(msg.message_id, user_id, msg.chat_id)
        
        await asyncio.sleep(1)
        
        analytics = self.db.get_slot_analytics(user_id, slot_id)
        messages = user_data_store.get(user_id, {}).get('messages', {})
        dashboard, keyboard = self.build_dashboard(user_id, slot_id, account, analytics, messages)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg2 = await update.message.reply_text(dashboard, reply_markup=reply_markup, parse_mode='Markdown')
        self.db.store_message(msg2.message_id, user_id, msg2.chat_id)
        
        return ACCOUNT_DASHBOARD
    
    async def start_ads(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        
        slot_id = query.data.replace("start_", "")
        user_id = query.from_user.id
        
        if is_broadcast_running(user_id, slot_id):
            await query.answer("Already running!", show_alert=True)
            return ACCOUNT_DASHBOARD
        
        messages = user_data_store.get(user_id, {}).get('messages', {})
        if not messages.get(slot_id):
            await query.answer("Please set a message first!", show_alert=True)
            return ACCOUNT_DASHBOARD
        
        account = self.db.get_account_by_slot(user_id, slot_id)
        if not account or slot_id not in user_sessions.get(user_id, {}):
            await query.answer("Session expired. Please re-login!", show_alert=True)
            return ACCOUNT_DASHBOARD
        
        message = messages[slot_id]
        
        set_broadcast_running(user_id, slot_id, True)
        
        if user_id not in auto_send_tasks:
            auto_send_tasks[user_id] = {}
        
        task = asyncio.create_task(self.auto_send_messages(user_id, slot_id, message, context))
        auto_send_tasks[user_id][slot_id] = task
        
        await query.answer("🚀 Broadcast started!", show_alert=False)
        
        analytics = self.db.get_slot_analytics(user_id, slot_id)
        messages_dict = user_data_store.get(user_id, {}).get('messages', {})
        dashboard, keyboard = self.build_dashboard(user_id, slot_id, account, analytics, messages_dict)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(dashboard, reply_markup=reply_markup, parse_mode='Markdown')
        
        return ACCOUNT_DASHBOARD
    
    async def stop_ads(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        
        slot_id = query.data.replace("stop_", "")
        user_id = query.from_user.id
        
        if not is_broadcast_running(user_id, slot_id):
            await query.answer("Not running!", show_alert=True)
            return ACCOUNT_DASHBOARD
        
        set_broadcast_running(user_id, slot_id, False)
        
        if user_id in auto_send_tasks and slot_id in auto_send_tasks[user_id]:
            task = auto_send_tasks[user_id][slot_id]
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except:
                pass
            
            if slot_id in auto_send_tasks[user_id]:
                del auto_send_tasks[user_id][slot_id]
        
        account = self.db.get_account_by_slot(user_id, slot_id)
        
        await query.answer("⏸️ Broadcast stopped!", show_alert=False)
        
        analytics = self.db.get_slot_analytics(user_id, slot_id)
        messages = user_data_store.get(user_id, {}).get('messages', {})
        dashboard, keyboard = self.build_dashboard(user_id, slot_id, account, analytics, messages)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(dashboard, reply_markup=reply_markup, parse_mode='Markdown')
        
        return ACCOUNT_DASHBOARD
    
    async def show_analytics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        slot_id = query.data.replace("analytics_", "")
        user_id = query.from_user.id
        
        account = self.db.get_account_by_slot(user_id, slot_id)
        analytics = self.db.get_slot_analytics(user_id, slot_id)
        
        total = analytics['total_sent'] + analytics['total_failed']
        rate = (analytics['total_sent'] / total * 100) if total > 0 else 0
        
        text = f"""📊 *DETAILED ANALYTICS*

👤 Account: {account['display_name']}
🎰 Slot: {slot_id.split('_')[-1]}

📈 *Performance:*
✅ Sent: {analytics['total_sent']}
⏭️ Skipped: {analytics['total_skipped']}
❌ Failed: {analytics['total_failed']}
👥 Groups: {analytics['total_groups']}
📊 Success Rate: {rate:.1f}%

💡 Keep broadcasting to improve your stats!"""
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data=f"open_slot_{slot_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        return ACCOUNT_DASHBOARD
    
    async def logout_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        slot_id = query.data.replace("logout_", "")
        user_id = query.from_user.id
        
        account = self.db.get_account_by_slot(user_id, slot_id)
        
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Logout", callback_data=f"confirm_logout_{slot_id}"),
             InlineKeyboardButton("❌ Cancel", callback_data=f"open_slot_{slot_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🗑️ *Confirm Logout*\n\n"
            f"Are you sure you want to remove:\n"
            f"👤 {account['display_name']}\n\n"
            f"⚠️ This will:\n"
            f"• Stop any running broadcasts\n"
            f"• Remove account from this slot\n"
            f"• Delete all session data",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return ACCOUNT_DASHBOARD
    
    async def confirm_logout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        slot_id = query.data.replace("confirm_logout_", "")
        user_id = query.from_user.id
        
        set_broadcast_running(user_id, slot_id, False)
        
        if user_id in auto_send_tasks and slot_id in auto_send_tasks[user_id]:
            task = auto_send_tasks[user_id][slot_id]
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5)
            except:
                pass
            del auto_send_tasks[user_id][slot_id]
        
        if user_id in user_sessions and slot_id in user_sessions[user_id]:
            try:
                client = user_sessions[user_id][slot_id]['client']
                if client.is_connected():
                    await client.disconnect()
            except:
                pass
            del user_sessions[user_id][slot_id]
        
        self.db.delete_account(slot_id)
        await self.cleanup_session(slot_id)
        
        if user_id in user_data_store and 'messages' in user_data_store[user_id]:
            if slot_id in user_data_store[user_id]['messages']:
                del user_data_store[user_id]['messages'][slot_id]
        
        await query.edit_message_text(
            "✅ *Logout Successful!*\n\n"
            "The slot is now available for a new account.",
            parse_mode='Markdown'
        )
        await asyncio.sleep(2)
        return await self.show_account_slots(update, context)
    
    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data
        
        try:
            await query.answer()
            
            if data.startswith("noop_"):
                return ACCOUNT_DASHBOARD
            
            router_map = {
                "verify_join": self.verify_join,
                "back_to_slots": self.show_account_slots,
                "add_to_slot_": self.add_to_slot,
                "open_slot_": self.open_slot,
                "set_msg_": self.set_message_handler,
                "start_": self.start_ads,
                "stop_": self.stop_ads,
                "analytics_": self.show_analytics,
                "logout_": self.logout_account,
                "confirm_logout_": self.confirm_logout,
            }
            
            for prefix, handler in router_map.items():
                if data == prefix or data.startswith(prefix):
                    return await handler(update, context)
            
            return ACCOUNT_SLOTS
            
        except Exception as e:
            logger.error(f"Callback error: {e}")
            try:
                await query.answer("Error occurred", show_alert=True)
            except:
                pass
            return ACCOUNT_SLOTS
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        await update.message.reply_text(
            "❌ *Operation Cancelled*\n\n"
            "Use /start to begin again.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END


def main():
    """Run bot"""
    try:
        bot = ZiddysAdsBot()
        app = Application.builder().token(BOT_TOKEN).build()
        
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', bot.start)],
            states={
                VERIFY_JOIN: [
                    CallbackQueryHandler(bot.callback_router),
                    CommandHandler('start', bot.start)
                ],
                PHONE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bot.receive_phone),
                    CommandHandler('start', bot.start)
                ],
                OTP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bot.receive_otp),
                    CommandHandler('start', bot.start)
                ],
                TWO_FA: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bot.receive_2fa),
                    CommandHandler('start', bot.start)
                ],
                MESSAGE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, bot.receive_message),
                    CommandHandler('start', bot.start)
                ],
                ACCOUNT_SLOTS: [
                    CallbackQueryHandler(bot.callback_router),
                    CommandHandler('start', bot.start)
                ],
                ACCOUNT_DASHBOARD: [
                    CallbackQueryHandler(bot.callback_router),
                    CommandHandler('start', bot.start)
                ],
            },
            fallbacks=[
                CommandHandler('cancel', bot.cancel),
                CommandHandler('start', bot.start)
            ],
            per_message=False,
            allow_reentry=True
        )
        
        app.add_handler(conv_handler)
        
        print("="*70)
        print("🤖 Ziddys Ads Bot - PRODUCTION READY")
        print("="*70)
        print(f"📢 Channel: {REQUIRED_CHANNEL}")
        print(f"👤 Owner: {OWNER_USERNAME}")
        print("\n✅ ALL FEATURES ENABLED:")
        print("  • ✨ Welcoming user experience")
        print("  • 📱 5 Account Slots")
        print("  • 🚀 Auto Broadcasting")
        print("  • 🛡️ Anti-Spam Protection")
        print("  • 📊 Real-time Analytics")
        print("  • ⚡ 24/7 Automated Messaging")
        print("="*70)
        
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        logger.critical(traceback.format_exc())
        raise


if __name__ == '__main__':
    main()
