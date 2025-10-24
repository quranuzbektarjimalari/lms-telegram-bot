import os
import json
import nest_asyncio
import asyncio
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pytz

# Encryption
from cryptography.fernet import Fernet

nest_asyncio.apply()

# BOT_TOKEN should be set in environment for safety. Fallback to previous token only if env missing.
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8469849269:AAHWt3-X4peInBtbPNgDQSuLL1su1cyo7WE"

# Files for storing encrypted credentials
CRED_FILE = "credentials.json"
KEY_FILE = "secret.key"

# In-memory user data (session state)
user_data = {}

# Tashkent timezone
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

# UI buttons (Reply keyboard)
keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("✅ Vazifalarni tekshirish"), KeyboardButton("🔄 Qayta tizimga kirish")]],
    resize_keyboard=True,
)


# ------------------ Encryption helpers ------------------

def ensure_key():
    """Ensure a Fernet key exists on disk and return Fernet instance."""
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
    else:
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    return Fernet(key)


def load_all_credentials():
    """Load and decrypt all credentials from disk. Returns dict chat_id -> {login, password}.
    If file missing, returns {}.
    """
    if not os.path.exists(CRED_FILE):
        return {}
    try:
        with open(CRED_FILE, "rb") as f:
            encrypted = f.read()
        if not encrypted:
            return {}
        fernet = ensure_key()
        data_json = fernet.decrypt(encrypted).decode("utf-8")
        return json.loads(data_json)
    except Exception:
        # If decryption fails, don't crash — return empty and warn later
        return {}


def save_all_credentials(all_creds: dict):
    """Encrypt and write all credentials to disk."""
    try:
        fernet = ensure_key()
        j = json.dumps(all_creds)
        token = fernet.encrypt(j.encode("utf-8"))
        with open(CRED_FILE, "wb") as f:
            f.write(token)
        return True
    except Exception:
        return False


def get_credentials_for(chat_id):
    allc = load_all_credentials()
    return allc.get(str(chat_id))


def set_credentials_for(chat_id, login, password):
    allc = load_all_credentials()
    allc[str(chat_id)] = {"login": login, "password": password}
    return save_all_credentials(allc)


def delete_credentials_for(chat_id):
    allc = load_all_credentials()
    if str(chat_id) in allc:
        allc.pop(str(chat_id))
        return save_all_credentials(allc)
    return True


# === 1. LMS tizimiga kirish (brauzerdek ishlaydigan sessiya) ===
def login_to_lms(username, password):
    session = requests.Session()
    login_url = "https://lms.iiau.uz/auth/login"

    # 💻 Brauzer headers – sayt bot emas, foydalanuvchi kiryapti deb o‘ylaydi
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uz-UZ,uz;q=0.9,en;q=0.8,ru;q=0.7",
        "Connection": "keep-alive",
        "Referer": "https://lms.iiau.uz/auth/login",
        "Upgrade-Insecure-Requests": "1",
    }

    # 1️⃣ Login sahifasini olish
    response = session.get(login_url, headers=headers)
    if response.status_code != 200:
        return None, None, "❌ LMS sahifasiga ulanib bo‘lmadi."

    # 2️⃣ Tokenni olish
    soup = BeautifulSoup(response.text, "html.parser")
    token_tag = soup.find("input", {"name": "_token"})
    token = token_tag["value"] if token_tag else ""

    # 3️⃣ Login so‘rovini yuborish
    payload = {
        "_token": token,
        "login": username,
        "password": password,
        "g-recaptcha-response": ""
    }

    login_response = session.post(login_url, data=payload, headers=headers)

    # 4️⃣ Kirish muvaffaqiyatli bo‘lganini tekshirish
    if "logout" in login_response.text or "Chiqish" in login_response.text:
        fullname = "Noma’lum foydalanuvchi"
        try:
            dashboard = session.get("https://lms.iiau.uz/dashboard", headers=headers, timeout=10)
            prof_soup = BeautifulSoup(dashboard.text, "html.parser")
            span_tag = prof_soup.select_one("button#dropLogin span")
            if span_tag and span_tag.get_text(strip=True):
                fullname = span_tag.get_text(strip=True)
        except:
            pass

        return session, fullname, None
    else:
        return None, None, "❌ Login yoki parol noto‘g‘ri bo‘lishi mumkin."


