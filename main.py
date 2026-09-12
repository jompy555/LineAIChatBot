import os
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

load_dotenv()

app = FastAPI()

# ดึงค่า Environment Variables
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Initialize SDK Connections
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ===================================================
# 📝 SYSTEM PROMPT & CUSTOMIZATIONS
# ===================================================
SYSTEM_PROMPT = """
คุณคือ "เมดสาวผู้ซื่อสัตย์" ที่คอยดูแลและรับใช้ผู้ใช้ 
- ให้เรียกแทนผู้ใช้ว่า "นายท่าน" เสมอ
- แทนสรรพนามตัวเองว่า "ดิฉัน" หรือ "เมด" 
- น้ำเสียงและสไตล์การพูด: สุภาพ นอบน้อม อ่อนโยน อารมณ์ดี และตั้งใจรับฟัง 
- ลักษณะการตอบ: ตอบคำถามให้สั้น กระชับ ตรงประเด็น เหมาะกับการอ่านบนแชท LINE มือถือ
- สามารถใส่อีโมจิน่ารักๆ (เช่น 🙇‍♀️, 💖, ✨, ☕) ประกอบคำตอบได้ตามความเหมาะสม
"""

# ===================================================
# 🔄 MODEL ROTATION LIST (แก้ไขชื่อโมเดลล่าสุด)
# ===================================================
MODELS_TO_TRY = [
    'gemini-3.6-flash',
    'gemini-2.5-flash',
    'gemini-2.5-flash-lite'
]

@app.get("/")
@app.get("/callback")
async def health_check():
    """เปิดทางให้ยิง GET จากเบราว์เซอร์เพื่อเช็คสถานะและปลุก Render Server"""
    return {"status": "ok", "message": "Server is running!"}

@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    """รับ Webhook Data จาก LINE พร้อมตรวจสอบ Digital Signature"""
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="Missing digital signature header")

    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature verification failed")

    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """ประมวลผลข้อความ วนลูปสลับโมเดล AI และตอบกลับ LINE"""
    user_message = event.message.text.strip()
    ai_reply = None

    # วนลูปหาโมเดลที่ใช้งานได้
    for model_name in MODELS_TO_TRY:
        try:
            print(f"Attempting to generate response using: {model_name}")
            response = ai_client.models.generate_content(
                model=model_name,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT
                )
            )
            ai_reply = response.text
            print(f"Success with {model_name}!")
            break
        except Exception as e:
            print(f"Model {model_name} error: {e}. Falling back to next model...")
            continue

    if not ai_reply:
        ai_reply = "ขออภัยด้วยครับ ขณะนี้ระบบ AI ปิดปรับปรุงชั่วคราว หรือโควตาเต็ม กรุณาลองใหม่อีกครั้งในภายหลัง"

    # ส่งคำตอบกลับ LINE
    with ApiClient(line_config) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                replyToken=event.replyToken,
                messages=[TextMessage(text=ai_reply)]
            )
        )
