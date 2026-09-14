import os
import json
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, Header, HTTPException
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError

from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)

from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent
)

from google import genai
from google.genai import types

from supabase import create_client, Client


# =========================================================
# LOAD ENVIRONMENT VARIABLES
# =========================================================

load_dotenv()

app = FastAPI()

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


# =========================================================
# LINE SETUP
# =========================================================

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET if LINE_CHANNEL_SECRET else "dummy_secret")


# =========================================================
# GEMINI SETUP
# =========================================================

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# =========================================================
# SUPABASE SETUP
# =========================================================

supabase: Client = None

if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================================================
# GEMINI MODEL
# =========================================================

MODEL_NAME = "gemini-3.5-flash-lite"


# =========================================================
# GET USER PROFILE
# =========================================================

def get_user_profile(user_id: str) -> dict:
    """ดึงข้อมูลโปรไฟล์ผู้ใช้จากตาราง users ใน Supabase"""
    if not supabase:
        return {}

    try:
        response = (
            supabase
            .table("users")
            .select("age,gender,weight_kg,height_cm,medical_conditions")
            .eq("user_id", user_id)
            .execute()
        )
        if response.data and len(response.data) > 0:
            return response.data[0]
        return {}
    except Exception as e:
        print(f"Get user profile error: {e}")
        return {}


# =========================================================
# SAVE USER PROFILE (ตาราง users)
# =========================================================

def save_user_profile(user_id: str, profile_data: dict):
    """บันทึกหรืออัปเดตโปรไฟล์ผู้ใช้ลงตาราง users"""
    if not supabase or not profile_data:
        return False

    update_payload = {"user_id": user_id}

    if profile_data.get("age") is not None:
        try:
            age = int(profile_data["age"])
            if 0 < age < 150:
                update_payload["age"] = age
        except (ValueError, TypeError): pass

    if profile_data.get("gender"):
        update_payload["gender"] = str(profile_data["gender"])[:20]

    if profile_data.get("weight_kg") is not None:
        try:
            weight = float(profile_data["weight_kg"])
            if 0 < weight < 500:
                update_payload["weight_kg"] = weight
        except (ValueError, TypeError): pass

    if profile_data.get("height_cm") is not None:
        try:
            height = float(profile_data["height_cm"])
            if 0 < height < 300:
                update_payload["height_cm"] = height
        except (ValueError, TypeError): pass

    if profile_data.get("medical_conditions"):
        update_payload["medical_conditions"] = str(profile_data["medical_conditions"])[:500]

    # บันทึกเฉพาะเมื่อมีข้อมูลโปรไฟล์อย่างน้อย 1 ฟิลด์
    if len(update_payload) > 1:
        try:
            supabase.table("users").upsert(update_payload, on_conflict="user_id").execute()
            print(f"Updated user profile for {user_id}: {update_payload}")
            return True
        except Exception as e:
            print(f"Supabase update profile error: {e}")
            return False
    return False


# =========================================================
# CHECK TODAY LOG
# =========================================================

def check_today_log_exists(user_id: str) -> bool:
    """เช็คว่าวันนี้ผู้ใช้เคยบันทึกข้อมูลสุขภาพไปแล้วหรือยัง"""
    if not supabase:
        return False

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    try:
        response = (
            supabase
            .table("health_logs")
            .select("id")
            .eq("user_id", user_id)
            .gte("created_at", today_start)
            .execute()
        )
        return len(response.data) > 0
    except Exception as e:
        print(f"Check today log error: {e}")
        return False


# =========================================================
# CLEAN HEALTH DATA
# =========================================================

def clean_health_data(data: dict) -> dict:
    result = {
        "sleep_hours": None,
        "exercise_minutes": None,
        "stress_level": None,
        "mood": None,
        "water_ml": None,
        "notes": None
    }

    # SLEEP
    sleep = data.get("sleep_hours")
    if sleep is not None:
        try:
            sleep = float(sleep)
            if 0 <= sleep <= 24:
                result["sleep_hours"] = sleep
        except (ValueError, TypeError): pass

    # EXERCISE
    exercise = data.get("exercise_minutes")
    if exercise is not None:
        try:
            exercise = int(exercise)
            if 0 <= exercise <= 1440:
                result["exercise_minutes"] = exercise
        except (ValueError, TypeError): pass

    # STRESS
    stress = data.get("stress_level")
    if stress is not None:
        try:
            stress = int(stress)
            if 1 <= stress <= 10:
                result["stress_level"] = stress
        except (ValueError, TypeError): pass

    # MOOD
    mood = data.get("mood")
    if mood:
        result["mood"] = str(mood)[:200]

    # WATER
    water = data.get("water_ml")
    if water is not None:
        try:
            water = int(water)
            if 0 <= water <= 20000:
                result["water_ml"] = water
        except (ValueError, TypeError): pass

    # NOTES
    notes = data.get("notes")
    if notes:
        result["notes"] = str(notes)[:2000]

    return result


# =========================================================
# CHECK HEALTH DATA
# =========================================================

