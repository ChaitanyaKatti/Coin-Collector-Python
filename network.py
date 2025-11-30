import heapq
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
        self.queue = []  # heap of (time, type, data, addr)

    def sendto(self, data, addr):
        delay = random.uniform(max(self.latency - self.jitter, 0),
                                   self.latency + self.jitter)
        heapq.heappush(self.queue, (time.time() + delay, 'send', data, addr))

    def update(self):
        now = time.time()

        # Read from real socket & queue
        try:
            while True:
                data, addr = self.sock.recvfrom(65536)
                delay = random.uniform(max(self.latency - self.jitter, 0),
                                           self.latency + self.jitter)
                heapq.heappush(self.queue, (now + delay, 'recv', data, addr))
        except BlockingIOError:
            pass

        ready_packets = []
        # Process all events whose time has passed
        while self.queue and self.queue[0][0] <= now: # Peek the latest scheduled packet
            t, type_, data, addr = heapq.heappop(self.queue) # Pop the packet
            if type_ == 'send':
                try:
                    self.sock.sendto(data, addr) # Send the packet through the real socket
                except Exception:
                    pass
            else:
                ready_packets.append((data, addr)) # Collect received packets

        return ready_packets
