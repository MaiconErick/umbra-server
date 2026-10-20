#!/usr/bin/env python3
"""
Watcher atualizado: envia lista numerada de novos MACs e aceita respostas no formato:
1 - Meu PC
2 - Tv da sala

Fluxo:
- roda sudo nmap -sn na subrede detectada
- compara MACs com scan_by_mac.csv (chave MAC)
- se houver novos MACs, envia mensagem com lista numerada (número -> MAC / IP / vendor)
- aguarda respostas via getUpdates e processa linhas com formato: <num> - <nome>
- mapeia número para MAC e salva no CSV (anexa ou atualiza)
- confirma via Telegram

Arquivo: /home/pi/MaiconErick/Umbra/scripts/scan_watch.py
"""
import os
import csv
import json
import time
import subprocess
from pathlib import Path
import re
import requests

BASE=Path('/home/pi/MaiconErick/Umbra')
SR=BASE/'scan_results'
CSV=SR/'scan_by_mac.csv'
CACHE=SR/'mac_vendor_cache.json'
TMP_NMAP='/tmp/nmap_watch.txt'

TG_TOKEN=os.environ.get('TELEGRAM_TOKEN')
TG_CHAT=os.environ.get('TELEGRAM_CHAT_ID')
if not TG_TOKEN or not TG_CHAT:
    print('Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID')
    raise SystemExit(1)

# helpers
def tg_send_message(text):
    url=f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    payload={'chat_id':TG_CHAT,'text':text}
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print('tg send failed', e)
        return None

def tg_get_updates(offset=None):
    url=f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates'
    params={}
    if offset is not None:
        params['offset']=offset
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print('tg getUpdates failed', e)
        return {'ok':False,'result':[]}

# read known MACs
known = {}
if CSV.exists():
    with CSV.open() as f:
        r=csv.DictReader(f)
        for row in r:
            mac=(row.get('MAC') or '').upper()
            if mac:
                known[mac]=row

# detect subnet
try:
    ip_out=subprocess.check_output(['hostname','-I'],text=True).strip()
    ip=ip_out.split()[0]
    subnet='.'.join(ip.split('.')[:3])+'.0/24'
except Exception:
    subnet='192.168.1.0/24'

# run nmap
try:
    subprocess.run(['sudo','nmap','-sn','-n',subnet,'-oN',TMP_NMAP], check=True)
except Exception as e:
    print('nmap failed', e)
    raise SystemExit(1)

text = Path(TMP_NMAP).read_text()
# parse pairs of (mac,ip,vendor)
entries = []
cur_ip=None
cur_name=None
for line in text.splitlines():
    if line.startswith('Nmap scan report for'):
        cur_ip=line.split()[-1]
    if 'MAC Address:' in line:
        part=line.split('MAC Address:')[1].strip()
        mac=part.split()[0].upper()
        vendor=' '.join(part.split()[1:]).strip('()') if len(part.split())>1 else ''
        entries.append({'MAC':mac,'IP':cur_ip or '','VENDOR':vendor})

# find new MACs
new = [e for e in entries if e['MAC'] and e['MAC'] not in known]
if not new:
    # nothing to do
    exit(0)

# load vendor cache if available
vendor_cache={}
if CACHE.exists():
    try:
        vendor_cache=json.loads(CACHE.read_text())
    except Exception:
        vendor_cache={}

# build message list with numbers
lines=[]
for i,e in enumerate(new, start=1):
    mac=e['MAC']
    ip=e.get('IP','')
    vendor=e.get('VENDOR') or vendor_cache.get(mac,'')
    lines.append(f"{i} - {mac} - {ip} - {vendor}")
msg = "Novos dispositivos detectados:\n" + "\n".join(lines) + "\n\nResponda com linhas no formato:\n1 - Meu PC\n2 - Tv da sala\n(um por linha)."
resp = tg_send_message(msg)

# get current update_id to avoid old messages
updates = tg_get_updates()
last_update_id = None
if updates.get('ok'):
    for u in updates.get('result',[]):
        last_update_id = max(last_update_id or u['update_id'], u['update_id'])

# poll for replies (timeout 10 minutes)
end_time = time.time() + 600
processed_updates = set()
while time.time() < end_time:
    updates = tg_get_updates(offset=(last_update_id+1) if last_update_id is not None else None)
    if updates.get('ok'):
        for u in updates.get('result', []):
            uid = u['update_id']
            if uid in processed_updates:
                continue
            processed_updates.add(uid)
            last_update_id = max(last_update_id or uid, uid)
            msg_obj = u.get('message') or u.get('edited_message')
            if not msg_obj:
                continue
            chat = msg_obj.get('chat',{})
            if str(chat.get('id')) != str(TG_CHAT):
                continue
            text = msg_obj.get('text','')
            # parse each line
            for line in text.splitlines():
                m = re.match(r"^\s*(\d+)\s*[-:]+\s*(.+)$", line)
                if not m:
                    m = re.match(r"^\s*(\d+)\s+(.+)$", line)
                if not m:
                    continue
                idx = int(m.group(1))
                name = m.group(2).strip()
                if 1 <= idx <= len(new):
                    mac = new[idx-1]['MAC']
                    ip = new[idx-1].get('IP','')
                    vendor = new[idx-1].get('VENDOR') or vendor_cache.get(mac,'')
                    # prepare row
                    row = {
                        'MAC': mac,
                        'NAME': name,
                        'IP': ip,
                        'HOSTNAME': '',
                        'VENDOR_IEEE': '',
                        'VENDOR_ONLINE': vendor or ''
                    }
                    # append or update CSV
                    # ensure CSV exists with header
                    write_header = not CSV.exists()
                    if write_header:
                        CSV.parent.mkdir(parents=True, exist_ok=True)
                        with CSV.open('w', newline='') as f:
                            writer = csv.DictWriter(f, fieldnames=['MAC','NAME','IP','HOSTNAME','VENDOR_IEEE','VENDOR_ONLINE'])
                            writer.writeheader()
                            writer.writerow(row)
                    else:
                        # read all rows and update if exists
                        modified=False
                        rows=[]
                        with CSV.open() as f:
                            reader=csv.DictReader(f)
                            for r in reader:
                                rows.append(r)
                        for r in rows:
                            if (r.get('MAC') or '').upper() == mac:
                                r.update(row)
                                modified=True
                                break
                        if not modified:
                            rows.append(row)
                        with CSV.open('w', newline='') as f:
                            writer=csv.DictWriter(f, fieldnames=['MAC','NAME','IP','HOSTNAME','VENDOR_IEEE','VENDOR_ONLINE'])
                            writer.writeheader()
                            for r in rows:
                                writer.writerow(r)
                    tg_send_message(f'Saved: {mac} -> {name}')
    time.sleep(3)

# finished polling
exit(0)
