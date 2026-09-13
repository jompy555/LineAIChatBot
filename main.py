import os
import json
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Header, HTTPException
from dotenv import load_dotenv

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from google import genai
from google.genai import types
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET if LINE_CHANNEL_SECRET else "dummy_secret")
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# อัปเดตรายชื่อโมเดลล่าสุด
MODELS_TO_TRY = ['gemini-3.6-flash', 'gemini-3.5-flash-lite']

SYSTEM_PROMPT = """
คุณคือ "CareBot" โค้ชผู้ช่วยดูแลสุขภาพส่วนบุคคลที่เป็นมิตร

วิเคราะห์ข้อความของผู้ใช้ แล้วตอบกลับในรูปแบบ JSON เท่านั้น โดยมีคีย์ดังนี้:
{
    "reply_text": "ข้อความตอบกลับผู้ใช้แบบเป็นกันเอง สั้นกระชับ ลงท้ายด้วยครับ",
    "sleep_hours": float หรือ null,
    "exercise_minutes": int หรือ null,
    "stress_level": int (1-10) หรือ null,
    "notes": "สรุปสิ่งบันทึกอื่นๆ" หรือ null
}
"""

def generate_weekly_summary(user_id: str) -> str:
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

        prompt = f"สรุปรายงานสุขภาพรายสัปดาห์จากข้อมูล JSON นี้ให้อ่านง่ายและมีกำลังใจ: {json.dumps(logs, ensure_ascii=False)}"
        res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=prompt)
        return res.text
    except Exception as e:
        print(f"Error generating summary: {e}")
        return "เกิดข้อผิดพลาดในการดึงข้อมูลสรุปรายสัปดาห์ครับ"

@app.get("/")
@app.get("/callback")
@app.get("/webhook")
async def health_check():
    return {"status": "ok", "message": "CareBot Server is running!"}

@app.post("/callback")
@app.post("/webhook")
async def callback(request: Request, x_line_signature: str = Header(None)):
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
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    ai_reply = None

    if "สรุปสัปดาห์" in user_message or "สรุปรายสัปดาห์" in user_message:
        ai_reply = generate_weekly_summary(user_id)
    else:
        # ยิง Gemini เพียง 1 ครั้งเพื่อดึงทั้งคำตอบและข้อมูลสุขภาพ
        for model_name in MODELS_TO_TRY:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=user_message,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json"
                    )
                )
                res_data = json.loads(response.text)
                ai_reply = res_data.get("reply_text")

                # บันทึกลง Supabase หากมีข้อมูลสุขภาพ
                if supabase and any([res_data.get('sleep_hours'), res_data.get('exercise_minutes'), res_data.get('stress_level')]):
                    log_data = {
                        "user_id": user_id,
                        "sleep_hours": res_data.get('sleep_hours'),
                        "exercise_minutes": res_data.get('exercise_minutes'),
                        "stress_level": res_data.get('stress_level'),
                        "notes": res_data.get('notes')
                    }
                    supabase.table('health_logs').insert(log_data).execute()
                    print(f"Saved log for {user_id}")
                break
            except Exception as e:
                print(f"Model {model_name} error: {e}")
                continue

    if not ai_reply:
        ai_reply = "ขออภัยด้วยครับ ขณะนี้ระบบประมวลผลติดขัดหรือโควตาเต็ม กรุณาลองใหม่อีกครั้ง"

    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=ai_reply)]
            )
        )
