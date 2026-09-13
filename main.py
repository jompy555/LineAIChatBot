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

# Line & Gemini Init
line_config = Configuration(access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
ai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# Supabase Init
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

MODELS_TO_TRY = ['gemini-3.6-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite']

SYSTEM_PROMPT = """
คุณคือ CareBot โค้ชผู้ช่วยดูแลสุขภาพส่วนบุคคล
- คอยถามไถ่และเก็บข้อมูลการนอน การออกกำลังกาย ความเครียด
- ให้คำแนะนำอย่างนุ่มนวล เป็นกันเอง สั้นกระชับ อ่านง่ายบนมือถือ
"""

def extract_and_save_health_data(user_id: str, text: str):
    """ใช้ Gemini แปลงข้อความผู้ใช้เป็น JSON แล้วบันทึกลง Supabase"""
    prompt = f"""
    วิเคราะห์ข้อความต่อไปนี้แล้วดึงข้อมูลสุขภาพออกมาในรูปแบบ JSON เท่านั้น (ถ้าไม่มีข้อมูลให้ใช้ null):
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
            print(f"Saved health log for {user_id}: {data}")
    except Exception as e:
        print(f"Error extracting/saving health log: {e}")

def generate_weekly_summary(user_id: str) -> str:
    """ดึงข้อมูล 7 วันย้อนหลังแล้วให้ Gemini สรุปผล"""
    seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
    
    # ดึงข้อมูลจาก Supabase
    response = supabase.table('health_logs') \
        .select('*') \
        .eq('user_id', user_id) \
        .gte('created_at', seven_days_ago) \
        .execute()
    
    logs = response.data
    if not logs:
        return "ยังไม่มีข้อมูลสุขภาพย้อนหลังในสัปดาห์นี้ครับ ลองพิมพ์บอกเล่าการนอนหรือออกกำลังกายกับ CareBot ก่อนได้เลย!"

    # ส่งประวัติให้ Gemini ทำรายงานสรุป
    prompt = f"""
    สรุปรายงานสุขภาพรายสัปดาห์จากข้อมูล JSON ต่อไปนี้ให้อ่านง่าย มีกำลังใจ และสั้นกระชับสำหรับส่งใน LINE:
    {json.dumps(logs, ensure_ascii=False)}
    """
    res = ai_client.models.generate_content(model='gemini-3.6-flash', contents=prompt)
    return res.text

@app.get("/")
@app.get("/callback")
async def health_check():
    return {"status": "ok"}

@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing signature")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    ai_reply = None

    # คำสั่งพิเศษ: ขอสรุปรายสัปดาห์
    if "สรุปสัปดาห์" in user_message or "สรุปรายสัปดาห์" in user_message:
        ai_reply = generate_weekly_summary(user_id)
    else:
        # แอบบันทึกข้อมูลสุขภาพเข้า DB แบบเบื้องหลัง
        extract_and_save_health_data(user_id, user_message)

        # ตอบกลับแชทปกติ
        for model_name in MODELS_TO_TRY:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=user_message,
                    config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
                )
                ai_reply = response.text
                break
            except Exception as e:
                continue

    if not ai_reply:
        ai_reply = "ขออภัยด้วยครับ ไม่สามารถประมวลผลได้ในขณะนี้"

    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                replyToken=event.replyToken,
                messages=[TextMessage(text=ai_reply)]
            )
        )
