import glfw
import imgui
from imgui.integrations.glfw import GlfwRenderer
import OpenGL.GL as gl
from PIL import Image
import socket
import time
import struct
import sys
import math
import random
from collections import deque
import colorsys

random.seed(42)

# Framing constants (matches repo style)
HEADER_SIZE = 8
CLASS_ID_SIZE = 3

# Message class IDs (ascii 3 digits)
MSG_JOIN_REQUEST = 1   # "001"
MSG_SERVER_ACCEPT = 2  # "002"
MSG_PONG = 3           # "003"
MSG_WORLD_SNAPSHOT = 4 # "004"
MSG_COMMAND = 5        # "005"
MSG_PING = 6           # "006"
MSG_CLOSE = 7          # "007"

# Constants
SERVER_IP = "127.0.0.1"
SERVER_PORT = 9999
WIDTH, HEIGHT = 720, 720
INTERPOLATION_DELAY = 0.15  # seconds
PLAYER_RADIUS = 0.05
COIN_RADIUS = 0.05

def pack_message(class_id, payload=b''):
    body = f"{class_id:03}".encode('ascii') + payload
    header = f"{len(body):08}".encode('ascii')
    return header + body

def unpack_message(data):
    # data is a single UDP datagram containing full framed message
    if len(data) < HEADER_SIZE + CLASS_ID_SIZE:
        return None, None
    body_size = int(data[:HEADER_SIZE].decode('ascii'))
    body = data[HEADER_SIZE:HEADER_SIZE + body_size]
    class_id = int(body[:CLASS_ID_SIZE].decode('ascii'))
    payload = body[CLASS_ID_SIZE:]
    return class_id, payload

class Snapshot:
    def __init__(self, server_time, players, coins):
        self.server_time = server_time
        self.players = players  # dict id -> (x,y,score,last_seq)
        self.coins = coins

