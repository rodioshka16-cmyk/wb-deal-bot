#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WB Deal Hunter Bot — версия для Railway.app
Использует переменные окружения вместо хардкода
"""

import os
import requests
import sqlite3
import time
import logging
from datetime import datetime
from typing import List, Dict
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ===== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ =====
BOT_TOKEN = os.environ.get('BOT_TOKEN')
MIN_DISCOUNT = int(os.environ.get('MIN_DISCOUNT', '50'))
MAX_PRICE = int(os.environ.get('MAX_PRICE', '5000'))
MIN_RATING = float(os.environ.get('MIN_RATING', '4.0'))
MIN_FEEDBACKS = int(os.environ.get('MIN_FEEDBACKS', '10'))
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', '600'))  # 10 минут

SEARCH_QUERIES = [
    "платье", "кроссовки", "наушники", "куртка",
    "сумка", "часы", "рюкзак", "джинсы",
    "свитер", "ботинки", "футболка", "очки",
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Origin': 'https://www.wildberries.ru',
    'Referer': 'https://www.wildberries.ru/',
}

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ===== БАЗА ДАННЫХ =====
class DealDatabase:
    def __init__(self, db_path="data/wb_bot_deals.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self._init_tables()
    
    def _init_tables(self):
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS deals (
                nm_id INTEGER PRIMARY KEY,
                name TEXT,
                brand TEXT,
                price INTEGER,
                old_price INTEGER,
                discount INTEGER,
                rating REAL,
                feedbacks INTEGER,
                url TEXT,
                found_at TEXT
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                subscribed INTEGER DEFAULT 1
            )
        ''')
        self.conn.commit()
    
    def is_seen(self, nm_id: int) -> bool:
        cur = self.conn.execute('SELECT 1 FROM deals WHERE nm_id = ?', (nm_id,))
        return cur.fetchone() is not None
    
    def save_deal(self, deal: dict):
        self.conn.execute('''
            INSERT OR IGNORE INTO deals 
            (nm_id, name, brand, price, old_price, discount, rating, feedbacks, url, found_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            deal['nm_id'], deal['name'], deal['brand'],
            deal['price'], deal['old_price'], deal['discount'],
            deal['rating'], deal['feedbacks'], deal['url'],
            datetime.now().isoformat()
        ))
        self.conn.commit()
    
    def add_user(self, chat_id: int):
        self.conn.execute(
            'INSERT OR IGNORE INTO users (chat_id, subscribed) VALUES (?, 1)',
            (chat_id,)
        )
        self.conn.commit()
    
    def get_subscribed_users(self) -> List[int]:
        cur = self.conn.execute('SELECT chat_id FROM users WHERE subscribed = 1')
        return [row[0] for row in cur.fetchall()]
    
    def get_recent_deals(self, limit=10):
        cur = self.conn.execute('''
            SELECT * FROM deals 
            ORDER BY found_at DESC 
            LIMIT ?
        ''', (limit,))
        return cur.fetchall()
    
    def close(self):
        self.conn.close()


# ===== ПАРСЕР WB =====
class WBParser:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
    
    def _get_image_url(self, nm_id: int) -> str:
        vol = nm_id // 100000
        part = nm_id // 1000
        hosts = [
            (143, "basket-01"), (287, "basket-02"), (431, "basket-03"),
            (719, "basket-04"), (1007, "basket-05"), (1061, "basket-06"),
            (1115, "basket-07"), (1169, "basket-08"), (1313, "basket-09"),
            (1601, "basket-10"), (1655, "basket-11"), (1919, "basket-12")
        ]
        host = "basket-13"
        for limit, h in hosts:
            if vol <= limit:
                host = h
                break
        return f"https://{host}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/big/1.webp"
    
    def search_by_query(self, query: str) -> list:
        url = "https://search.wb.ru/exactmatch/ru/common/v7/search"
        params = {
            'appType': 1, 'curr': 'rub', 'dest': -1257786,
            'query': query, 'resultset': 'catalog', 'sort': 'discount',
            'spp': 30, 'page': 1, 'limit': 100,
        }
        
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                if resp.status_code == 429:
                    time.sleep((attempt + 1) * 10)
                    continue
                resp.raise_for_status()
                return resp.json().get('data', {}).get('products', [])
            except Exception as e:
                logger.error(f"Ошибка поиска '{query}': {e}")
                if attempt < 2:
                    time.sleep(5)
        return []
    
    def search_hot_deals(self) -> List[Dict]:
        all_deals = []
        seen_ids = set()
        
        for query in SEARCH_QUERIES:
            logger.info(f"🔍 Ищу: {query}...")
            products = self.search_by_query(query)
            
            for p in products:
                nm_id = p.get('id', 0)
                if nm_id in seen_ids:
                    continue
                seen_ids.add(nm_id)
                
                sale_price = p.get('salePriceU', 0) / 100
                basic_price = p.get('basicPriceU', 0) / 100
                
                if basic_price <= 0 or sale_price <= 0:
                    continue
                
                discount = p.get('sale', 0)
                rating = p.get('rating', 0)
                feedbacks = p.get('feedbacks', 0)
                
                if (discount >= MIN_DISCOUNT and sale_price <= MAX_PRICE and 
                    rating >= MIN_RATING and feedbacks >= MIN_FEEDBACKS):
                    
                    all_deals.append({
                        'nm_id': nm_id,
                        'name': p.get('name', ''),
                        'brand': p.get('brand', ''),
                        'price': int(sale_price),
                        'old_price': int(basic_price),
                        'discount': discount,
                        'rating': rating,
                        'feedbacks': feedbacks,
                        'url': f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
                        'image_url': self._get_image_url(nm_id),
                    })
            
            time.sleep(2)
        
        all_deals.sort(key=lambda x: x['discount'], reverse=True)
        return all_deals


# ===== TELEGRAM БОТ =====
class DealBot:
    def __init__(self):
        self.db = DealDatabase()
        self.parser = WBParser()
        self.app = None
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat_id = update.effective_chat.id
        self.db.add_user(chat_id)
        
        await update.message.reply_text(
            f"🔥 *Привет, {user.first_name}!*\n\n"
            f"Я буду присылать тебе горящие скидки с WB!\n\n"
            f"Настройки:\n"
            f"• Скидка от {MIN_DISCOUNT}%\n"
            f"• Цена до {MAX_PRICE} ₽\n"
            f"• Проверка каждые {CHECK_INTERVAL // 60} мин\n\n"
            f"*Команды:*\n"
            f"/deals — последние находки\n"
            f"/status — статус\n"
            f"/stop — отписаться",
            parse_mode='Markdown'
        )
    
    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("👋 Уведомления остановлены. /start — возобновить.")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"✅ Бот активен\n"
            f"⏱ Интервал: {CHECK_INTERVAL // 60} мин\n"
            f"💰 Скидка от: {MIN_DISCOUNT}%\n"
            f"💸 Цена до: {MAX_PRICE} ₽",
            parse_mode='Markdown'
        )
    
    async def deals_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = self.db.get_recent_deals(10)
        
        if not rows:
            await update.message.reply_text("😕 Пока нет находок. Подождите!")
            return
        
        text = "🔥 *Последние находки:*\n\n"
        for i, row in enumerate(rows, 1):
            nm_id, name, brand, price, old_price, discount, rating, feedbacks, url, found_at = row
            text += f"{i}. *{brand}* — {name[:40]}...\n"
            text += f"   💰 {price} ₽ (−{discount}%)\n"
            text += f"   [Открыть]({url})\n\n"
        
        await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)
    
    async def send_deal(self, chat_id: int, deal: Dict):
        savings = deal['old_price'] - deal['price']
        text = (
            f"🔥 *ГОРЯЩАЯ СКИДКА!*\n\n"
            f"*{deal['brand']}* — {deal['name']}\n\n"
            f"💰 *{deal['price']} ₽* (было {deal['old_price']} ₽)\n"
            f"📉 Скидка: *{deal['discount']}%*\n"
            f"💵 Экономия: {savings} ₽\n\n"
            f"⭐ {deal['rating']} ({deal['feedbacks']} отзывов)"
        )
        
        keyboard = [[InlineKeyboardButton("🛒 Купить на WB", url=deal['url'])]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await self.app.bot.send_photo(
                chat_id=chat_id,
                photo=deal['image_url'],
                caption=text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    async def background_checker(self, context: ContextTypes.DEFAULT_TYPE):
        logger.info("🔍 Проверка скидок...")
        
        try:
            deals = self.parser.search_hot_deals()
            new_deals = [d for d in deals if not self.db.is_seen(d['nm_id'])]
            
            if new_deals:
                logger.info(f"✅ Найдено {len(new_deals)} новых предложений")
                
                for deal in new_deals:
                    self.db.save_deal(deal)
                
                users = self.db.get_subscribed_users()
                for deal in new_deals[:5]:
                    for chat_id in users:
                        await self.send_deal(chat_id, deal)
                        await asyncio.sleep(1)
            else:
                logger.info("😴 Новых предложений нет")
        
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    
    def run(self):
        if not BOT_TOKEN:
            logger.error("❌ BOT_TOKEN не установлен! Добавьте в переменные окружения.")
            return
        
        logger.info("✅ Бот запущен на Railway!")
        
        self.app = Application.builder().token(BOT_TOKEN).build()
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("stop", self.stop_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("deals", self.deals_command))
        
        job_queue = self.app.job_queue
        job_queue.run_repeating(self.background_checker, interval=CHECK_INTERVAL, first=10)
        
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    bot = DealBot()
    bot.run()
