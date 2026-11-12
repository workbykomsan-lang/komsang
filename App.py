
# PriceGate - Backend حقيقي | Flask + Facebook / Instagram Graph API
# بيشغل: Login بفيسبوك + انستجرام، طلب الصلاحيات، جلب الريلزات، الرد على الكومنتات، إرسال DM، Follow Gate
# مهم: ده كود حقيقي بس مش هيشتغل غير لما تحط بيانات الـ Facebook App بتاعك في ملف .env وترفعه على سيرفر

import os, json, requests
from flask import Flask, request, redirect, jsonify, session
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "pricegate-secret-change-me")

APP_ID = os.getenv("FACEBOOK_APP_ID")
APP_SECRET = os.getenv("FACEBOOK_APP_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")  # مثال: https://pricegate.onrender.com/callback
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "pricegate_verify_123")

GRAPH = "https://graph.facebook.com/v19.0"
# الصلاحيات المطلوبة - دي اللي طلبتها
SCOPES = "instagram_basic,instagram_manage_comments,instagram_manage_messages,instagram_manage_insights,pages_show_list,pages_read_engagement,pages_messaging"

# تخزين بسيط - في الإنتاج استخدم Supabase / Postgres بدل ده
# الشكل: { ig_user_id: { access_token, page_id, ig_id, automations: [...] } }
USERS = {}

# ============ 1) تسجيل الدخول ============
@app.route("/login")
def login():
    # بيودي المستخدم لفيسبوك عشان يوافق على الصلاحيات
    url = f"https://www.facebook.com/v19.0/dialog/oauth?client_id={APP_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPES}&response_type=code&auth_type=rerequest"
    return redirect(url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "مفيش كود - المستخدم رفض؟ " + str(request.args), 400
    # 1) بدل الكود بتوكن قصير
    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "client_id": APP_ID, "client_secret": APP_SECRET,
        "redirect_uri": REDIRECT_URI, "code": code
    }).json()
    short_token = r.get("access_token")
    if not short_token:
        return jsonify(r), 400
    # 2) حوله لتوكن طويل (60 يوم)
    r2 = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": APP_ID,
        "client_secret": APP_SECRET, "fb_exchange_token": short_token
    }).json()
    long_token = r2.get("access_token", short_token)
    session["access_token"] = long_token
    # 3) هات الصفحات وحساب الانستجرام المربوط
    pages = requests.get(f"{GRAPH}/me/accounts", params={"access_token": long_token, "fields": "id,name,instagram_business_account"}).json()
    # خد أول صفحة فيها انستجرام
    ig_id = None; page_id = None
    for p in pages.get("data", []):
        if p.get("instagram_business_account"):
            ig_id = p["instagram_business_account"]["id"]; page_id = p["id"]; break
    if not ig_id:
        return jsonify({"error": "مفيش حساب انستجرام Business مربوط بصفحة فيسبوك", "pages": pages})
    # 4) هات بيانات الانستجرام
    me = requests.get(f"{GRAPH}/{ig_id}", params={"access_token": long_token, "fields": "username,profile_picture_url,followers_count"}).json()
    USERS[ig_id] = {"access_token": long_token, "page_id": page_id, "ig_id": ig_id, "username": me.get("username"), "automations": []}
    # رجعه للواجهة ومعاه الـ ig_id
    frontend = os.getenv("FRONTEND_URL", "/")
    return redirect(f"{frontend}?connected=1&ig_id={ig_id}&username={me.get('username')}")

@app.route("/api/me")
def me():
    # الفرونت بيكلم دي عشان يعرف مين المتصل
    ig_id = request.args.get("ig_id")
    u = USERS.get(ig_id)
    if not u: return jsonify({"connected": False})
    return jsonify({"connected": True, "username": u["username"], "ig_id": ig_id, "page_id": u["page_id"]})

# ============ 2) عرض الريلزات ============
@app.route("/api/reels")
def reels():
    ig_id = request.args.get("ig_id")
    u = USERS.get(ig_id)
    if not u: return jsonify({"error": "مش متصل - سجل دخول الأول"}), 401
    # جلب آخر 20 ريل/فيديو
    res = requests.get(f"{GRAPH}/{ig_id}/media", params={
        "access_token": u["access_token"],
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count",
        "limit": 20
    }).json()
    return jsonify(res)

# ============ 3) حفظ إعدادات الأوتوميشن لريل معين ============
@app.route("/api/automation", methods=["POST"])
def save_automation():
    data = request.json  # { ig_id, media_id, keyword, public_reply, dm_text, dm_link, dm_file_url, follow_gate: true/false }
    u = USERS.get(data.get("ig_id"))
    if not u: return jsonify({"error": "مش متصل"}), 401
    # امسح القديم لنفس الريل وضيف الجديد
    u["automations"] = [a for a in u["automations"] if a["media_id"] != data["media_id"]]
    u["automations"].append(data)
    return jsonify({"ok": True, "automations": u["automations"]})

