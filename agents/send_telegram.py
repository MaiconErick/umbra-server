#!/usr/bin/env python3
import os, sys, urllib.parse, urllib.request

BASE=os.path.expanduser('/home/pi/MaiconErick/Umbra')
ENV_FILE=os.path.join(BASE, '.env')

def load_env():
    env={}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r') as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k,v=line.split('=',1)
                    env[k.strip()]=v.strip().strip('"').strip("'")
    for k in ('TELEGRAM_TOKEN','TELEGRAM_CHAT_ID'):
        if k in os.environ:
            env[k]=os.environ[k]
    return env


def send_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
    req = urllib.request.Request(url, data)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def main():
    env = load_env()
    token = env.get('TELEGRAM_TOKEN')
    chat_id = env.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print('Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID', file=sys.stderr)
        sys.exit(2)
    text = ' '.join(sys.argv[1:]) or 'Teste: mensagem em 1 minuto'
    try:
        resp = send_message(token, chat_id, text)
        print('OK', resp)
    except Exception as e:
        print('ERROR', e, file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