# === ⚡ Tezkor HEAD tekshiruvi ===
def fast_check_exists(session, url):
    try:
        head = session.head(url, timeout=3)
        return head.status_code == 200
    except:
        # Fallback: try GET with tiny timeout
        try:
            r = session.get(url, timeout=3)
            return r.status_code == 200
        except:
            return False

# === 2. Qilinmagan testlarni topish (HEAD bilan tezlashtirilgan) ===
def check_test(session, url):
    try:
        # 404 bo‘lsa darrov tashlab ketamiz
        if not fast_check_exists(session, url):
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
            subject = "-"
            try:
                # "Orqaga" tugmasi joylashgan divni topamiz
                header_div = soup.find("div", class_="page-title text-right page-title--space")
                if header_div:
                  back_link = header_div.find("a", href=True)
                  if back_link:
                    href = back_link["href"].strip()
                    # to‘liq URL yasaymiz
                    if href.startswith("http"):
                        back_url = href
                    else:
                        back_url = "https://lms.iiau.uz" + href
                    
                    # "Orqaga" sahifasini ochamiz
                    back_page = session.get(back_url, timeout=5)
                    if back_page.status_code == 200:
                        back_soup = BeautifulSoup(back_page.text, "html.parser")
                        div_tag = back_soup.find("div", class_="page-title")
                        if div_tag and div_tag.get_text(strip=True):
                            subject = div_tag.get_text(strip=True)
            except:
              pass

            return (title, subject, deadline, url)
    except Exception:
        return None

def find_unfinished_tests(session, start_id=1004, end_id=1304):
    base_url = "https://lms.iiau.uz/student/my-course/calendar/resource/test/"
    unfinished = []
    urls = [f"{base_url}{i}" for i in range(start_id, end_id + 1)]

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(check_test, session, url) for url in urls]
        for future in as_completed(futures):
            result = future.result()
            if result:
                unfinished.append(result)

    return unfinished


# === 3. Qilinmagan topshiriqlarni topish (HEAD bilan tezlashtirilgan) ===
def check_assignment(session, url, resend_variants):
    try:
        # 1️⃣ Avval HEAD orqali mavjudligini tekshiramiz
        if not fast_check_exists(session, url):
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
            subject = "-"
            try:
                # "Orqaga" tugmasi joylashgan divni topamiz
                header_div = soup.find("div", class_="page-title text-right page-title--space")
                if header_div:
                  back_link = header_div.find("a", href=True)
                  if back_link:
                    href = back_link["href"].strip()
                    # to‘liq URL yasaymiz
                    if href.startswith("http"):
                        back_url = href
                    else:
                        back_url = "https://lms.iiau.uz" + href
                    
                    # "Orqaga" sahifasini ochamiz
                    back_page = session.get(back_url, timeout=5)
                    if back_page.status_code == 200:
                        back_soup = BeautifulSoup(back_page.text, "html.parser")
                        div_tag = back_soup.find("div", class_="page-title")
                        if div_tag and div_tag.get_text(strip=True):
                            subject = div_tag.get_text(strip=True)
            except:
              pass


        # 🔚 Natijani fan nomi bilan qaytaramiz
        return (title, subject, deadline, url)

    except Exception:
        return None