# ============ 4) Follow Gate - التحقق من المتابعة ============
def check_is_follower(ig_id, access_token, commenter_username):
    """
    الحقيقة المهمة: ميتا قفلت الـ API المباشر اللي بيقولك 'الشخص ده متابعك ولا لا' من 2023.
    الحل الحقيقي اللي شغال عند كل المواقع الكبيرة:
    1) أول مرة: ابعت DM 'اعمل Follow الأول' + زرار تحقق
    2) لما يدوس تحقق، بنجرب Business Discovery لو متاح، ولو مش متاح بنطلب منه يكتب الكلمة تاني بعد ما يتابع
    الكود هنا بيحاول الطريقة المتاحة، ولو فشل بيرجع False وبيخلي الفلو يكمل بزرار التحقق.
    """
    try:
        # محاولة عبر Business Discovery - بتشتغل لو الحساب التاني Business/Creator وسمح
        r = requests.get(f"{GRAPH}/{ig_id}", params={
            "access_token": access_token,
            "fields": f"business_discovery.username({commenter_username}){{followers_count,follows_count}}"
        }).json()
        # لو رجع بيانات يبقى الحساب موجود، لكن ده مش إثبات متابعة 100% - عشان كده بنعتمد على زرار التحقق + الكومنت التاني كتأكيد
        if "business_discovery" in r:
            return None  # None = مش متأكدين، اطلب تحقق يدوي
    except Exception as e:
        print("follow check error", e)
    return False

# ============ 5) Webhook - استقبال الكومنتات والرد ============
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    # فيسبوك بيبعت ده أول مرة عشان يتأكد إن السيرفر بتاعك
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "verify token غلط", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("WEBHOOK:", json.dumps(data, ensure_ascii=False))
    # الشكل اللي بيجي من انستجرام: entry -> changes -> comments
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            val = change.get("value", {})
            comment_id = val.get("id")
            comment_text = (val.get("text") or "").lower()
            commenter = val.get("from", {})
            commenter_id = commenter.get("id")
            commenter_username = commenter.get("username", "")
            media_id = val.get("media", {}).get("id") or val.get("parent_id")
            # دور على أوتوميشن الريل ده - بنجرب كل المستخدمين (في الإنتاج اربط بالـ ig_id من الـ entry)
            for ig_id, u in list(USERS.items()):
                for auto in u.get("automations", []):
                    if auto["media_id"] != media_id and auto.get("media_id") != "all":
                        continue
                    keyword = (auto.get("keyword") or "بكام").lower()
                    if keyword not in comment_text and keyword != "all":
                        continue
                    token = u["access_token"]
                    # 1) رد عام في الكومنت
                    requests.post(f"{GRAPH}/{comment_id}/replies", data={
                        "access_token": token, "message": auto.get("public_reply", "بعتلك في الـ DM يا بطل 🔥")
                    })
                    # 2) لو Follow Gate شغال، اتأكد الأول
                    if auto.get("follow_gate"):
                        # ابعت DM يطلب المتابعة + زرار تحقق
                        # ملحوظة: زرار التحقق بيتعمل بـ Quick Replies / Buttons في Messenger API - هنا بنبعت رسالة نصية فيها تعليمات
                        msg = f"عشان ابعتلك {auto.get('dm_label','السعر / اللينك')} لازم تعمل Follow الأول ❤️\nاعمل Follow لـ @{u['username']} وبعدين اكتب '{keyword}' تاني أو رد عليا هنا بكلمة 'تابعت' وهبعتلك على طول."
                        send_dm(u["ig_id"], commenter_id, msg, token)
                    else:
                        send_dm(u["ig_id"], commenter_id, build_dm_text(auto), token)
    return "OK", 200

def build_dm_text(auto):
    t = auto.get("dm_text", "")
    if auto.get("dm_link"):
        t += f"\n\nاللينك: {auto['dm_link']}"
    if auto.get("dm_file_url"):
        t += f"\nالملف: {auto['dm_file_url']}"
    return t

def send_dm(ig_id, recipient_id, text, token):
    # إرسال DM عبر Instagram Graph API
    return requests.post(f"{GRAPH}/{ig_id}/messages", json={
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }, params={"access_token": token}).json()

# لما المستخدم يرد في الـ DM بكلمة "تابعت" - فيسبوك هيبعت Webhook تاني من نوع messages
# هتلاقي الكود بتاعه في README - بنفس فكرة الـ comments

@app.route("/")
def home():
    return jsonify({"status": "PriceGate Backend شغال", "login": "/login", "webhook": "/webhook"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
