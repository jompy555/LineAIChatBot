import os
import json
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Header, HTTPException
from dotenv import load_dotenv

# Official LINE SDK v3 imports
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI import
from google import genai
from google.genai import types

# Supabase import
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

# ดึงค่า Environment Variables พร้อมใส่ค่า fallback ป้องกัน Server Crash
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Initialize Clients
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET if LINE_CHANNEL_SECRET else "dummy_secret")
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# รายการโมเดลสำหรับสำรองอัตโนมัติ
MODELS_TO_TRY = ['gemini-3.6-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite']

# คำสั่งกำหนดบทบาท CareBot
SYSTEM_PROMPT = """
คุณคือ "CareBot" โค้ชผู้ช่วยดูแลสุขภาพส่วนบุคคลที่เป็นมิตร ใส่ใจ และให้กำลังใจเสมอ

หน้าที่ของคุณ:
1. คอยถามไถ่และเก็บข้อมูลสุขภาพประจำวันของผู้ใช้ เช่น ระยะเวลาการนอน, การออกกำลังกาย, และระดับความเครียด
2. ให้คำแนะนำด้านสุขภาพที่เหมาะสม ปลอดภัย อ่านง่าย และเหมาะกับการอ่านบน LINE (ลงท้ายด้วย 'ครับ' อย่างสุภาพ)
3. ตอบคำถามสั้น กระชับ แบ่งบรรทัดให้อ่านง่าย
"""

def extract_and_save_health_data(user_id: str, text: str):
    """ใช้ Gemini แปลงข้อความผู้ใช้เป็น JSON แล้วบันทึกลง Supabase"""
    if not supabase or not ai_client:
        return

    prompt = f"""
    วิเคราะห์ข้อความต่อไปนี้แล้วดึงข้อมูลสุขภาพออกมาในรูปแบบ JSON เท่านั้น (ถ้าไม่มีข้อมูลในหัวข้อไหนให้ใช้ null):
    ข้อความ: "{text}"

    รูปแบบ JSON ที่ต้องการ:
    {{
        "sleep_hours": float หรือ null,
        "exercise_minutes": int หรือ null,
        "stress_level": int (1-10) หรือ null,
        "notes": "สรุปอาการหรือสิ่งบันทึกอื่นๆ" หรือ null
    }}
    """
    try:
        res = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        data = json.loads(res.text)
        
        # บันทึกเมื่อมีข้อมูลสุขภาพอย่างน้อย 1 รายการ
        if any([data.get('sleep_hours'), data.get('exercise_minutes'), data.get('stress_level')]):
            data['user_id'] = user_id
            supabase.table('health_logs').insert(data).execute()
            print(f"Successfully saved health log for {user_id}")
    except Exception as e:
        print(f"Error extracting/saving health log: {e}")

def generate_weekly_summary(user_id: str) -> str:
    """ดึงข้อมูล 7 วันย้อนหลังแล้วให้ Gemini สรุปผล"""
    if not supabase or not ai_client:
        return "ระบบ Database หรือ AI ยังไม่ได้ตั้งค่า API Key ครับ"

    seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
    
    try:
        response = supabase.table('health_logs') \
            .select('*') \
            .eq('user_id', user_id) \
            .gte('created_at', seven_days_ago) \
            .execute()
        
        logs = response.data
        if not logs:
            return "ยังไม่มีข้อมูลสุขภาพย้อนหลังในสัปดาห์นี้ครับ ลองพิมพ์บอกเล่าการนอนหรือออกกำลังกายกับ CareBot ก่อนได้เลย!"

        prompt = f"""
        สรุปรายงานสุขภาพรายสัปดาห์จากข้อมูล JSON ต่อไปนี้ให้อ่านง่าย มีกำลังใจ สรุปประเด็นการนอน การออกกำลังกาย และความเครียด สั้นกระชับสำหรับส่งใน LINE:
        {json.dumps(logs, ensure_ascii=False)}
        """
        res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=prompt)
        return res.text
    except Exception as e:
        print(f"Error generating summary: {e}")
        return "เกิดข้อผิดพลาดในการดึงข้อมูลสรุปรายสัปดาห์ครับ"

@app.get("/")
@app.get("/callback")
async def health_check():
    """Endpoint สำหรับให้ Render หรือเบราว์เซอร์เช็คสถานะการทำงาน"""
    return {"status": "ok", "message": "CareBot Server is running!"}

@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    """รับ Webhook จาก LINE"""
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing x-line-signature header")

    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature verification failed")

    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """ประมวลผลข้อความและตอบกลับ"""
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    ai_reply = None

    # กรณีผู้ใช้พิมพ์คำว่า "สรุปสัปดาห์" หรือ "สรุปรายสัปดาห์"
    if "สรุปสัปดาห์" in user_message or "สรุปรายสัปดาห์" in user_message:
        ai_reply = generate_weekly_summary(user_id)
    else:
        # แอบบันทึกข้อมูลสุขภาพลง Supabase แบบเบื้องหลัง
        extract_and_save_health_data(user_id, user_message)

        # ตอบกลับแชตปกติด้วย Gemini
        for model_name in MODELS_TO_TRY:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT
                    )
                )
                ai_reply = response.text
                break
            except Exception as e:
                print(f"Model {model_name} failed: {e}")
                continue

    if not ai_reply:
        ai_reply = "ขออภัยด้วยครับ ขณะนี้ระบบประมวลผลติดขัด ชั่วคราว กรุณาลองใหม่อีกครั้ง"

    # ส่งข้อความตอบกลับไปยัง LINE
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                replyToken=event.replyToken,
                messages=[TextMessage(text=ai_reply)]
            )
        )
