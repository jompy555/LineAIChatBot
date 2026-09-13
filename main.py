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
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from google import genai
from google.genai import types

from supabase import create_client, Client


# =========================================================
# ENV
# =========================================================

load_dotenv()

app = FastAPI()

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
)

LINE_CHANNEL_SECRET = os.environ.get(
    "LINE_CHANNEL_SECRET", ""
)

GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY", ""
)

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", ""
)

SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY", ""
)


# =========================================================
# CLIENTS
# =========================================================

line_config = Configuration(
    access_token=LINE_CHANNEL_ACCESS_TOKEN
)

handler = WebhookHandler(
    LINE_CHANNEL_SECRET if LINE_CHANNEL_SECRET else "dummy_secret"
)

ai_client = (
    genai.Client(api_key=GEMINI_API_KEY)
    if GEMINI_API_KEY
    else None
)

supabase: Client = None

if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )


# =========================================================
# AI MODELS
# =========================================================

# ตัวหลัก = ประหยัดที่สุดสำหรับงานทั่วไป
MODEL_PRIMARY = "gemini-2.5-flash-lite"

# ถ้าตัวหลักมีปัญหา ค่อยใช้ตัวนี้
MODEL_FALLBACK = "gemini-3.1-flash-lite"


# =========================================================
# SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = """
คุณคือ "หมอ DAI" ผู้ช่วยติดตามสุขภาพและพฤติกรรมการใช้ชีวิต

ตอบเป็น JSON เท่านั้น:
{
  "reply_text": "ข้อความตอบผู้ใช้",
  "sleep_hours": null,
  "exercise_minutes": null,
  "stress_level": null,
  "mood": null,
  "water_ml": null,
  "notes": null
}

