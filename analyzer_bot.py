import logging
import requests
import sqlite3
import time
import os
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# --- الإعدادات (نفس السابقة) ---
# --- الإعدادات (تقرأ من بيئة PM2) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BSCSCAN_API_KEY = os.getenv("BSCSCAN_API_KEY")
ARKHAM_API_KEY = os.getenv("ARKHAM_API_KEY")

# 2. عناوين API
DEXSCREENER_API_BASE = "https://api.dexscreener.com/latest/dex"
BSCSCAN_API_BASE = "https://api.bscscan.com/api"
ARKHAM_API_BASE = "https://api.arkham.com/v1"

# 3. معايير التحليل
EARLY_BUYER_TIMEFRAME_MINUTES = 15
MIN_PNL_USD_TO_NOTIFY = 100000  # 100k$ P&L

# 4. إعدادات أخرى
DB_NAME = 'arkham_hunter.db' # نفس ملف الـ DB السابق (سيستخدم كذاكرة)

# إعداد Logging (سيعرض في الطرفية الآن)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# إعداد قاعدة البيانات (للتخزين المؤقت لـ Arkham)
conn = sqlite3.connect(DB_NAME, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    arkham_label TEXT,
    arkham_pnl_usd REAL DEFAULT 0,
    arkham_is_smart BOOLEAN DEFAULT 0,
    last_updated TIMESTAMP
)
''')
conn.commit()

# --- تعريف حالات المحادثة ---
# سنستخدم هذا لإدارة "ماذا ينتظر البوت من المستخدم"
STATE_START, STATE_AWAITING_CONTRACT = range(2)

# --- الدوال المساعدة (طلبات API) ---
def make_api_request(url, headers=None, retries=3):
    """طلب API (متزامن)"""
    for attempt in range(retries):
        try:
            time.sleep(0.5) # لتجنب ضغط الـ API
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning(f"محاولة {attempt+1} فشلت: {e}")
            time.sleep(1)
    return None

def get_token_creation_time(token_address):
    """جلب وقت إنشاء العملة من DexScreener"""
    url = f"{DEXSCREENER_API_BASE}/tokens/{token_address}"
    data = make_api_request(url)
    
    if not data or 'pairs' not in data or not data['pairs']:
        logger.warning(f"لم يتم العثور على Pair للعملة {token_address} في DexScreener")
        return None
    
    # نفترض أن أول pair هو الأقدم (الأهم)
    try:
        # pairCreatedAt يأتي كـ timestamp بالمللي ثانية
        created_at_ms = data['pairs'][0]['pairCreatedAt']
        created_at = datetime.fromtimestamp(created_at_ms / 1000)
        return created_at
    except Exception as e:
        logger.error(f"خطأ في استخراج وقت إنشاء {token_address}: {e}")
        return None

def get_early_buyers(token_address, created_at):
    """الحصول على المشترين الأوائل عبر BSCScan API"""
    early_time = created_at + datetime.timedelta(minutes=EARLY_BUYER_TIMEFRAME_MINUTES)
    end_timestamp = int(early_time.timestamp())
    
    url = (f"{BSCSCAN_API_BASE}?module=account&action=tokentx"
           f"&contractaddress={token_address}"
           f"&page=1&offset=100&sort=asc"
           f"&apikey={BSCSCAN_API_KEY}")
    
    data = make_api_request(url)
    if not data or data['status'] != '1':
        logger.error(f"فشل جلب txns لـ {token_address}")
        return set()
    
    early_buyers = set()
    for tx in data['result']:
        try:
            tx_timestamp = int(tx['timeStamp'])
            if tx_timestamp <= end_timestamp and float(tx['value']) > 0:
                if len(tx['to']) == 42 and tx['to'].lower() != token_address.lower() and not tx['to'].lower().startswith("0x0000"):
                    early_buyers.add(tx['to'].lower())
        except Exception as e:
            logger.warning(f"خطأ في معالجة tx: {e}")
            
    logger.info(f"تم العثور على {len(early_buyers)} early buyer للـ token {token_address[:8]}")
    return early_buyers

def get_arkham_intelligence(address):
    """جلب "الذكاء" حول المحفظة من Arkham (مع ذاكرة مؤقتة)"""
    
    # 1. تحقق من الذاكرة (DB) أولاً
    cursor.execute("SELECT arkham_label, arkham_pnl_usd, arkham_is_smart FROM wallets WHERE address = ?", (address,))
    cached = cursor.fetchone()
    if cached:
        logger.info(f"Arkham data for {address[:8]} [FROM CACHE]")
        return {
            'label': cached[0],
            'pnl': cached[1],
            'is_smart': bool(cached[2])
        }
        
    # 2. إذا لم يكن في الذاكرة، اطلبه من API
    logger.info(f"Arkham data for {address[:8]} [FROM API]")
    headers = {'API-Key': ARKHAM_API_KEY}
    results = {'pnl': 0.0, 'label': None, 'is_smart': False}

    # P&L
    pnl_url = f"{ARKHAM_API_BASE}/address/{address}/pnl?chain=bsc"
    pnl_data = make_api_request(pnl_url, headers=headers)
    if pnl_data and 'bsc' in pnl_data and 'totalPnlUsd' in pnl_data['bsc']:
        results['pnl'] = float(pnl_data['bsc']['totalPnlUsd'])

    # Labels
    entities_url = f"{ARKHAM_API_BASE}/address/{address}/entities"
    entities_data = make_api_request(entities_url, headers=headers)
    if entities_data and 'entities' in entities_data and entities_data['entities']:
        first_entity = entities_data['entities'][0]
        if 'arkhamLabel' in first_entity and 'name' in first_entity['arkhamLabel']:
            label_name = first_entity['arkhamLabel']['name']
            results['label'] = label_name
            if "smart money" in label_name.lower():
                results['is_smart'] = True
    
    # 3. احفظ النتيجة في الذاكرة (DB) للمرة القادمة
    cursor.execute("""
        INSERT OR REPLACE INTO wallets (address, arkham_label, arkham_pnl_usd, arkham_is_smart, last_updated)
        VALUES (?, ?, ?, ?, ?)
    """, (address, results['label'], results['pnl'], results['is_smart'], datetime.now()))
    conn.commit()

    return results

# --- دوال البوت التفاعلية ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إرسال رسالة ترحيب وعرض الأزرار الرئيسية"""
    keyboard = [["🔍 تحليل عقد جديد"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "أهلاً بك في بوت المحلل الاستخباراتي.\n\n"
        "اضغط 'تحليل عقد جديد' ثم أرسل لي عقد العملة (BSC) لأقوم بتحليل المشترين الأوائل.",
        reply_markup=reply_markup,
    )
    return STATE_START

async def ask_for_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يطلب من المستخدم إرسال العقد"""
    await update.message.reply_text(
        "حسناً، الرجاء إرسال عنوان عقد العملة (BSC) الآن...",
        reply_markup=ReplyKeyboardRemove(), # إخفاء الأزرار مؤقتاً
    )
    return STATE_AWAITING_CONTRACT

async def analyze_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """الدالة الرئيسية: تستلم العقد وتبدأ التحليل"""
    token_address = update.message.text.strip().lower()
    
    # تحقق بسيط من أن العنوان صالح
    if not (len(token_address) == 42 and token_address.startswith("0x")):
        await update.message.reply_text("❌ عنوان عقد غير صالح. الرجاء إرسال عنوان BSC صحيح.")
        return STATE_AWAITING_CONTRACT # اطلب منه مرة أخرى

    await update.message.reply_text("⏳ تم استلام العقد. جاري التحليل، قد يستغرق هذا دقيقة...")

    try:
        # --- خطوة 1: جلب وقت الإنشاء ---
        created_at = get_token_creation_time(token_address)
        if not created_at:
            await update.message.reply_text(f"❌ فشل العثور على العملة {token_address[:8]}... في DexScreener. تأكد أنه عقد عملة وليس Pair.")
            return await start(update, context) # العودة للبداية

        # --- خطوة 2: جلب المشترين الأوائل ---
        early_buyers = get_early_buyers(token_address, created_at)
        if not early_buyers:
            await update.message.reply_text(f"✅ تم تحليل {token_address[:8]}... \nلم يتم العثور على مشترين أوائل في أول {EARLY_BUYER_TIMEFRAME_MINUTES} دقيقة.")
            return await start(update, context) # العودة للبداية

        # --- خطوة 3: تحليل المشترين بـ Arkham ---
        smart_wallets_found = []
        await update.message.reply_text(f"⏳ تم العثور على {len(early_buyers)} مشتري مبكر. جاري فحصهم بـ Arkham...")

        for buyer in early_buyers:
            intel = get_arkham_intelligence(buyer)
            
            # فلترة النتائج المهمة فقط
            if intel['is_smart'] or intel['pnl'] >= MIN_PNL_USD_TO_NOTIFY or intel['label']:
                smart_wallets_found.append({
                    'address': buyer,
                    'label': intel['label'],
                    'pnl': intel['pnl'],
                    'is_smart': intel['is_smart']
                })

        # --- خطوة 4: إرسال التقرير النهائي ---
        if not smart_wallets_found:
            await update.message.reply_text(f"✅ تحليل كامل لـ {token_address[:8]}... \n\nتم فحص {len(early_buyers)} مشتري مبكر، ولم يتم العثور على محافظ 'Smart Money' معروفة بينهم.")
        else:
            report = f"🎯 **تقرير استخباراتي لـ {token_address[:8]}...** 🎯\n\nتم العثور على {len(smart_wallets_found)} محفظة مميزة من أصل {len(early_buyers)} مشتري مبكر:\n\n"
            report += "--------------------\n"
            
            smart_wallets_found.sort(key=lambda x: x['pnl'], reverse=True) # ترتيب حسب الربح
            
            for wallet in smart_wallets_found:
                reason = ""
                if wallet['is_smart']: reason = "🧠 Smart Money"
                elif wallet['pnl'] >= MIN_PNL_USD_TO_NOTIFY: reason = f"💰 High PNL (${wallet['pnl']:,.0f})"
                elif wallet['label']: reason = f"🏷️ Labeled ({wallet['label']})"

                report += (
                    f"🔗 [Wallet (BscScan)](https://bscscan.com/address/{wallet['address']})\n"
                    f"`{wallet['address']}`\n"
                    f"📈 السبب: {reason}\n"
                    "--------------------\n"
                )
            
            report += "\nيمكنك الآن نسخ هذه العناوين وإضافتها لبوت القنص الخاص بك."
            await update.message.reply_text(report, parse_mode='Markdown', disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"حدث خطأ فادح أثناء التحليل: {e}", exc_info=True)
        await update.message.reply_text(f"❌ حدث خطأ أثناء التحليل. الرجاء مراجعة اللوج.")

    # العودة إلى القائمة الرئيسية
    return await start(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """إلغاء الأمر والعودة للبداية"""
    await update.message.reply_text(
        "تم الإلغاء. العودة للقائمة الرئيسية.",
        reply_markup=ReplyKeyboardMarkup([["🔍 تحليل عقد جديد"]], resize_keyboard=True),
    )
    return STATE_START

def main() -> None:
    """الدالة الرئيسية لتشغيل البوت"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # إعداد نظام المحادثة لإدارة الحالات
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STATE_START: [
                MessageHandler(filters.Regex("^🔍 تحليل عقد جديد$"), ask_for_contract)
            ],
            STATE_AWAITING_CONTRACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_contract)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )

    application.add_handler(conv_handler)
    
    logger.info("--- 🚀 بوت المحلل الاستخباراتي بدأ التشغيل 🚀 ---")
    logger.info("--- أرسل /start للبوت لبدء الواجهة ---")
    
    # بدء تشغيل البوت (سيظل يعمل 24/7)
    application.run_polling()

if __name__ == "__main__":
    main()
