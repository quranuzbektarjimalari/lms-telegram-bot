import os
import json
import nest_asyncio
import asyncio
import time
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pytz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Encryption
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
import mysql.connector

# ========== Configuration & safety checks ==========
# Ensure required environment variables exist (fail fast)
required_env = ["BOT_TOKEN", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "FERNET_KEY"]
missing = [e for e in required_env if not os.getenv(e)]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "314980609"))  # default provided; override via env if needed

nest_asyncio.apply()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== Globals ==========
GLOBAL_EXECUTOR = ThreadPoolExecutor(max_workers=30)

TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("✅ Vazifalarni tekshirish"), KeyboardButton("🔐 Tizimdan chiqish")]],
    resize_keyboard=True,
)

# HTTP session (with retries)
GLOBAL_SESSION = requests.Session()
adapter = HTTPAdapter(max_retries=Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
))
GLOBAL_SESSION.mount("http://", adapter)
GLOBAL_SESSION.mount("https://", adapter)

# In-memory session state
user_data = {}

# ------------------ Encryption helpers ------------------

def ensure_key():
    key = os.getenv("FERNET_KEY")
    if not key:
        raise ValueError("FERNET_KEY environment variable topilmadi!")
    return Fernet(key.encode())

def get_db():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
        port=int(os.getenv("MYSQL_PORT", 3306)),
        autocommit=False
    )