กฎ:
- ตอบสั้น กระชับ เป็นมิตร
- ใช้ภาษาเดียวกับผู้ใช้
- ภาษาไทยลงท้ายด้วย "ค่ะ"
- ให้คำแนะนำสุขภาพทั่วไป ห้ามวินิจฉัยโรค
- ห้ามสร้างข้อมูลที่ผู้ใช้ไม่ได้บอก
- คำถามทั่วไปไม่ต้องบันทึกข้อมูล
- sleep_hours = ชั่วโมงนอน 0-24
- exercise_minutes = นาทีออกกำลังกาย 0-1440
- stress_level = 1-10
- water_ml = ปริมาณน้ำเป็น ml
- mood = อารมณ์
- notes = ข้อมูลสุขภาพอื่นที่สำคัญ
- ถ้าไม่มีข้อมูลให้ใช้ null
- ห้ามใช้ Markdown
"""


# =========================================================
# HEALTH DATA VALIDATION
# =========================================================

def clean_health_data(data: dict) -> dict:
    """
    ตรวจสอบข้อมูลจาก AI ก่อนบันทึกลง Database
    """

    result = {
        "sleep_hours": None,
        "exercise_minutes": None,
        "stress_level": None,
        "mood": None,
        "water_ml": None,
        "notes": None
    }

    # -------------------------
    # sleep
    # -------------------------

    sleep = data.get("sleep_hours")

    if sleep is not None:
        try:
            sleep = float(sleep)

            if 0 <= sleep <= 24:
                result["sleep_hours"] = sleep

        except (ValueError, TypeError):
            pass

    # -------------------------
    # exercise
    # -------------------------

    exercise = data.get("exercise_minutes")

    if exercise is not None:
        try:
            exercise = int(exercise)

            if 0 <= exercise <= 1440:
                result["exercise_minutes"] = exercise

        except (ValueError, TypeError):
            pass

    # -------------------------
    # stress
    # -------------------------

    stress = data.get("stress_level")

    if stress is not None:
        try:
            stress = int(stress)

            if 1 <= stress <= 10:
                result["stress_level"] = stress

        except (ValueError, TypeError):
            pass

    # -------------------------
    # mood
    # -------------------------

    mood = data.get("mood")

    if mood:
        result["mood"] = str(mood)[:200]

    # -------------------------
    # water
    # -------------------------

    water = data.get("water_ml")

    if water is not None:
        try:
            water = int(water)

            if 0 <= water <= 20000:
                result["water_ml"] = water

        except (ValueError, TypeError):
            pass

    # -------------------------
    # notes
    # -------------------------

    notes = data.get("notes")

    if notes:
        result["notes"] = str(notes)[:2000]

    return result


# =========================================================
# CHECK WHETHER HEALTH DATA EXISTS
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

    return any(
        data.get(field) is not None
        for field in fields
    )


# =========================================================
# SAVE HEALTH LOG
# =========================================================

def save_health_log(user_id: str, data: dict):

    if not supabase:
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

    # -----------------------------------------------------
    # สำคัญ
    #
    # ตอนนี้ตั้งใจเก็บเฉพาะ field ที่โค้ดเดิมของนายท่าน
    # ใช้อยู่ เพื่อไม่บังคับให้แก้ Supabase schema
    #
    # mood / water_ml ยังไม่ถูก insert
    # จนกว่าจะยืนยันว่า column มีอยู่จริง
    # -----------------------------------------------------

    try:

        supabase \
            .table("health_logs") \
            .insert(log_data) \
            .execute()

        print(f"Saved health log: {user_id}")

        return True

    except Exception as e:

        print(f"Supabase save error: {e}")

        return False


# =========================================================
# ASK GEMINI
# =========================================================

def ask_gemini(user_message: str):

    if not ai_client:
        return None

    models = [
        MODEL_PRIMARY,
        MODEL_FALLBACK
    ]

    for model_name in models:

        try:

            response = ai_client.models.generate_content(

                model=model_name,

                contents=user_message,

                config=types.GenerateContentConfig(

                    system_instruction=SYSTEM_PROMPT,

                    response_mime_type="application/json",

                    # จำกัด output
                    max_output_tokens=250
                )
            )

            text = response.text.strip()

            data = json.loads(text)

            return data

        except Exception as e:

            print(
                f"Gemini {model_name} error: {e}"
            )

            continue

    return None


# =========================================================
# WEEKLY SUMMARY
# =========================================================

def generate_weekly_summary(user_id: str) -> str:

    if not supabase or not ai_client:

        return (
            "ตอนนี้ระบบสรุปข้อมูลยังไม่พร้อมใช้งานค่ะ"
        )

    seven_days_ago = (
        datetime.now() - timedelta(days=7)
    ).isoformat()

    try:

        # -------------------------------------------------
        # ดึงเฉพาะข้อมูลที่จำเป็น
        # ไม่ใช้ select("*")
        # -------------------------------------------------

        response = (
            supabase
            .table("health_logs")
            .select(
                "created_at,"
                "sleep_hours,"
                "exercise_minutes,"
                "stress_level,"
                "notes"
            )
            .eq("user_id", user_id)
            .gte("created_at", seven_days_ago)
            .execute()
        )

        logs = response.data

        if not logs:

            return (
                "ยังไม่มีข้อมูลสุขภาพย้อนหลัง 7 วันค่ะ "
                "ลองบอกหมอ DAI เรื่องการนอน "
                "การออกกำลังกาย หรือความเครียดได้เลยนะคะ"
            )

        # -------------------------------------------------
        # จำกัดจำนวนข้อมูล
        # -------------------------------------------------

        logs = logs[-30:]

        compact_logs = []

        for log in logs:

            compact_logs.append({
                "date": log.get("created_at"),
                "sleep": log.get("sleep_hours"),
                "exercise": log.get("exercise_minutes"),
                "stress": log.get("stress_level"),
                "notes": log.get("notes")
            })

        # -------------------------------------------------
        # Prompt สั้นมาก
        # -------------------------------------------------

        prompt = (
            "สรุปสุขภาพ 7 วันจากข้อมูลนี้ "
            "ให้สั้น อ่านง่าย และให้คำแนะนำทั่วไป 2-3 ข้อ "
            "ห้ามวินิจฉัยโรค:\n"
            + json.dumps(
                compact_logs,
                ensure_ascii=False,
                separators=(",", ":")
            )
        )

        response = ai_client.models.generate_content(

            model=MODEL_PRIMARY,

            contents=prompt,

            config=types.GenerateContentConfig(

                max_output_tokens=300
            )
        )

        summary = response.text.strip()

        # -------------------------------------------------
        # ป้องกัน LINE message ยาวเกิน
        # -------------------------------------------------

        if len(summary) > 4500:

            summary = summary[:4500]

        return summary

    except Exception as e:

        print(
            f"Weekly summary error: {e}"
        )

        return (
            "เกิดข้อผิดพลาดในการสร้างสรุปสุขภาพค่ะ"
        )


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

        print(
            f"Webhook error: {e}"
        )

        raise HTTPException(
            status_code=500,
            detail="Webhook error"
        )

    return "OK"


# =========================================================
# HANDLE TEXT MESSAGE
# =========================================================

@handler.add(
    MessageEvent,
    message=TextMessageContent
)
def handle_text_message(event):

    user_id = event.source.user_id

    user_message = (
        event.message.text.strip()
    )

    ai_reply = None

    # =====================================================
    # WEEKLY SUMMARY
    # =====================================================

    if (
        "สรุปสัปดาห์" in user_message
        or
        "สรุปรายสัปดาห์" in user_message
    ):

        ai_reply = generate_weekly_summary(
            user_id
        )

    # =====================================================
    # NORMAL CHAT
    # =====================================================

    else:

        result = ask_gemini(
            user_message
        )

        if result:

            # ---------------------------------------------
            # ทำความสะอาดข้อมูล
            # ---------------------------------------------

            cleaned = clean_health_data(
                result
            )

            # ---------------------------------------------
            # บันทึกเฉพาะเมื่อมีข้อมูลสุขภาพ
            # ---------------------------------------------

            if contains_health_data(cleaned):

                save_health_log(
                    user_id,
                    cleaned
                )

            # ---------------------------------------------
            # ข้อความตอบกลับ
            # ---------------------------------------------

            ai_reply = result.get(
                "reply_text"
            )

    # =====================================================
    # FALLBACK MESSAGE
    # =====================================================

    if not ai_reply:

        ai_reply = (
            "ขออภัยค่ะ ตอนนี้หมอ DAI "
            "ประมวลผลไม่สำเร็จ ลองส่งข้อความใหม่อีกครั้งนะคะ"
        )

    # =====================================================
    # LINE LIMIT
    # =====================================================

    if len(ai_reply) > 4900:

        ai_reply = ai_reply[:4900]

    # =====================================================
    # SEND LINE MESSAGE
    # =====================================================

    try:

        with ApiClient(line_config) as api_client:

            line_bot_api = MessagingApi(
                api_client
            )

            line_bot_api.reply_message(

                ReplyMessageRequest(

                    reply_token=event.reply_token,

                    messages=[
                        TextMessage(
                            text=ai_reply
                        )
                    ]
                )
            )

    except Exception as e:

        print(
            f"LINE reply error: {e}"
        )