def contains_health_data(data: dict) -> bool:
    fields = [
        "sleep_hours",
        "exercise_minutes",
        "stress_level",
        "mood",
        "water_ml",
        "notes"
    ]
    return any(data.get(field) is not None for field in fields)


# =========================================================
# SAVE HEALTH LOG (ตาราง health_logs)
# =========================================================

def save_health_log(user_id: str, data: dict):
    if not supabase:
        print("Supabase is not configured.")
        return False

    cleaned = clean_health_data(data)

    if not contains_health_data(cleaned):
        return False

    log_data = {
        "user_id": user_id,
        "sleep_hours": cleaned["sleep_hours"],
        "exercise_minutes": cleaned["exercise_minutes"],
        "stress_level": cleaned["stress_level"],
        "notes": cleaned["notes"]
    }

    try:
        supabase.table("health_logs").insert(log_data).execute()
        print(f"Saved health log for {user_id}")
        return True
    except Exception as e:
        print(f"Supabase save log error: {e}")
        return False


# =========================================================
# ASK GEMINI
# =========================================================

def ask_gemini(user_message: str, has_logged_today: bool, user_profile: dict = None):
    if not ai_client:
        print("Gemini client is not configured.")
        return None

    # สรุปโปรไฟล์เดิมของผู้ใช้เพื่อแจ้ง AI
    profile_text = "ไม่ระบุ (ประเมินตามเกณฑ์ผู้ใหญ่ทั่วไป)"
    if user_profile:
        details = []
        if user_profile.get("age"): details.append(f"อายุ {user_profile['age']} ปี")
        if user_profile.get("gender"): details.append(f"เพศ {user_profile['gender']}")
        if user_profile.get("weight_kg"): details.append(f"น้ำหนัก {user_profile['weight_kg']} กก.")
        if user_profile.get("height_cm"): details.append(f"ส่วนสูง {user_profile['height_cm']} ซม.")
        if user_profile.get("medical_conditions"): details.append(f"โรคประจำตัว/ข้อควรระวัง: {user_profile['medical_conditions']}")
        if details:
            profile_text = ", ".join(details)

    if has_logged_today:
        asking_rule = "- วันนี้ผู้ใช้ **บันทึกข้อมูลสุขภาพประจำวันเรียบร้อยแล้ว** -> ตอบรับอย่างเป็นมิตร คอยให้คำแนะนำ ห้ามถามชวนบันทึกข้อมูลสุขภาพซ้ำอีก"
    else:
        asking_rule = "- วันนี้ผู้ใช้ **ยังไม่ได้บันทึกข้อมูลสุขภาพ** -> ตอบรับอย่างเป็นมิตรพร้อมชวนคุยถามถึงการนอน ออกกำลังกาย หรือความเครียด เพื่อกระตุ้นให้บันทึกข้อมูล"

    dynamic_system_prompt = f"""
คุณคือ "หมอ DAI" ผู้ช่วยติดตามสุขภาพและพฤติกรรมการใช้ชีวิต

ข้อมูลโปรไฟล์ปัจจุบันของผู้ใช้: [{profile_text}]

ตอบเป็น JSON เท่านั้น:
{{
  "reply_text": "ข้อความตอบผู้ใช้",
  "sleep_hours": null,
  "exercise_minutes": null,
  "stress_level": null,
  "mood": null,
  "water_ml": null,
  "notes": null,
  "user_profile_update": {{
    "age": null,
    "gender": null,
    "weight_kg": null,
    "height_cm": null,
    "medical_conditions": null
  }}
}}

กฎ:
- หากผู้ใช้ระบุหรือบอกข้อมูลส่วนบุคคล (เช่น อายุ, เพศ, น้ำหนัก, ส่วนสูง, โรคประจำตัว) ให้สกัดใส่ใน user_profile_update ทันที
- หากผู้ใช้บอกข้อมูลสุขภาพประจำวัน (นอน, ออกกำลังกาย, ความเครียด, น้ำ, อารมณ์) ให้สกัดใส่ฟิลด์หลัก (sleep_hours, exercise_minutes ฯลฯ)
- หากว่ายังไม่ได้กรอกข้อมูลสุขภาพประจำวันให้ลองพยายามถามข้อมูลสุขภาพประจำวัน (นอน, ออกกำลังกาย, ความเครียด)
- นำข้อมูลโปรไฟล์มาปรับคำแนะนำสุขภาพให้เหมาะสมกับสรีระของผู้ใช้
- ตอบสั้น กระชับ เป็นมิตร ใช้ภาษาเดียวกับผู้ใช้ (ภาษาไทยลงท้ายด้วย "ค่ะ")
- ให้คำแนะนำสุขภาพทั่วไป ห้ามวินิจฉัยโรค
- ห้ามสร้างข้อมูลที่ผู้ใช้ไม่ได้บอก (ถ้าไม่มีให้ใช้ null)
- sleep_hours = 0-24, exercise_minutes = 0-1440, stress_level = 1-10
{asking_rule}
- ห้ามใช้ Markdown ในส่วน reply_text
"""

    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=dynamic_system_prompt,
                response_mime_type="application/json",
                max_output_tokens=350
            )
        )
        text = response.text.strip()
        print(f"Gemini response: {text}")
        return json.loads(text)
    except Exception as e:
        print(f"Gemini {MODEL_NAME} error: {e}")
        return None