# Create credentials table if not exists (run once at startup)
def ensure_tables():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS credentials (
        chat_id BIGINT PRIMARY KEY,
        login TEXT NOT NULL,
        password TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    );
    """)
    db.commit()
    cursor.close()
    db.close()


# Return decrypted credentials for single chat_id (or None)
def get_credentials_for(chat_id):
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT login, password FROM credentials WHERE chat_id = %s", (chat_id,))
    row = cursor.fetchone()
    cursor.close()
    db.close()

    if not row:
        return None

    f = ensure_key()
    try:
        login = f.decrypt(row["login"].encode()).decode()
        password = f.decrypt(row["password"].encode()).decode()
    except Exception as e:
        logger.exception("Failed to decrypt credentials for %s: %s", chat_id, e)
        return None

    return {"login": login, "password": password}


# Save (insert/update) encrypted credentials
def set_credentials_for(chat_id, login, password):
    db = get_db()
    cursor = db.cursor()
    f = ensure_key()
    enc_login = f.encrypt(login.encode()).decode()
    enc_password = f.encrypt(password.encode()).decode()

    cursor.execute(
        """
        INSERT INTO credentials (chat_id, login, password)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE
            login = VALUES(login),
            password = VALUES(password),
            updated_at = CURRENT_TIMESTAMP
        """,
        (chat_id, enc_login, enc_password)
    )
    db.commit()
    cursor.close()
    db.close()
    return True


# Delete credentials
def delete_credentials_for(chat_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM credentials WHERE chat_id = %s", (chat_id,))
    db.commit()
    cursor.close()
    db.close()
    return True


# Return dict of all chat_ids -> True (used by broadcast)
def load_all_credentials():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT chat_id FROM credentials")
    rows = cursor.fetchall()
    cursor.close()
    db.close()
    return {str(r["chat_id"]): True for r in rows} if rows else {}

# === 1. LMS tizimiga kirish (brauzerdek ishlaydigan sessiya) ===
def login_to_lms(username, password):
    """Sinxron: LMS ga session bilan kiradi va (session, fullname, error) qaytaradi."""
    try:
        session = requests.Session()
        login_url = "https://lms.iiau.uz/auth/login"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "uz-UZ,uz;q=0.9,en;q=0.8,ru;q=0.7",
            "Connection": "keep-alive",
            "Referer": login_url,
            "Upgrade-Insecure-Requests": "1",
        }

        resp = session.get(login_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None, None, "❌ LMS sahifasiga ulanib bo‘lmadi."

        soup = BeautifulSoup(resp.text, "html.parser")
        token_tag = soup.find("input", {"name": "_token"})
        token = token_tag["value"] if token_tag else ""

        payload = {"_token": token, "login": username, "password": password, "g-recaptcha-response": ""}
        login_resp = session.post(login_url, data=payload, headers=headers, timeout=10)

        if "logout" in login_resp.text or "Chiqish" in login_resp.text:
            fullname = "Noma’lum foydalanuvchi"
            try:
                dashboard = session.get("https://lms.iiau.uz/dashboard", headers=headers, timeout=10)
                prof_soup = BeautifulSoup(dashboard.text, "html.parser")
                span_tag = prof_soup.select_one("button#dropLogin span")
                if span_tag and span_tag.get_text(strip=True):
                    fullname = span_tag.get_text(strip=True)
            except Exception:
                pass
            return session, fullname, None
        else:
            return None, None, "❌ Login yoki parol noto‘g‘ri bo‘lishi mumkin."
    except Exception as e:
        return None, None, f"❌ LMS ga ulanishda xato: {e}"

# === 📘 Fanlar ro‘yxati (id → nom) ===
SUBJECT_LINKS = {
    "826-27-uz": "Kalom ilmi tarixi va nazariyasi II",
    "827-27-uz": "Islom manbashunosligi",
    "828-27-uz": "Moturidiya ta’limotiga oid manbalar",
    "829-27-uz": "Tasavvuf II",
    "830-27-uz": "Islom falsafasi",
    "831-27-uz": "Arab tilining nazariy grammatikasi",
    "832-27-uz": "Mantiq ilmi asoslari"
}

def extract_subject_fast(soup):
    """
    Sahifadagi fan nomini aniqlash: 'Orqaga' tugmasidagi link orqali
    """
    try:
        # Orqaga tugmasini qidiramiz, faqat text bo'yicha
        back_link = None
        for a in soup.find_all("a", href=True):
            if "Orqaga" in a.get_text(strip=True):
                back_link = a
                break

        if back_link:
            href = back_link["href"]
            for key, name in SUBJECT_LINKS.items():
                if key in href:
                    return name
        return "❓ Fani aniqlanmadi"
    except Exception:
        return "❓ Fani aniqlanmadi"





# === ⚡ Tezkor HEAD tekshiruvi ===
def fast_check_exists(url):
    """
    URL mavjudligini HEAD va GET so‘rovlari bilan ishonchli tekshiradi.
    GLOBAL_SESSION orqali retry ishlaydi.
    """
    try:
        res = GLOBAL_SESSION.head(url, timeout=(3, 5), allow_redirects=True)
        if res.status_code in (200, 302):
            return True
    except requests.RequestException:
        pass

    try:
        res = GLOBAL_SESSION.get(url, timeout=(3, 5), allow_redirects=True)
        if res.status_code in (200, 302):
            return True
    except requests.RequestException as e:
        print(f"[x] HEAD/GET xato: {url} ({e})")

    return False


# === 2. Qilinmagan testlarni topish (HEAD bilan tezlashtirilgan) ===
def check_test(session, url):
    try:
        # 404 bo‘lsa darrov tashlab ketamiz
        if not fast_check_exists(url):
            return None

        response = session.get(url, timeout=5)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        if "Testni boshlash" in text and "Natijani korish" not in text:
            title_tag = soup.find("h3", class_="page-title")
            title = title_tag.get_text(strip=True) if title_tag else "Noma’lum test"

            strong_tag = soup.find("strong", string=lambda s: s and "Tugallanish vaqti" in s)
            deadline = "-"
            if strong_tag:
                span_tag = strong_tag.find_next("span", class_="text-primary")
                if span_tag:
                    deadline = span_tag.get_text(strip=True)

        # --- 7️⃣ Fan nomini olish ---
            subject = extract_subject_fast(soup)
        return (title, subject, deadline, url)

    except Exception:
        return None

def find_unfinished_tests(session, start_id=1004, end_id=1304):
    base_url = "https://lms.iiau.uz/student/my-course/calendar/resource/test/"
    unfinished = []
    urls = [f"{base_url}{i}" for i in range(start_id, end_id + 1)]

    futures = [GLOBAL_EXECUTOR.submit(check_test, session, url) for url in urls]
    for fut in as_completed(futures):
        try:
            res = fut.result()
            if res:
                unfinished.append(res)
        except Exception:
            continue
    return unfinished




# === 3. Qilinmagan topshiriqlarni topish (HEAD bilan tezlashtirilgan) ===
def check_assignment(session, url, resend_variants):
    try:
        # 1️⃣ Avval HEAD orqali mavjudligini tekshiramiz
        if not fast_check_exists(url):
            return None

        # 2️⃣ Sahifani yuklaymiz
        response = session.get(url, timeout=5)
        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # --- 3️⃣ “Jo‘natish”/“Fayl:”/“Qayta jo‘natish” shartlarini tekshiramiz ---
        has_jonatish = any(t in text for t in ["Jo’natish", "Jo'natish", "Joʻnatish", "Jo`natish"])
        has_fayl = "Fayl:" in text
        has_qayta = any(r in text for r in resend_variants)

        # 💡 4️⃣ To‘rtta shart bo‘yicha tahlil:
        # - “Jo‘natish” bor, “Fayl:” yo‘q → jo‘natilmagan (✅ qoldiramiz)
        # - “Jo‘natish” va “Fayl:” bor → jo‘natilgan (❌ o‘tkazib yuboramiz)
        # - “Qayta jo‘natish” bor, “Jo‘natish” yo‘q → jo‘natilgan (❌ o‘tkazib yuboramiz)
        # - “Jo‘natish” ham, “Qayta jo‘natish” ham yo‘q → e’tiborsiz (❌ o‘tkazib yuboramiz)

        if has_jonatish and not has_fayl:
            pass  # jo‘natilmagan — davom etamiz
        else:
            return None  # qolgan barcha holatlar e’tiborsiz

        # --- 5️⃣ Topshiriq nomini topamiz ---
        title = None
        for p in soup.find_all("p", class_="header-title"):
            if p.find("span") and "Topshiriq nomi" in p.find("span").get_text(strip=True):
                title = p.get_text(" ", strip=True).replace("Topshiriq nomi:", "").strip()
                break
        if not title:
            title = "Noma’lum topshiriq"

        # --- 6️⃣ Tugash muddatini topamiz ---
        deadline = "-"
        for p in soup.find_all("p", class_="header-title"):
            if p.find("span") and "Topshiriq muddati" in p.find("span").get_text(strip=True):
                deadline = p.get_text(" ", strip=True).replace("Topshiriq muddati", "").strip()
                break

        # --- 7️⃣ Fan nomini olish ---
            subject = extract_subject_fast(soup)
        return (title, subject, deadline, url)

    except Exception:
        return None


def find_unfinished_assignments(session, start_id=6343, end_id=6643):
    base_url = "https://lms.iiau.uz/student/my-course/calendar/resource/activity/standard-"
    resend_variants = ["Qayta jo'natish", "Qayta jo’natish", "Qayta joʻnatish", "Qayta jo`natish"]
    unfinished = []
    urls = [f"{base_url}{i}" for i in range(start_id, end_id + 1)]

    futures = [GLOBAL_EXECUTOR.submit(check_assignment, session, url, resend_variants) for url in urls]
    for fut in as_completed(futures):
        try:
            res = fut.result()
            if res:
                unfinished.append(res)
        except Exception:
            continue
    return unfinished



# === 4. Vaqt yordamchisi ===

def find_closest_deadline(items):
    """
    items: [(title, deadline_str, link), ...]
    """
    now = datetime.now(TASHKENT_TZ)
    closest_dt = None
    closest_diff = None

    for title, subject, deadline_str, link in items:
        try:
            # deadline stringni Tashkent vaqti bilan o‘qish
            dt = datetime.strptime(deadline_str.strip(), "%d-%m-%Y %H:%M:%S")
            dt = TASHKENT_TZ.localize(dt)
        except Exception:
            continue  # format xato bo‘lsa tashlab o‘tamiz

        diff = dt - now
        if diff.total_seconds() <= 0:
            continue  # muddati tugagan topshiriqlarni tashlaymiz

        if closest_diff is None or diff < closest_diff:
            closest_diff = diff
            closest_dt = dt

    return closest_dt, closest_diff


def format_timedelta(td: timedelta):
    """
    timedelta -> "X kun Y soat, Z minut" formatida chiqaradi
    """
    if not td or td.total_seconds() <= 0:
        return ""

    total_seconds = int(td.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts = []
    if days > 0:
        parts.append(f"{days} kun")
    if hours > 0:
        parts.append(f"{hours} soat")
    if minutes > 0:
        parts.append(f"{minutes} minut")

    return ", ".join(parts)


def days_left_text(deadline_str):
    try:
        dt = datetime.strptime(deadline_str.strip(), "%d-%m-%Y %H:%M:%S")
        if dt.tzinfo is None:
            dt = TASHKENT_TZ.localize(dt)
        now = datetime.now(TASHKENT_TZ)
        days = (dt.date() - now.date()).days

        if days < 0:
            return ""
        elif days == 0:
            return "(bugun tugaydi)"
        elif days == 1:
            return "(1 kun qoldi)"
        else:
            return f"({days} kun qoldi)"
    except Exception:
        return ""


# === 5. Ishlov beruvchilar ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Show keyboard and check if credentials exist
    creds = get_credentials_for(chat_id)
    if creds:
        await update.message.reply_text(
            "👋 Assalomu alaykum! Siz oldin tizimga kirgansiz. Quyidagi tugmalardan foydalaning:",
            reply_markup=keyboard,
        )
    else:
        # ask for login/password as before
        user_data[chat_id] = {"stage": "login"}
        await update.message.reply_text(
            "👋 Assalomu alaykum! Botga xush kelibsiz. Botdan foydalanish uchun login va parol kiritish kerak. \n\nIltimos, LMS dagi loginingizni kiriting:",
            reply_markup=keyboard,
        )

# === 6. Xabarlarni qayta ishlash ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # 1) Vazifalarni tekshirish
    if text == "✅ Vazifalarni tekshirish":
        creds = get_credentials_for(chat_id)
        if not creds:
            await update.message.reply_text("Siz tizimga kirmagansiz. Iltimos, /start qilib login va parol orqali tizimga kiring.")
            return

        await update.message.reply_text("⏳ Vazifalar tekshirilmoqda, 1 daqiqacha kuting...")
        loop = asyncio.get_running_loop()
        session, fullname, error = await loop.run_in_executor(GLOBAL_EXECUTOR, login_to_lms, creds["login"], creds["password"])
            
        if error:
          await update.message.reply_text(error)
          return
        
    
        tests_future = loop.run_in_executor(GLOBAL_EXECUTOR, find_unfinished_tests, session)
        assigns_future = loop.run_in_executor(GLOBAL_EXECUTOR, find_unfinished_assignments, session)
        tests, assignments = await asyncio.gather(tests_future, assigns_future)

        # Natijani yuborish
        await send_results(update, fullname, tests, assignments)
        return

    # 2) Tizimga kirish/chiqish
    if text == "🔐 Tizimdan chiqish":
        ok = delete_credentials_for(chat_id)
        user_data.pop(chat_id, None)
        if ok:
            await update.message.reply_text(
                "Siz tizimdan chiqdingiz. Iltimos, /start qilib qaytadan tizimga kiring.",
                reply_markup=keyboard,
            )
        else:
            await update.message.reply_text("Xato: login/parolni o‘chirib bo‘lmadi. Administratorga murojaat qiling.")
        return

    # 3) Interactive login flow (stated-based)
    if chat_id in user_data and user_data[chat_id].get("stage"):
        stage = user_data[chat_id]["stage"]

        if stage == "login":
            user_data[chat_id]["login"] = text
            user_data[chat_id]["stage"] = "password"
            await update.message.reply_text("🔑 Endi parolingizni kiriting:")
            return

        if stage == "password":
            login = user_data[chat_id]["login"]
            password = text
            await update.message.reply_text("⏳ Tizimga kirish tekshirilmoqda...")
            loop = asyncio.get_running_loop()

            # Bu yerda ham bloklovchi login_to_lms ni executor orqali chaqiramiz
            session, fullname, error = await loop.run_in_executor(GLOBAL_EXECUTOR, login_to_lms, login, password)


            if error:
                await update.message.reply_text(error)
                user_data.pop(chat_id, None)
                return

            saved = set_credentials_for(chat_id, login, password)
            if not saved:
                await update.message.reply_text(
                    "⚠️ Ogohlantirish: login-parolingizni saqlashda xatolik yuz berdi. Qayta kiritishni amalga oshirish kerak."
                )
            user_data.pop(chat_id, None)
            await update.message.reply_text(
                f"✅ {fullname}, tizimga muvaffaqiyatli kirdingiz!\n\nIltimos, menyudagi tugmalardan birini tanlang yoki /start deb yozing.",
                reply_markup=keyboard,
            )
            return

    # Default response
    await update.message.reply_text(
        "Iltimos, menyudagi tugmalardan birini tanlang yoki /start deb yozing.", reply_markup=keyboard
    )


# === Admin buyrug‘i: foydalanuvchilar sonini ko‘rish ===
async def users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM credentials")
    count = cursor.fetchone()[0] or 0
    cursor.close()
    db.close()
    await update.message.reply_text(f"👥 Hozirgi foydalanuvchilar soni: {count}")         

# === Admin xabari: barcha foydalanuvchilarga xabar yuborish ===
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    creds = load_all_credentials()
    if not creds:
        await update.message.reply_text("⚠️ Hozircha tizimda foydalanuvchi yo‘q.")
        return
    if len(context.args) == 0:
        await update.message.reply_text("✏️ Foydalanish: /broadcast Siz yubormoqchi bo‘lgan xabar matni")
        return
    message_text = " ".join(context.args)
    success = 0
    fail = 0
    for chat_id in creds.keys():
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=message_text)
            success += 1
            await asyncio.sleep(0.1)
        except Exception:
            fail += 1
            continue
    await update.message.reply_text(
        f"📢 Xabar yuborildi!\n✅ Muvaffaqiyatli: {success}\n❌ Xatolik: {fail}"
    )

# === Vazifalar tekshiruv natijasini ===
async def send_results(update: Update, fullname, tests, assignments):
        def parse_deadline(item):
          # item: (title, subject, deadline_str, url)
            try:
              _, _, deadline_str, _ = item
              return datetime.strptime(deadline_str.strip(), "%d-%m-%Y %H:%M:%S")
            except Exception:
              return datetime.max  # Sana xato bo‘lsa, oxirida tursin

    # 🕒 Testlar va topshiriqlarni tartiblash (eng yaqin muddat birinchi)
        tests = sorted(tests, key=parse_deadline) if tests else []
        assignments = sorted(assignments, key=parse_deadline) if assignments else []

        

        if not tests and not assignments:
            await update.message.reply_text(
                f"👤 Hurmatli {fullname}! \n\n✅ *SIZDA BARCHA TEST VA TOPSHIRIQLAR BAJARILGAN!*",
                parse_mode="Markdown",
            )
        else:
            msg = f"👤 Hurmatli {fullname}! \n\n"
            test_count = len(tests)
            assign_count = len(assignments)
            count_parts = []
            if test_count > 0:
              count_parts.append(f"{test_count} ta test")
            if assign_count > 0:
              count_parts.append(f"{assign_count} ta topshiriq")
            count_text = " va ".join(count_parts)
            msg += f"❗️ *Sizda {count_text} bajarilmagan👇 *\n\n"

            if tests:
                
                for title, subject, deadline, link in tests:
                    left = days_left_text(deadline)
                    # Soatni "23:00" ko‘rinishida formatlaymiz
                    try:
                      short_deadline = datetime.strptime(deadline, "%d-%m-%Y %H:%M:%S").strftime("%d-%m-%Y %H:%M")
                    except Exception:
                      short_deadline = deadline
                    msg += f"📘 *Test:* *{title}* ([ko‘rish]({link}))\n⏱️ Tugash: {short_deadline} _{left}_\n📓 Fan: {subject}\n\n"


            if assignments:
                
                for title, subject, deadline, link in assignments:
                    left = days_left_text(deadline)
                    try:
                      short_deadline = datetime.strptime(deadline, "%d-%m-%Y %H:%M:%S").strftime("%d-%m-%Y %H:%M")
                    except Exception:
                      short_deadline = deadline                  
                    msg += f"📕 *Topshiriq:* *{title}* ([ko‘rish]({link}))\n⏱️ Tugash: {short_deadline} _{left}_\n📓 Fan: {subject}\n\n"
            
            # 🕓 Eng yaqin deadline
            all_items = tests + assignments
            closest_deadline, closest_diff = find_closest_deadline(all_items)
            if closest_deadline:
                remaining = format_timedelta(closest_diff)
                msg += f"```lms.iiau.uz ⏳ Sizdagi eng yaqin deadline tugashiga {remaining} qoldi! ``` \n\n"            
            await update.message.reply_markdown(msg, disable_web_page_preview=True)

# === 6. Botni ishga tushirish ===
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", users))  # 🆕 admin uchun buyruq
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("broadcast", broadcast))


    print("🤖 Bot ishga tushdi! Endi Telegramda /start deb yozing.")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
