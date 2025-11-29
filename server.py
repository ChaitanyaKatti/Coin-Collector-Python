import socket
import threading
import time
import struct
import random
import math

# Framing constants (matches repo style)
HEADER_SIZE = 8
CLASS_ID_SIZE = 3

MSG_JOIN_REQUEST = 1
MSG_SERVER_ACCEPT = 2
MSG_PONG = 3
MSG_WORLD_SNAPSHOT = 4
MSG_COMMAND = 5
MSG_PING = 6

HOST = "0.0.0.0"
PORT = 9999
TICK_RATE = 60
MAP_SIZE = 2.0
PLAYER_RADIUS = 0.05
COIN_RADIUS = 0.05

def pack_message(class_id, payload=b''):
    body = f"{class_id:03}".encode('ascii') + payload
    header = f"{len(body):08}".encode('ascii')
    return header + body

def unpack_message(data):
    if len(data) < HEADER_SIZE + CLASS_ID_SIZE:
        return None, None
    body_size = int(data[:HEADER_SIZE].decode('ascii'))
    body = data[HEADER_SIZE:HEADER_SIZE + body_size]
    class_id = int(body[:CLASS_ID_SIZE].decode('ascii'))
    payload = body[CLASS_ID_SIZE:]
    return class_id, payload

class Player:
    def __init__(self, client_id, addr):
        self.id = client_id
        self.addr = addr
        self.x = 0.0
        self.y = 0.0
        self.score = 0
        self.inputs = []  # list of (seq, dx, dy, client_ts, recv_ts)
        self.last_seq = 0

class Coin:
    def __init__(self, coin_id, x, y):
        self.id = coin_id
        self.x = x
        self.y = y