# =========================================================
# WEEKLY SUMMARY
# =========================================================

def generate_weekly_summary(user_id: str) -> str:
    if not supabase:
        return "ตอนนี้ระบบฐานข้อมูลยังไม่พร้อมใช้งานค่ะ"

    if not ai_client:
        return "ตอนนี้ระบบ AI ยังไม่พร้อมใช้งานค่ะ"

    seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()

    try:
        response = (
            supabase
            .table("health_logs")
            .select("created_at,sleep_hours,exercise_minutes,stress_level,notes")
            .eq("user_id", user_id)
            .gte("created_at", seven_days_ago)
            .order("created_at", desc=True)
            .execute()
        )

        logs = response.data

        if not logs:
            return (
                "ยังไม่มีข้อมูลสุขภาพย้อนหลัง 7 วันค่ะ\n"
                "ลองบอกหมอ DAI เรื่องการนอน การออกกำลังกาย หรือความเครียด ได้เลยนะคะ"
            )

        logs = logs[:30]

        compact_logs = []
        for log in logs:
            compact_logs.append({
                "date": log.get("created_at"),
                "sleep": log.get("sleep_hours"),
                "exercise": log.get("exercise_minutes"),
                "stress": log.get("stress_level"),
                "notes": log.get("notes")
            })

        prompt = (
            "สรุปสุขภาพ 7 วันจากข้อมูลนี้ ให้สั้น อ่านง่าย และให้คำแนะนำทั่วไป 2-3 ข้อ ห้ามวินิจฉัยโรค:\n"
            + json.dumps(compact_logs, ensure_ascii=False, separators=(",", ":"))
        )

        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=300
            )
        )

        summary = response.text.strip()

        if len(summary) > 4500:
            summary = summary[:4500]

        return summary

    except Exception as e:
        print(f"Weekly summary error: {e}")
        return "เกิดข้อผิดพลาดในการสร้างสรุปสุขภาพค่ะ"


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
@app.get("/callback")
@app.get("/webhook")
async def health_check():
    return {
        "status": "ok",
        "message": "หมอ DAI Server is running!"
    }


# =========================================================
# LINE WEBHOOK
# =========================================================

@app.post("/callback")
@app.post("/webhook")
async def callback(
    request: Request,
    x_line_signature: str = Header(None)
):
    if not x_line_signature:
        raise HTTPException(
            status_code=400,
            detail="Missing x-line-signature header"
        )

    body = await request.body()

    try:
        handler.handle(
            body.decode("utf-8"),
            x_line_signature
        )
    except InvalidSignatureError:
        raise HTTPException(
            status_code=400,
            detail="Invalid signature"
        )
    except Exception as e:
        print(f"Webhook error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Webhook error"
        )

    return "OK"


# =========================================================
# HANDLE TEXT MESSAGE
# =========================================================

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    ai_reply = None

    # =====================================================
    # WEEKLY SUMMARY COMMAND
    # =====================================================
    if "สรุปสัปดาห์" in user_message or "สรุปรายสัปดาห์" in user_message:
        ai_reply = generate_weekly_summary(user_id)

    # =====================================================
    # NORMAL MESSAGE
    # =====================================================
    else:
        # 1. เช็คข้อมูลประจำวันของวันนี้
        has_logged_today = check_today_log_exists(user_id)
        
        # 2. ดึงข้อมูลโปรไฟล์ผู้ใช้
        user_profile = get_user_profile(user_id)

        # 3. ให้ Gemini วิเคราะห์ข้อความ
        result = ask_gemini(user_message, has_logged_today, user_profile)

        if result:
            # 4.1 บันทึก Log สุขภาพประจำวัน (ลงตาราง health_logs)
            save_health_log(user_id, result)

            # 4.2 บันทึก/อัปเดต โปรไฟล์ส่วนบุคคล (ลงตาราง users)
            if "user_profile_update" in result and result["user_profile_update"]:
                save_user_profile(user_id, result["user_profile_update"])

            ai_reply = result.get("reply_text")

    # =====================================================
    # FALLBACK
    # =====================================================
    if not ai_reply:
        ai_reply = "ขออภัยค่ะ ตอนนี้หมอ DAI ประมวลผลไม่สำเร็จ ลองส่งข้อความใหม่อีกครั้งนะคะ"

    # =====================================================
    # LINE MESSAGE LIMIT
    # =====================================================
    if len(ai_reply) > 4900:
        ai_reply = ai_reply[:4900]

    # =====================================================
    # SEND REPLY
    # =====================================================
    try:
        with ApiClient(line_config) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=ai_reply)]
                )
            )
    except Exception as e:
        print(f"LINE reply error: {e}")
