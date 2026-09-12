import os
from flask import Flask, request
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from google import genai

app = Flask(__name__)

@app.route("/", methods=['GET'])
@app.route("/webhook", methods=['GET'])
def health_check():
    return 'Server is running!', 200

@app.route("/webhook", methods=['POST'])
def webhook():
    line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if not line_token or not gemini_key:
        return 'Missing API Keys', 500

    body = request.get_json()
    
    try:
        events = body.get('events', [])
        if not events:
            return 'OK', 200
            
        event = events[0]
        if event.get('type') == 'message' and event['message']['type'] == 'text':
            user_text = event['message']['text']
            reply_token = event['replyToken']

            # ส่งหา Gemini AI
            gemini_client = genai.Client(api_key=gemini_key)
            response = gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=user_text
            )
            ai_answer = response.text

            # ตอบกลับ LINE
            configuration = Configuration(access_token=line_token)
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        replyToken=reply_token,
                        messages=[TextMessage(text=ai_answer)]
                    )
                )
    except Exception as e:
        print(f"Error: {e}")
        
    return 'OK', 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
