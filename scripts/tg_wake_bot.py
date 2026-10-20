#!/usr/bin/env python3
"""Lightweight Telegram bot to send Wake-on-LAN to 'meu pc'.
Listens for /liga_meu_pc or /liga from authorized chat and runs the saved wake script.
"""
import os, urllib.request, urllib.parse, json, time, subprocess, sys

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ALLOWED_CHAT = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
WAKE_SCRIPT = "/home/pi/MaiconErick/Umbra/scripts/wake_meu_pc.py"
API = f"https://api.telegram.org/bot{TOKEN}"

def get_updates(offset=None, timeout=20):
    url = API + '/getUpdates'
    params = {}
    if offset:
        params['offset'] = offset
    if params:
        url += '?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.load(resp)
    except Exception as e:
        print('get_updates error', e, file=sys.stderr)
        return None

def send_message(chat_id, text):
    url = API + '/sendMessage'
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
    try:
        with urllib.request.urlopen(url, data, timeout=10) as resp:
            return json.load(resp)
    except Exception as e:
        print('send_message error', e, file=sys.stderr)
        return None


def handle_update(update):
    if 'message' not in update:
        return
    msg = update['message']
    chat = msg.get('chat', {})
    chat_id = chat.get('id')
    text = msg.get('text','').strip()
    if chat_id != ALLOWED_CHAT:
        return
    if text in ['/liga_meu_pc', '/liga']:
        send_message(chat_id, 'Recebido. Enviando magic packet...')
        try:
            res = subprocess.run([WAKE_SCRIPT], capture_output=True, text=True, timeout=10)
            out = res.stdout.strip() + ('; ' + res.stderr.strip() if res.stderr.strip() else '')
            send_message(chat_id, 'Feito: ' + (out or 'magic packet enviado'))
        except Exception as e:
            send_message(chat_id, 'Erro ao executar script: ' + str(e))


def main():
    print('tg_wake_bot starting')
    offset = None
    while True:
        data = get_updates(offset)
        if not data or not data.get('ok'):
            time.sleep(3)
            continue
        for upd in data.get('result', []):
            offset = (upd['update_id'] + 1)
            try:
                handle_update(upd)
            except Exception as e:
                print('handle_update error', e, file=sys.stderr)
        time.sleep(1)

if __name__ == '__main__':
    main()