def find_unfinished_assignments(session, start_id=6343, end_id=6643):
    base_url = "https://lms.iiau.uz/student/my-course/calendar/resource/activity/standard-"
    resend_variants = ["Qayta jo'natish", "Qayta jo’natish", "Qayta joʻnatish", "Qayta jo`natish"]
    unfinished = []
    urls = [f"{base_url}{i}" for i in range(start_id, end_id + 1)]

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(check_assignment, session, url, resend_variants) for url in urls]
        for future in as_completed(futures):
            result = future.result()
            if result:
                unfinished.append(result)

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
        # Masalan: "25-10-2025 23:00:00"
        dt = datetime.strptime(deadline_str.strip(), "%d-%m-%Y %H:%M:%S")
        dt = TASHKENT_TZ.localize(dt)
        now = datetime.now(TASHKENT_TZ)
        diff = dt - now
        days = diff.days

        # O‘tgan sanalar uchun hech narsa chiqmasin
        if days < 0:
            return ""
        elif days == 0:
            return "(bugun)"
        elif days == 1:
            return "(1 kun)"
        else:
            return f"({days} kun)"
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

    # Button: Vazifalarni tekshirish
    if text == "✅ Vazifalarni tekshirish":
        creds = get_credentials_for(chat_id)
        if not creds:
            await update.message.reply_text("Siz tizimga kirmagansiz. Iltimos /start orqali login va parolingizni kiriting.")
            return
        await update.message.reply_text("⏳ Vazifalar tekshirilmoqda, 1 daqiqacha kuting...")
        session, fullname, error = login_to_lms(creds["login"], creds["password"])
        if error:
            await update.message.reply_text(error)
            return
        tests = find_unfinished_tests(session)
        assignments = find_unfinished_assignments(session)
        await send_results(update, fullname, tests, assignments)
        return

    # Button: Qayta tizimga kirish
    if text == "🔄 Qayta tizimga kirish":
        # Remove credentials and reset state
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

    # If user in interactive login flow
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
            
            session, fullname, error = login_to_lms(login, password)
            if error:
              await update.message.reply_text(error)
              user_data.pop(chat_id, None)
              return
            # ✅ Login-parolni saqlaymiz
            saved = set_credentials_for(chat_id, login, password)
            if not saved:
              await update.message.reply_text("⚠️ Ogohlantirish: login-parolingizni saqlashda xatolik yuz berdi. Qayta kiritishni amalga oshirish kerak."
              )
            user_data.pop(chat_id, None)  # jarayonni tozalaymiz
            await update.message.reply_text(
                f"✅ {fullname}, tizimga muvaffaqiyatli kirdingiz!\n\n"
                "Iltimos, menyudagi tugmalardan birini tanlang yoki /start deb yozing.",
                reply_markup=keyboard,
                )

            return
    # Default response if unknown text
    await update.message.reply_text(
        "Iltimos, menyudagi tugmalardan birini tanlang yoki /start deb yozing.", reply_markup=keyboard
        )


async def send_results(update: Update, fullname, tests, assignments):
    if not tests and not assignments:
        await update.message.reply_text(
            f"👤 {fullname}, sizda quyidagilar aniqlandi:\n\n✅ *BARCHA TEST VA TOPSHIRIQLAR BAJARILGAN!*",
            parse_mode="Markdown",
        )
    else:
      msg = f"👤 {fullname}, sizda quyidagilar aniqlandi:\n\n"
    
    if tests:
        msg += "❗ *BAJARILMAGAN TESTLAR 👇*\n\n"
        for title, subject, deadline, link in tests:
            left = days_left_text(deadline)
            # Soatni "23:00" ko‘rinishida formatlaymiz
            try:
                short_deadline = datetime.strptime(deadline, "%d-%m-%Y %H:%M:%S").strftime("%d-%m-%Y %H:%M")
            except Exception:
                short_deadline = deadline
                msg += f"📘 *{title}* ([ko‘rish]({link}))\n🕒 Tugash: {left} {short_deadline}\n👉 {subject}\n\n"

    if assignments:
        msg += "❗ *BAJARILMAGAN TOPSHIRIQLAR 👇*\n\n"
        for title, subject, deadline, link in assignments:
            left = days_left_text(deadline)
            try:
                short_deadline = datetime.strptime(deadline, "%d-%m-%Y %H:%M:%S").strftime("%d-%m-%Y %H:%M")
            except Exception:
                short_deadline = deadline                  
            msg += f"📘 *{title}* ([ko‘rish]({link}))\n🕒 Tugash: {left} {short_deadline}\n👉 {subject}\n\n"
            
            # 🕓 Eng yaqin deadline
    all_items = tests + assignments
    closest_deadline, closest_diff = find_closest_deadline(all_items)
    if closest_deadline:
        remaining = format_timedelta(closest_diff)
        msg += f"```lms.iiau.uz ⏳ Sizdagi eng yaqin deadline tugashiga {remaining} qoldi! ``` \n\n"            
    await update.message.reply_markdown(msg)
            

# === 6. Botni ishga tushirish ===
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot ishga tushdi! Endi Telegramda /start deb yozing.")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())




