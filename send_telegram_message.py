import os
import requests
import sys

def send_telegram_message(message):
    telegram_token = os.getenv('TELEGRAM_TOKEN')
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')

    if not telegram_token or not telegram_chat_id:
        print("Error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set in environment variables.")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {
        'chat_id': telegram_chat_id,
        'text': message
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()  # Raises for 4xx / 5xx HTTP responses.
        print("Message sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram message: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        message_to_send = sys.argv[1]
        send_telegram_message(message_to_send)
    else:
        print("Uso: python send_telegram_message.py \"Sua mensagem aqui\"")
        sys.exit(1)
