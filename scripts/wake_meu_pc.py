#!/usr/bin/env python3
"""
Send Wake-on-LAN magic packet to user's PC (meu pc).
Saved by Umbra so next "Liga meu pc" triggers this.
"""
import socket, re
MAC = '74:56:3c:f0:ff:0d'
mac = re.sub('[^0-9a-fA-F]', '', MAC)
packet = bytes.fromhex('ff'*6 + mac*16)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(packet, ('255.255.255.255', 9))
print('magic packet sent to', MAC)
