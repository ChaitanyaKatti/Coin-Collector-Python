import math
import random
import socket
import struct
import threading
import time

import network
from player import Player
from world import Coin

HOST = "0.0.0.0"
PORT = 9999
TICK_RATE = 60
MAP_SIZE = 2.0
PLAYER_RADIUS = 0.075
PLAYER_SPEED = 1.0  # units per second
COIN_RADIUS = 0.05
BORDER_SIZE = 2*(8/1024)

class GameServer:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, PORT))
        self.sock.setblocking(False)
        self.clients = {}          # addr -> Player
        self.client_id_map = {}    # id -> Player
        self.last_client_recv = {}  # addr -> timestamp
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
                msg_type, payload = network.unpack_message(data)
                if msg_type is None:
                    continue
                self.process_packet(msg_type, payload, addr)
            except BlockingIOError:
                time.sleep(0.001)
            except Exception as e:
                print("recv error", e)

    def process_packet(self, msg_type, payload, addr):
        now = time.time()
        if msg_type == network.MSG_JOIN_REQUEST:
            # payload: desired client id (uint32)
            client_id = struct.unpack_from('!I', payload, 0)[0]

            with self.lock:
                if client_id in self.client_id_map: # ID already taken
                    self.sock.sendto(network.pack_message(network.MSG_JOIN_REJECT), addr)
                    return

                p = Player(client_id, addr)

                # place roughly in center
                p.x = random.uniform(-0.5, 0.5)
                p.y = random.uniform(-0.5, 0.5)
                self.clients[addr] = p
                self.client_id_map[client_id] = p
                # Resolve collisions with existing players
                self.resolve_player_player_collisions(list(self.clients.values()))

            # send accept with desired id
            payload = struct.pack('!I', client_id)
            self.sock.sendto(network.pack_message(network.MSG_SERVER_ACCEPT, payload), addr)
            print(f"Client {client_id} joined from {addr}")

        elif msg_type == network.MSG_COMMAND:
            # payload: seq(uint32), dx(float), dy(float), client_ts(double)
            if addr not in self.clients:
                return
            seq, dx, dy, client_ts = struct.unpack_from('!Iffd', payload, 0)
            with self.lock:
                p = self.clients[addr] # type: Player
                p.inputs.append((seq, dx, dy, client_ts, now))

        elif msg_type == network.MSG_PING:
            # echo pong: send back timestamp
            self.sock.sendto(network.pack_message(network.MSG_PONG, payload), addr)

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

    def process_player_inputs(self, p: Player, dt: float):
        """Apply player inputs, update movement direction flags."""
        p.inputs.sort(key=lambda x: x[0]) # sort by input seq

        moved = False # Tag for collision resolution
        last_dx, last_dy = 0.0, 0.0

        # Process packets in order,
        while p.inputs:
            seq, dx, dy, client_ts, recv_ts = p.inputs[0]

            if seq <= p.last_seq: # duplicate / old packet
                p.inputs.pop(0)
                continue

            if seq != p.last_seq + 1: # Out-of-order packet
                last_recv = self.last_client_recv.get(p.addr, 0.0)
                print(f"Out of order input from player {p.id}: expected {p.last_seq + 1}, got {seq}. [HIGH VARIABILITY IN LATENCY]")
                # Check if too much time has passed since last in-order packet
                if time.time() - last_recv > 0.100: # 100 ms timeout
                    print(f"Skipping to seq {seq} for player {p.id} due to timeout")
                    p.last_seq = seq - 1
                else:
                    break # Exit loop, wait for in-order packet
            else:
                self.last_client_recv[p.addr] = time.time()

            p.inputs.pop(0)

            if dx != 0 or dy != 0: # Mark movement for collision logic
                moved = True
                last_dx, last_dy = dx, dy

            # Apply movement
            norm = math.hypot(dx, dy)
            if norm > 0:
                dxn, dyn = dx / norm, dy / norm
                p.x += dxn * PLAYER_SPEED * (1.0 / 60.0)
                p.y += dyn * PLAYER_SPEED * (1.0 / 60.0)

            p.last_seq = max(p.last_seq, seq)

        p._moved = moved
        p._last_dir = (last_dx, last_dy)

    def resolve_player_coin_collisions(self, p: Player):
        for coin in self.coins[:]:
            dist = math.hypot(p.x - coin.x, p.y - coin.y)
            if dist < (PLAYER_RADIUS + COIN_RADIUS):
                p.score += 1
                self.coins.remove(coin)
                self.spawn_coin()

    def resolve_player_player_collisions(self, players: list[Player]):
        """Push players apart based on relative motion history."""
        R = PLAYER_RADIUS
        N = len(players)

        for i in range(N):
            a = players[i]
            for j in range(i + 1, N):
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

                    a_moved = a._moved
                    b_moved = b._moved

                    # Weight rules
                    if a_moved and not b_moved:
                        w_a, w_b = 1.0, 0.0
                    elif b_moved and not a_moved:
                        w_a, w_b = 0.0, 1.0
                    elif a_moved and b_moved:
                        w_a, w_b = 0.5, 0.5
                    else:
                        w_a, w_b = 0.5, 0.5

                    # apply displacement
                    a.x -= nx * overlap * w_a
                    a.y -= ny * overlap * w_a
                    b.x += nx * overlap * w_b
                    b.y += ny * overlap * w_b

            # Clamp to bounds
            a.x = max(-1 + PLAYER_RADIUS + BORDER_SIZE, min(1-PLAYER_RADIUS-BORDER_SIZE, a.x))
            a.y = max(-1 + PLAYER_RADIUS + BORDER_SIZE, min(1-PLAYER_RADIUS-BORDER_SIZE, a.y))

    def update_physics(self, dt: float):
        players = list(self.client_id_map.values())

        # Apply inputs and movement flags
        for p in players:
            self.process_player_inputs(p, dt)

        # Resolve player–player collisions
        self.resolve_player_player_collisions(players)
        
        # Resolve player–coin collisions
        for p in players:
            self.resolve_player_coin_collisions(p)

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

        msg = network.pack_message(network.MSG_WORLD_SNAPSHOT, bytes(payload))
        # send to all clients
        for addr in list(self.clients.keys()):
            try:
                self.sock.sendto(msg, addr)
            except Exception:
                pass

if __name__ == "__main__":
    server = GameServer()
    server.run()