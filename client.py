import glfw
import imgui
import socket
import time
import struct
import sys
import math
import random
from collections import deque
import colorsys
import network
from world import Snapshot
from gui import GUI

random.seed(42)

# Constants
SERVER_IP = "127.0.0.1"
SERVER_PORT = 9999
WIDTH, HEIGHT = 720, 720
INTERPOLATION_DELAY = 0.15  # seconds
PLAYER_RADIUS = 0.075
PLAYER_SPEED = 1.0  # units per second
COIN_RADIUS = 0.05

class Client:
    def __init__(self, client_id):
        self.client_id = client_id
        
        # Networking
        real_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        real_sock.setblocking(False)
        self.sock = network.SimulatedSocket(real_sock, latency=0.200, jitter=0.010)
        
        self.server_addr = (SERVER_IP, SERVER_PORT)

        # Game State
        self.players = {}  # remote id -> dict with interp fields {'x','y','tx','ty','t0','t1'}
        self.coins = []
        self.local_x = 0.0
        self.local_y = 0.0
        self.score = 0

        # Networking & Simulation
        self.input_seq = 0
        self.pending_inputs = []  # list of (seq, dx, dy, t)
        self.rtt = 0.0
        self.last_ping = time.time()
        self.server_time_offset = 0.0  # server_time - local_time estimate

        # Snapshots for interpolation
        self.snapshots = deque(maxlen=64)  # list of Snapshot ordered by server_time
        self.debug_show_server = True
        self.last_raw_snapshot = None  # store real-time snapshot (no delay)

        # GUI
        self.gui = GUI(WIDTH, HEIGHT, f"Coin Collector - {self.client_id}")
        self.load_assets()

        # register (join)
        payload = struct.pack('!I', self.client_id)
        self.send_packet(network.pack_message(network.MSG_JOIN_REQUEST, payload))
        
        # Wait for connection response
        print(f"Connecting to server at {SERVER_IP}:{SERVER_PORT} with ID {self.client_id}...")
        start_time = time.time()
        connected = False
        while time.time() - start_time < 2.0: # 2 second timeout
            # pump network
            packets = self.sock.update()
            for data, addr in packets:
                msg_type, payload = network.unpack_message(data)
                if msg_type == network.MSG_SERVER_ACCEPT:
                    assigned = struct.unpack('!I', payload[:4])[0]
                    if assigned == self.client_id:
                        print(f"Successfully connected. Assigned ID: {assigned}")
                        connected = True
                        break
                elif msg_type == network.MSG_JOIN_REJECT:
                    print("Player id already running")
                    self.gui.shutdown()
                    sys.exit(1)
            
            if connected:
                break
            time.sleep(0.01)
            
        if not connected:
            print("Server not connected")
            self.gui.shutdown()
            sys.exit(1)

    def load_assets(self):
        self.bg_tex = self.gui.load_texture("assets/background.png")
        self.coin_tex = self.gui.load_texture("assets/coin.png")

    def send_packet(self, data):
        self.sock.sendto(data, self.server_addr)

    def network_loop(self):
        now = time.time()
        
        # Update simulated socket (processes sends and receives)
        packets = self.sock.update()
        
        for data, addr in packets:
            self.process_packet(data)

        # periodic ping for RTT every second
        if now - self.last_ping > 1000.0:
            payload = struct.pack('!d', time.time()) # Pack current time into ping payload
            self.send_packet(network.pack_message(network.MSG_PING, payload))
            self.last_ping = now

    def process_packet(self, data):
        msg_type, payload = network.unpack_message(data)
        if msg_type is None:
            return
        if msg_type == network.MSG_SERVER_ACCEPT:
            # payload: assigned_id (uint32)
            assigned = struct.unpack('!I', payload[:4])[0]
            # server might reassign. for our simple client ignore if differs
            print(f"Joined, assigned id: {assigned}")
            if assigned != self.client_id:
                self.client_id = assigned

        elif msg_type == network.MSG_PONG:
            ts = struct.unpack('!d', payload[:8])[0] # Server echoed same timestamp
            self.rtt = time.time() - ts # approximate RTT = now - sent_time

        elif msg_type == network.MSG_WORLD_SNAPSHOT:
            # parse snapshot:
            # payload: server_time(double) | num_players(uint32) | players... | num_coins(uint32) | coins...
            off = 0
            server_time = struct.unpack_from('!d', payload, off)[0]; off += 8
            num_players = struct.unpack_from('!I', payload, off)[0]; off += 4
            players = {}
            for _ in range(num_players):
                pid, x, y, score, last_seq = struct.unpack_from('!IffII', payload, off)
                off += struct.calcsize('!IffII')
                players[pid] = (x, y, score, last_seq)
            num_coins = struct.unpack_from('!I', payload, off)[0]; off += 4
            coins = []
            for _ in range(num_coins):
                cid, cx, cy = struct.unpack_from('!Iff', payload, off)
                off += struct.calcsize('!Iff')
                coins.append((cid, cx, cy))

            # push snapshot (server_time is authoritative)
            s = Snapshot(server_time, players, coins)

            # If snapshot arrives out of order, ignore it
            if self.snapshots and s.server_time <= self.snapshots[-1].server_time:
                return

            self.snapshots.append(s)

            #  Store the server snapshot directly for debugging
            if self.debug_show_server:
                self.last_raw_snapshot = s

            # reconciliation for local player
            if self.client_id in players:
                sx, sy, sscore, last_seq = players[self.client_id]
                # accept server position as baseline
                self.local_x = sx
                self.local_y = sy
                self.score = sscore
                # remove pending inputs up to last_seq and reapply remaining
                remaining = []
                for seq, dx, dy, ts in self.pending_inputs:
                    if seq <= last_seq: # already processed by server
                        continue
                    remaining.append((seq, dx, dy, ts))
                self.pending_inputs = remaining
                # reapply remaining inputs on top of authoritative state
                for seq, dx, dy, ts in self.pending_inputs:
                    dt = 1.0 / 60.0
                    norm = math.hypot(dx, dy)
                    if norm > 0:
                        dxn, dyn = dx/norm, dy/norm
                        self.local_x += dxn * PLAYER_SPEED * dt
                        self.local_y += dyn * PLAYER_SPEED * dt

            # update remote players interpolation targets
            now_local = time.time()
            for pid, (x, y, score, last_seq) in players.items():
                if pid == self.client_id: # Do not interpolate local player
                    continue
                if pid not in self.players:
                    # initialize interpolation state (snap->target)
                    self.players[pid] = {'x': x, 'y': y, 'tx': x, 'ty': y, 't0': now_local, 't1': now_local}
                else:
                    p = self.players[pid]
                    # shift current target to previous and set new target
                    p['x'] = p.get('tx', p['x'])
                    p['y'] = p.get('ty', p['y'])
                    p['tx'] = x
                    p['ty'] = y
                    p['t0'] = now_local
                    p['t1'] = now_local + INTERPOLATION_DELAY

            # update coins
            self.coins = [(cx, cy) for (_, cx, cy) in coins]

    def run(self):
        while not self.gui.should_close():
            self.gui.poll_events()
            self.network_loop()

            # Input
            dx, dy = 0, 0
            if glfw.get_key(self.gui.window, glfw.KEY_LEFT) == glfw.PRESS: dx -= 1
            if glfw.get_key(self.gui.window, glfw.KEY_RIGHT) == glfw.PRESS: dx += 1
            if glfw.get_key(self.gui.window, glfw.KEY_UP) == glfw.PRESS: dy += 1
            if glfw.get_key(self.gui.window, glfw.KEY_DOWN) == glfw.PRESS: dy -= 1

            if dx != 0 or dy != 0: # normalize speed
                l = math.hypot(dx, dy)
                dxn, dyn = dx / l, dy / l
                self.local_x += dxn * PLAYER_SPEED * (1.0 / 60.0)
                self.local_y += dyn * PLAYER_SPEED * (1.0 / 60.0)

                # send input command with seq and client timestamp
                self.input_seq += 1
                self.pending_inputs.append((self.input_seq, dxn, dyn, time.time()))
                payload = struct.pack('!Iffd', self.input_seq, dxn, dyn, time.time())
                self.send_packet(network.pack_message(network.MSG_COMMAND, payload))

            # Render
            self.gui.prepare_frame()

            # Background
            self.gui.draw_quad(self.bg_tex, 0, 0, 2, 2)

            # Coins
            for cx, cy in self.coins:
                self.gui.draw_quad(self.coin_tex, cx, cy, COIN_RADIUS * 2, COIN_RADIUS * 2)

            # Players overlay
            imgui.set_next_window_position(0, 0)
            imgui.set_next_window_size(WIDTH, HEIGHT)
            imgui.set_next_window_bg_alpha(0.0)
            imgui.begin("Overlay", flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_INPUTS | imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS)
            draw_list = imgui.get_window_draw_list()

            # Local
            local_color = self.get_player_color(self.client_id)
            self.gui.draw_circle(self.local_x, self.local_y, PLAYER_RADIUS, local_color)
            self.gui.draw_label(draw_list, self.local_x, self.local_y, str(self.client_id))

            # Remote interpolation (lerp between snapshot targets)
            now = time.time()
            for pid, p in list(self.players.items()):
                t0, t1 = p.get('t0', now), p.get('t1', now)
                if t1 == t0:
                    alpha = 1.0
                else:
                    alpha = min(1.0, max(0.0, (now - t0) / (t1 - t0)))
                ix = p['x'] + (p['tx'] - p['x']) * alpha
                iy = p['y'] + (p['ty'] - p['y']) * alpha
                color = self.get_player_color(pid)
                self.gui.draw_circle(ix, iy, PLAYER_RADIUS, color)
                self.gui.draw_label(draw_list, ix, iy, str(pid))

            # Server debug positions
            if self.debug_show_server and self.last_raw_snapshot:
                for pid, (sx, sy, sscore, last_seq) in self.last_raw_snapshot.players.items():
                    color = self.get_player_color(pid)
                    self.gui.draw_ring(sx, sy, PLAYER_RADIUS, color)

            imgui.end()

            # UI
            imgui.begin("Debug")
            imgui.push_style_color(imgui.COLOR_TEXT, 0.0, 1.0, 0.0)
            imgui.text(f"Score: {self.score}")
            imgui.pop_style_color()
            imgui.text(f"FPS: {imgui.get_io().framerate:.1f}")
            imgui.text(f"RTT (estimated): {self.rtt*1000:.1f}ms")
            imgui.text(f"Latency: {self.sock.latency*1000:.1f}ms")
            imgui.text(f"Jitter : {self.sock.jitter*1000:.1f}ms")
            _, self.sock.latency = imgui.slider_float("Latency (s)", self.sock.latency, 0.0, 1.0)
            _, self.sock.jitter = imgui.slider_float("Jitter (s)", self.sock.jitter, 0.0, 0.1)
            _, self.debug_show_server = imgui.checkbox("Show Server Debug Positions", self.debug_show_server)
            imgui.end()

            self.gui.end_frame()
            time.sleep(1/60)
    
    def close(self):
        # Send info to server about disconnecting
        payload = struct.pack('!I', self.client_id)
        self.send_packet(network.pack_message(network.MSG_CLOSE, payload))
        self.sock.sock.close()

        # GUI Cleanup
        self.gui.shutdown()

    @staticmethod
    def get_player_color(client_id):
        h = random.Random(client_id).random()  # consistent per client_id
        r, g, b = colorsys.hsv_to_rgb(h, 0.5, 0.5)
        return (r, g, b)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python Client.py <client_id>")
        sys.exit(1)

    client = Client(int(sys.argv[1]))

    try:
        client.run()
    except KeyboardInterrupt:
        client.close()
