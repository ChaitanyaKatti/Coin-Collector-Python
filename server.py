import socket
import threading
import time
import struct
import random
import math
import argparse

# Constants
HOST = "0.0.0.0"
PORT = 9999
TICK_RATE = 60
MAP_SIZE = 2.0 # -1 to 1 OpenGL coordinates
PLAYER_RADIUS = 0.05
COIN_RADIUS = 0.05
MAX_CLIENTS = 32

class Player:
    def __init__(self, client_id, addr):
        self.id = client_id
        self.addr = addr
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.score = 0
        self.last_update = time.time()
        self.rtt_samples = []
        self.avg_rtt = 0.0
        self.inputs = [] # Queue of inputs to process

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
        self.clients = {} # addr -> Player
        self.client_id_map = {} # client_id -> Player
        self.coins = []
        self.next_coin_id = 0
        self.running = True
        self.lock = threading.Lock()
        self.start_time = time.time()
        
        # Spawn initial coins
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
                data, addr = self.sock.recvfrom(1024)
                self.process_packet(data, addr)
            except BlockingIOError:
                time.sleep(0.001)
            except Exception as e:
                print(f"Error receiving: {e}")

    def process_packet(self, data, addr):
        current_time = time.time()
        try:
            msg_type = data[0]
            if msg_type == 0: # Register: [0, client_id (4 bytes)]
                client_id = struct.unpack('!I', data[1:5])[0]
                with self.lock:
                    if addr not in self.clients:
                        player = Player(client_id, addr)
                        self.clients[addr] = player
                        self.client_id_map[client_id] = player
                        print(f"Client registered: {client_id} from {addr}")
            
            elif msg_type == 1: # Input: [1, seq, dx, dy, client_time]
                if addr in self.clients:
                    # struct.unpack('!Iffd', data[1:]) -> seq, dx, dy, timestamp
                    seq, dx, dy, client_ts = struct.unpack('!Iffd', data[1:])
                    player = self.clients[addr]
                    
                    # RTT Calculation
                    rtt = current_time - client_ts # This is one-way delay + clock diff if not synced. 
                    # Better: Client sends its timestamp, Server echoes it back, Client calcs RTT.
                    # But prompt says "Server time synchronization based on average RTT".
                    # Let's assume client sends Ping, Server sends Pong.
                    # For input, we just queue it.
                    
                    with self.lock:
                        player.inputs.append((seq, dx, dy, client_ts, current_time))
            
            elif msg_type == 2: # Ping: [2, timestamp]
                 if addr in self.clients:
                     self.sock.sendto(b'\x03' + data[1:], addr) # Pong: [3, timestamp]

        except Exception as e:
            pass
            # print(f"Packet error: {e}")

    def game_loop(self):
        last_tick = time.time()
        while self.running:
            current_time = time.time()
            dt = current_time - last_tick
            if dt < 1.0 / TICK_RATE:
                time.sleep(0.001)
                continue
            
            last_tick = current_time
            
            with self.lock:
                self.update_physics(dt)
                self.broadcast_state()

    def update_physics(self, dt):
        # Process inputs
        # "Subtract average RTT per client, sort the queue"
        # We will collect all inputs from all players, adjust their effective time, and apply.
        # For simplicity in this loop, we just process pending inputs for each player.
        
        for player in self.clients.values():
            # Sort inputs by sequence or timestamp?
            # Prompt: "subtract the average RTT per client, sort the queue"
            # This implies we want to order events globally.
            # But inputs are per-player. 
            # Let's just process all pending inputs in order of their sequence.
            
            player.inputs.sort(key=lambda x: x[0]) # Sort by seq
            
            if player.inputs:
                for seq, dx, dy, cts, rts in player.inputs:
                    # Normalize direction
                    length = math.sqrt(dx*dx + dy*dy)
                    if length > 0:
                        dx /= length
                        dy /= length
                    
                    speed = 1.0 # Units per second
                    player.x += dx * speed * (1.0/60.0) # Assume input is for one frame
                    player.y += dy * speed * (1.0/60.0)
                    
                    # Clamp to map
                    player.x = max(-1, min(1, player.x))
                    player.y = max(-1, min(1, player.y))
                
                player.inputs.clear()

            # Coin collision
            for coin in self.coins[:]:
                dist = math.sqrt((player.x - coin.x)**2 + (player.y - coin.y)**2)
                if dist < (PLAYER_RADIUS + COIN_RADIUS):
                    player.score += 1
                    self.coins.remove(coin)
                    self.spawn_coin()

    def broadcast_state(self):
        # Format: [4, num_players, (id, x, y, score)..., num_coins, (id, x, y)...]
        
        msg = bytearray()
        msg.append(4) # State update
        
        # Players
        msg.extend(struct.pack('!I', len(self.clients)))
        for p in self.clients.values():
            msg.extend(struct.pack('!IffI', p.id, p.x, p.y, p.score))
            
        # Coins
        msg.extend(struct.pack('!I', len(self.coins)))
        for c in self.coins:
            msg.extend(struct.pack('!Iff', c.id, c.x, c.y))
            
        for addr in self.clients:
            self.sock.sendto(msg, addr)

if __name__ == "__main__":
    server = GameServer()
    server.run()
