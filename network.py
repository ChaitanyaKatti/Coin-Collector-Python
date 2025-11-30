import socket
import time
import random

# Framing constants
HEADER_SIZE = 8
MSG_TYPE_SIZE = 3

# Message class IDs
MSG_JOIN_REQUEST = 1
MSG_SERVER_ACCEPT = 2
MSG_PONG = 3
MSG_WORLD_SNAPSHOT = 4
MSG_COMMAND = 5
MSG_PING = 6
MSG_CLOSE = 7
MSG_JOIN_REJECT = 8

def pack_message(msg_type, payload=b''):
    body = f"{msg_type:03}".encode('ascii') + payload
    header = f"{len(body):08}".encode('ascii')
    return header + body

def unpack_message(data):
    if len(data) < HEADER_SIZE + MSG_TYPE_SIZE:
        return None, None
    try:
        body_size = int(data[:HEADER_SIZE].decode('ascii'))
        body = data[HEADER_SIZE:HEADER_SIZE + body_size]
        msg_type = int(body[:MSG_TYPE_SIZE].decode('ascii'))
        payload = body[MSG_TYPE_SIZE:]
        return msg_type, payload
    except ValueError:
        return None, None

class SimulatedSocket:
    def __init__(self, sock, latency=0.0, jitter=0.0):
        self.sock = sock
        self.latency = latency
        self.jitter = jitter
        self.queue = [] # (time, type, data, addr)

    def sendto(self, data, addr):
        delay = max(0, random.gauss(self.latency, self.jitter))
        self.queue.append((time.time() + delay, 'send', data, addr))

    def update(self):
        """
        Reads from real socket, queues incoming with delay.
        Processes queue: sends ready 'send' packets, returns ready 'recv' packets.
        Returns list of (data, addr) for received packets.
        """
        now = time.time()
        
        # 1. Read from real socket and queue
        try:
            while True:
                data, addr = self.sock.recvfrom(65536)
                delay = max(0, random.gauss(self.latency, self.jitter))
                self.queue.append((now + delay, 'recv', data, addr))
        except BlockingIOError:
            pass
        except Exception:
            pass
            
        # 2. Process queue
        ready_packets = []
        remaining = []
        
        for t, type_, data, addr in self.queue:
            if now >= t:
                if type_ == 'send':
                    try:
                        self.sock.sendto(data, addr)
                    except Exception:
                        pass
                elif type_ == 'recv':
                    ready_packets.append((data, addr))
            else:
                remaining.append((t, type_, data, addr))
        
        self.queue = remaining
        return ready_packets