class GameServer:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, PORT))
        self.sock.setblocking(False)
        self.clients = {}          # addr -> Player
        self.client_id_map = {}    # id -> Player
        self.next_client_id = 1000
        self.coins = []
        self.next_coin_id = 0
        self.running = True
        self.lock = threading.Lock()

        for _ in range(10):
            self.spawn_coin()

    def spawn_coin(self):
        x = random.uniform(-1 + COIN_RADIUS, 1 - COIN_RADIUS)
        y = random.uniform(-1 + COIN_RADIUS, 1 - COIN_RADIUS)
        self.coins.append(Coin(self.next_coin_id, x, y))
        self.next_coin_id += 1

    def run(self):
        print(f"Server started on {HOST}:{PORT}")
        threading.Thread(target=self.receive_loop, daemon=True).start()
        threading.Thread(target=self.game_loop, daemon=True).start()
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.running = False

    def receive_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65536)
                class_id, payload = unpack_message(data)
                if class_id is None:
                    continue
                self.process_packet(class_id, payload, addr)
            except BlockingIOError:
                time.sleep(0.001)
            except Exception as e:
                print("recv error", e)

    def process_packet(self, class_id, payload, addr):
        now = time.time()
        if class_id == MSG_JOIN_REQUEST:
            # payload: desired client id (uint32)
            desired = struct.unpack_from('!I', payload, 0)[0] if len(payload) >= 4 else None
            with self.lock:
                # assign id
                assigned = self.next_client_id
                self.next_client_id += 1
                p = Player(assigned, addr)
                # place randomly
                p.x = random.uniform(-0.5, 0.5)
                p.y = random.uniform(-0.5, 0.5)
                self.clients[addr] = p
                self.client_id_map[assigned] = p
            # send accept with assigned id
            payload = struct.pack('!I', assigned)
            self.sock.sendto(pack_message(MSG_SERVER_ACCEPT, payload), addr)
            print(f"Client {assigned} joined from {addr}")

        elif class_id == MSG_COMMAND:
            # payload: seq(uint32), dx(float), dy(float), client_ts(double)
            if addr not in self.clients:
                return
            seq, dx, dy, client_ts = struct.unpack_from('!Iffd', payload, 0)
            with self.lock:
                p = self.clients[addr]
                p.inputs.append((seq, dx, dy, client_ts, now))

        elif class_id == MSG_PING:
            # echo pong: send back timestamp
            self.sock.sendto(pack_message(MSG_PONG, payload), addr)

    def game_loop(self):
        last_tick = time.time()
        while self.running:
            now = time.time()
            dt = now - last_tick
            if dt < 1.0 / TICK_RATE:
                time.sleep(0.001)
                continue
            last_tick = now
            with self.lock:
                self.update_physics(dt)
                self.broadcast_state()

    def process_player_inputs(self, p, dt):
        """Apply player inputs, update movement direction flags."""
        p.inputs.sort(key=lambda x: x[0])

        moved = False
        last_dx, last_dy = 0.0, 0.0

        for seq, dx, dy, client_ts, recv_ts in p.inputs:
            # mark movement for collision logic
            if dx != 0 or dy != 0:
                moved = True
                last_dx, last_dy = dx, dy

            # apply movement
            norm = math.hypot(dx, dy)
            if norm > 0:
                dxn, dyn = dx / norm, dy / norm
                speed = 1.0
                p.x += dxn * speed * (1.0 / 60.0)
                p.y += dyn * speed * (1.0 / 60.0)
                p.x = max(-1, min(1, p.x))
                p.y = max(-1, min(1, p.y))

            p.last_seq = max(p.last_seq, seq)

        p.inputs.clear()
        p._moved = moved
        p._last_dir = (last_dx, last_dy)

    def resolve_player_coin_collisions(self, p):
        for coin in self.coins[:]:
            dist = math.hypot(p.x - coin.x, p.y - coin.y)
            if dist < (PLAYER_RADIUS + COIN_RADIUS):
                p.score += 1
                self.coins.remove(coin)
                self.spawn_coin()

    def resolve_player_player_collisions(self, players):
        """Push players apart based on relative motion history."""
        R = PLAYER_RADIUS
        N = len(players)

        for i in range(N):
            for j in range(i + 1, N):
                a = players[i]
                b = players[j]

                dx = b.x - a.x
                dy = b.y - a.y
                dist = math.hypot(dx, dy)
                min_dist = 2 * R

                if dist == 0:
                    dx, dy = 1e-6, 0
                    dist = 1e-6

                if dist < min_dist:
                    overlap = min_dist - dist
                    nx = dx / dist
                    ny = dy / dist

                    a_moved = getattr(a, "_moved", False)
                    b_moved = getattr(b, "_moved", False)

                    # weight rules:
                    if a_moved and not b_moved:
                        w_a, w_b = 1.0, 0.0
                    elif b_moved and not a_moved:
                        w_a, w_b = 0.0, 1.0
                    elif a_moved and b_moved:
                        w_a, w_b = 0.5, 0.5
                    else:
                        continue  # idle–idle, no push

                    # apply displacement
                    a.x -= nx * overlap * w_a
                    a.y -= ny * overlap * w_a
                    b.x += nx * overlap * w_b
                    b.y += ny * overlap * w_b

                    # clamp to bounds
                    a.x = max(-1, min(1, a.x))
                    a.y = max(-1, min(1, a.y))
                    b.x = max(-1, min(1, b.x))
                    b.y = max(-1, min(1, b.y))


    def update_physics(self, dt):
        players = list(self.client_id_map.values())

        # 1. Apply inputs and movement flags
        for p in players:
            self.process_player_inputs(p, dt)
            self.resolve_player_coin_collisions(p)

        # 2. Resolve player–player collisions
        self.resolve_player_player_collisions(players)

    def broadcast_state(self):
        # payload: server_time(double) | num_players(uint32) |
        # for each player: id(uint32), x(float), y(float), score(uint32), last_seq(uint32)
        # | num_coins(uint32) | for each coin: id(uint32), x(float), y(float)
        payload = bytearray()
        payload.extend(struct.pack('!d', time.time()))
        payload.extend(struct.pack('!I', len(self.client_id_map)))
        for pid, p in self.client_id_map.items():
            payload.extend(struct.pack('!IffII', pid, p.x, p.y, p.score, p.last_seq))
        payload.extend(struct.pack('!I', len(self.coins)))
        for c in self.coins:
            payload.extend(struct.pack('!Iff', c.id, c.x, c.y))

        msg = pack_message(MSG_WORLD_SNAPSHOT, bytes(payload))
        # send to all clients
        for addr in list(self.clients.keys()):
            try:
                self.sock.sendto(msg, addr)
            except Exception:
                pass

if __name__ == "__main__":
    server = GameServer()
    server.run()