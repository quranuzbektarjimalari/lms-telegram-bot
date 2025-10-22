import nest_asyncio, asyncio, requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from concurrent.futures import ThreadPoolExecutor, as_completed

nest_asyncio.apply()

BOT_TOKEN = "8469849269:AAHWt3-X4peInBtbPNgDQSuLL1su1cyo7WE"
user_data = {}

# === 1. LMS tizimiga kirish ===
def login_to_lms(username, password):
    session = requests.Session()
    login_url = "https://lms.iiau.uz/auth/login"

    response = session.get(login_url)
    if response.status_code != 200:
        return None, "❌ LMS sahifasiga ulanib bo‘lmadi."

    soup = BeautifulSoup(response.text, "html.parser")
    token_tag = soup.find("input", {"name": "_token"})
    token = token_tag["value"] if token_tag else ""

    payload = {
        "_token": token,
        "login": username,
        "password": password,
        "g-recaptcha-response": ""
    }

    login_response = session.post(login_url, data=payload)
    if "logout" in login_response.text or "Chiqish" in login_response.text:
        # foydalanuvchi ismini olishga urinish
        fullname = "Noma’lum foydalanuvchi"
        try:
            dashboard = session.get("https://lms.iiau.uz/dashboard", timeout=10)
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
    """Sahifa mavjudligini HEAD orqali tezda tekshiradi"""
    try:
        head = session.head(url, timeout=3)
        return head.status_code == 200
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

# === 4. /start komandasi ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data[update.effective_chat.id] = {"stage": "login"}
    await update.message.reply_text(
        "👋 Assalomu alaykum! LMS vazifalarini tekshiruvchi botga xush kelibsiz. Botdan foydalanish uchun login va parol terish kerak. \n\nIltimos, LMS dagi loginingizni kiriting:"
    )
from datetime import datetime, timedelta
import pytz

# Tashkent vaqt zonasi
TASHKENT_TZ = pytz.timezone("Asia/Tashkent")

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

# === 5. Xabarlarni qayta ishlash ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if chat_id not in user_data:
        await update.message.reply_text("Boshlash uchun /start deb yozing va login-parol kiriting.")
        return

    stage = user_data[chat_id]["stage"]

    # 1. Login bosqichi
    if stage == "login":
        user_data[chat_id]["login"] = text
        user_data[chat_id]["stage"] = "password"
        await update.message.reply_text("🔑 Endi parolingizni kiriting:")

    # 2. Parol bosqichi
    elif stage == "password":
        login = user_data[chat_id]["login"]
        password = text
        await update.message.reply_text("⏳ Vazifalar tekshirilmoqda, 1 daqiqacha kuting...")

        session, fullname, error = login_to_lms(login, password)
        if error:
            await update.message.reply_text(error)
            user_data.pop(chat_id, None)
            return

        tests = find_unfinished_tests(session)
        assignments = find_unfinished_assignments(session)

        user_data[chat_id]["stage"] = "done"

        if not tests and not assignments:
            await update.message.reply_text(
                f"👤 {fullname}, sizda quyidagilar aniqlandi:\n\n✅ *BARCHA TEST VA TOPSHIRIQLAR BAJARILGAN!*",
                parse_mode="Markdown",
            )
        else:
            msg = f"👤 {fullname}, sizda quyidagilar aniqlandi:\n\n"

            # 🕓 Eng yaqin deadline
            all_items = tests + assignments
            closest_deadline, closest_diff = find_closest_deadline(all_items)
            if closest_deadline:
                remaining = format_timedelta(closest_diff)
                msg += f"⏳ Eng yaqin deadline {remaining}dan keyin tugaydi! \n\n"

            if tests:
                msg += "❗ *BAJARILMAGAN TESTLAR 👇*\n\n"
                for title, subject, deadline, link in tests:
                    msg += f"📘 *{title}* ([ko‘rish]({link}))\n🕒 Tugash vaqti: {deadline}\n👉 {subject}\n\n"


            if assignments:
                msg += "❗ *BAJARILMAGAN TOPSHIRIQLAR 👇*\n\n"
                for title, subject, deadline, link in assignments:
                    msg += f"📘 *{title}* ([ko‘rish]({link}))\n🕒 Tugash vaqti: {deadline}\n👉 {subject}\n\n"


            await update.message.reply_markdown(msg)

    # 3. Tugallangan bosqich
    elif stage == "done":
        await update.message.reply_text(
            "🔐 Siz avval LMS tekshiruvini yakunlagansiz.\n"
            "Agar yana tekshirishni xohlasangiz, /start deb yozing va qayta login qiling."
        )
# === 6. Botni ishga tushirish ===
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot ishga tushdi! Endi Telegramda /start deb yozing.")
    await app.run_polling()

asyncio.run(main())