class Client:
    def __init__(self, client_id):
        self.client_id = client_id
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.server_addr = (SERVER_IP, SERVER_PORT)

        # Game State
        self.players = {}  # remote id -> dict with interp fields {'x','y','tx','ty','t0','t1'}
        self.coins = []
        self.local_x = 0.0
        self.local_y = 0.0
        self.score = 0

        # Networking & Simulation
        self.sim_latency = 0.05 # Simulate latency, RTT
        self.sim_jitter = 0.00
        self.input_seq = 0
        self.pending_inputs = []  # list of (seq, dx, dy, t)
        self.recv_queue = []      # recv datagrams (time_to_process, raw)
        self.rtt = 0.0
        self.last_ping = time.time()
        self.server_time_offset = 0.0  # server_time - local_time estimate

        # Snapshots for interpolation
        self.snapshots = deque(maxlen=64)  # list of Snapshot ordered by server_time
        self.debug_show_server = False
        self.last_raw_snapshot = None  # store real-time snapshot (no delay)

        # GUI
        self.window = self.init_glfw(f"Coin Collector - {self.client_id}")
        self.impl = GlfwRenderer(self.window)
        self.load_assets()

        # register (join)
        payload = struct.pack('!I', self.client_id)
        self.send_packet(pack_message(MSG_JOIN_REQUEST, payload))

    @staticmethod
    def init_glfw(title="Coin Collector Client"):
        if not glfw.init():
            print("Failed to initialize GLFW")
            exit(1)
        glfw.window_hint(glfw.RESIZABLE, False)
        glfw.window_hint(glfw.SAMPLES, 4)
        window = glfw.create_window(WIDTH, HEIGHT, title, None, None)
        if not window:
            print("Failed to create GLFW window")
            glfw.terminate()
            exit(1)
        glfw.make_context_current(window)
        imgui.create_context()
        return window

    def load_assets(self):
        self.bg_tex = self.load_texture("assets/background.png")
        self.coin_tex = self.load_texture("assets/coin.png")

    def send_packet(self, data):
        # simulate network latency/jitter locally by delaying send
        delay = max(0, random.gauss(self.sim_latency, self.sim_jitter))
        self.recv_queue.append((time.time() + delay, ('send', data)))

    def network_loop(self):
        now = time.time()
        # flush simulated sends
        for t, item in self.recv_queue[:]:
            if now >= t and item[0] == 'send':
                _, data = item
                try:
                    self.sock.sendto(data, self.server_addr)
                except Exception:
                    pass
                self.recv_queue.remove((t, item))

        # receive from socket and simulate latency
        try:
            while True:
                data, _ = self.sock.recvfrom(65536)
                delay = max(0, random.gauss(self.sim_latency, self.sim_jitter))
                self.recv_queue.append((time.time() + delay, ('recv', data)))
        except BlockingIOError:
            pass

        # process delayed recv/send items
        for t, item in self.recv_queue[:]:
            if now >= t and item[0] == 'recv':
                _, data = item
                self.process_packet(data)
                self.recv_queue.remove((t, item))

        # periodic ping for RTT every second
        if now - self.last_ping > 1.0:
            payload = struct.pack('!d', time.time()) # Pack current time into ping payload
            self.sock.sendto(pack_message(MSG_PING, payload), self.server_addr)
            self.last_ping = now

    def process_packet(self, data):
        class_id, payload = unpack_message(data)
        if class_id is None:
            return
        if class_id == MSG_SERVER_ACCEPT:
            # payload: assigned_id (uint32)
            assigned = struct.unpack('!I', payload[:4])[0]
            # server might reassign. for our simple client ignore if differs
            print(f"Joined, assigned id: {assigned}")
            if assigned != self.client_id:
                self.client_id = assigned

        elif class_id == MSG_PONG:
            ts = struct.unpack('!d', payload[:8])[0] # Server echoed same timestamp
            self.rtt = time.time() - ts # approximate RTT = now - sent_time

        elif class_id == MSG_WORLD_SNAPSHOT:
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
            self.snapshots.append(s)

            #  Store the server snapshot directly for debugging
            if self.debug_show_server:
                self.last_raw_snapshot = Snapshot(server_time, players, coins)

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
                    if seq <= last_seq:
                        continue
                    remaining.append((seq, dx, dy, ts))
                self.pending_inputs = remaining
                # reapply remaining inputs on top of authoritative state
                for seq, dx, dy, ts in self.pending_inputs:
                    dt = 1.0 / 60.0
                    norm = math.hypot(dx, dy)
                    if norm > 0:
                        dxn, dyn = dx/norm, dy/norm
                        speed = 1.0
                        self.local_x += dxn * speed * dt
                        self.local_y += dyn * speed * dt

            # update remote players interpolation targets
            now_local = time.time()
            for pid, (x, y, score, last_seq) in players.items():
                if pid == self.client_id:
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
        while not glfw.window_should_close(self.window):
            glfw.poll_events()
            self.impl.process_inputs()
            self.network_loop()

            # Input
            dx, dy = 0, 0
            if glfw.get_key(self.window, glfw.KEY_LEFT) == glfw.PRESS: dx -= 1
            if glfw.get_key(self.window, glfw.KEY_RIGHT) == glfw.PRESS: dx += 1
            if glfw.get_key(self.window, glfw.KEY_UP) == glfw.PRESS: dy += 1
            if glfw.get_key(self.window, glfw.KEY_DOWN) == glfw.PRESS: dy -= 1

            speed = 1.0 * (1.0 / 60.0)
            if dx != 0 or dy != 0: # normalize speed
                l = math.hypot(dx, dy)
                dxn, dyn = dx / l, dy / l
                self.local_x += dxn * speed
                self.local_y += dyn * speed

                # send input command with seq and client timestamp
                self.input_seq += 1
                self.pending_inputs.append((self.input_seq, dxn, dyn, time.time()))
                payload = struct.pack('!Iffd', self.input_seq, dxn, dyn, time.time())
                self.sock.sendto(pack_message(MSG_COMMAND, payload), self.server_addr)

            # Render
            imgui.new_frame()
            gl.glClearColor(0.1, 0.1, 0.1, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)

            # Background
            self.draw_quad(self.bg_tex, 0, 0, 2, 2)

            # Coins
            for cx, cy in self.coins:
                self.draw_quad(self.coin_tex, cx, cy, COIN_RADIUS * 2, COIN_RADIUS * 2)

            # Players overlay
            imgui.set_next_window_position(0, 0)
            imgui.set_next_window_size(WIDTH, HEIGHT)
            imgui.set_next_window_bg_alpha(0.0)
            imgui.begin("Overlay", flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_INPUTS | imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS)
            draw_list = imgui.get_window_draw_list()

            # Local
            local_color = self.get_player_color(self.client_id)
            self.draw_circle(self.local_x, self.local_y, PLAYER_RADIUS, local_color)
            self.draw_label(draw_list, self.local_x, self.local_y, str(self.client_id))

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
                self.draw_circle(ix, iy, PLAYER_RADIUS, color)
                self.draw_label(draw_list, ix, iy, str(pid))

            # Server debug positions
            if self.debug_show_server and self.last_raw_snapshot:
                for pid, (sx, sy, sscore, last_seq) in self.last_raw_snapshot.players.items():
                    color = self.get_player_color(pid)
                    self.draw_ring(sx, sy, PLAYER_RADIUS, color)

            imgui.end()

            # UI
            imgui.begin("Debug")
            imgui.text(f"FPS: {imgui.get_io().framerate:.1f}")
            imgui.text(f"Score: {self.score}")
            imgui.text(f"RTT (estimated): {self.rtt*1000:.1f}ms")
            imgui.text(f"Latency(RTT) (sim): {self.sim_latency*1000:.1f}ms")
            imgui.text(f"Jitter (sim): {self.sim_jitter*1000:.1f}ms")
            _, self.sim_latency = imgui.slider_float("Latency (s)", self.sim_latency, 0.0, 1.0)
            _, self.sim_jitter = imgui.slider_float("Jitter (s)", self.sim_jitter, 0.0, 0.1)
            _, self.debug_show_server = imgui.checkbox("Show Server Debug Positions", self.debug_show_server)
            imgui.end()

            imgui.render()
            self.impl.render(imgui.get_draw_data())
            glfw.swap_buffers(self.window)

            time.sleep(1/120)
    
    def close(self):
        # Send info to server about disconnecting
        payload = struct.pack('!I', self.client_id)
        self.sock.sendto(pack_message(MSG_CLOSE, payload), self.server_addr)
        self.sock.close()

        # GUI Cleanup
        self.impl.shutdown()
        glfw.terminate()

    @staticmethod
    def load_texture(path):
        try:
            img = Image.open(path).convert("RGBA")
            img = img.point(lambda x: ((x/255)**(1/1.3))*255)
            tex_id = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img.tobytes())
            return tex_id
        except Exception as e:
            print(f"Failed to load {path}: {e}")
            return 0

    @staticmethod
    def get_player_color(client_id):
        h = (client_id * 0.618033988749895) % 1.0  # golden ratio conjugate
        r, g, b = colorsys.hsv_to_rgb(h, 0.5, 0.5)
        return (r, g, b)

    @staticmethod
    def draw_label(draw_list, x, y, text):
        sx = (x + 1) * WIDTH / 2
        sy = (1 - y) * HEIGHT / 2
        text_width = imgui.calc_text_size(text).x
        draw_list.add_text(sx - text_width/2, sy - 10, 0xFFFFFFFF, text)

    @staticmethod
    def draw_quad(tex, x, y, w, h):
        gl.glEnable(gl.GL_TEXTURE_2D)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glBegin(gl.GL_QUADS)
        gl.glTexCoord2f(0, 1); gl.glVertex2f(x - w/2, y - h/2)
        gl.glTexCoord2f(1, 1); gl.glVertex2f(x + w/2, y - h/2)
        gl.glTexCoord2f(1, 0); gl.glVertex2f(x + w/2, y + h/2)
        gl.glTexCoord2f(0, 0); gl.glVertex2f(x - w/2, y + h/2)
        gl.glEnd()

    @staticmethod
    def draw_circle(x, y, r, color):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor3f(*color)
        gl.glBegin(gl.GL_TRIANGLE_FAN)
        gl.glVertex2f(x, y)
        for i in range(361):
            rad = i * math.pi / 180
            gl.glVertex2f(x + r * math.cos(rad), y + r * math.sin(rad))
        gl.glEnd()
        gl.glColor3f(1,1,1)

    @staticmethod
    def draw_ring(x, y, radius, color):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor3f(*color)
        gl.glLineWidth(2.0) # 2px line
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(64):
            rad = 2 * math.pi * i / 64
            gl.glVertex2f(x + radius * math.cos(rad), y + radius * math.sin(rad))
        gl.glEnd()
        gl.glColor3f(1, 1, 1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python Client.py <client_id>")
        sys.exit(1)

    client = Client(int(sys.argv[1]))

    try:
        client.run()
    except KeyboardInterrupt:
        client.close()
