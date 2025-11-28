import glfw
import imgui
from imgui.integrations.glfw import GlfwRenderer
import OpenGL.GL as gl
from PIL import Image
import socket
import threading
import time
import struct
import sys
import math
import random
import collections

random.seed(42)

# Constants
SERVER_IP = "127.0.0.1"
SERVER_PORT = 9999
WIDTH, HEIGHT = 720, 720

class Client:
    def __init__(self, client_id):
        self.client_id = client_id
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.server_addr = (SERVER_IP, SERVER_PORT)
        
        # Game State
        self.players = {} # id -> {x, y, score, target_x, target_y, t}
        self.coins = []
        self.local_x = 0.0
        self.local_y = 0.0
        self.score = 0
        
        # Networking & Simulation
        self.sim_latency = 0.2 # 200ms default
        self.sim_jitter = 0.00
        self.input_seq = 0
        self.pending_inputs = []
        self.send_queue = [] # (time_to_send, data)
        self.recv_queue = [] # (time_to_process, data)
        self.rtt = 0.0
        self.last_ping = time.time()
        
        # GUI
        self.window = self.init_glfw()
        self.impl = GlfwRenderer(self.window)
        self.load_assets()
        
        # Register
        self.send_packet(struct.pack('!BI', 0, client_id))

    def init_glfw(self):
        if not glfw.init(): exit(1)
        glfw.window_hint(glfw.RESIZABLE, False)
        window = glfw.create_window(WIDTH, HEIGHT, f"Coin Collector - {self.client_id}", None, None)
        glfw.make_context_current(window)
        imgui.create_context()
        return window

    def load_assets(self):
        self.bg_tex = self.load_texture("assets/background.png")
        self.coin_tex = self.load_texture("assets/coin.png")

    def load_texture(self, path):
        try:
            img = Image.open(path).convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM)
            tex_id = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img.tobytes())
            return tex_id
        except Exception as e:
            print(f"Failed to load {path}: {e}")
            return 0

    def send_packet(self, data):
        delay = max(0, random.gauss(self.sim_latency, self.sim_jitter))
        self.send_queue.append((time.time() + delay, data))

    def network_loop(self):
        # Sending
        now = time.time()
        for t, data in self.send_queue[:]:
            if now >= t:
                self.sock.sendto(data, self.server_addr)
                self.send_queue.remove((t, data))
        
        # Receiving
        try:
            while True:
                data, _ = self.sock.recvfrom(4096)
                delay = max(0, random.gauss(self.sim_latency, self.sim_jitter))
                self.recv_queue.append((time.time() + delay, data))
        except BlockingIOError: pass
        
        # Processing
        for t, data in self.recv_queue[:]:
            if now >= t:
                self.process_packet(data)
                self.recv_queue.remove((t, data))
                
        # Ping
        if now - self.last_ping > 1.0:
            self.send_packet(struct.pack('!Bd', 2, now))
            self.last_ping = now

    def process_packet(self, data):
        msg_type = data[0]
        if msg_type == 3: # Pong
            ts = struct.unpack('!d', data[1:])[0]
            self.rtt = (time.time() - ts)
        elif msg_type == 4: # State
            offset = 1
            num_players = struct.unpack('!I', data[offset:offset+4])[0]
            offset += 4
            
            seen_players = set()
            for _ in range(num_players):
                pid, x, y, score = struct.unpack('!IffI', data[offset:offset+16])
                offset += 16
                
                seen_players.add(pid)
                if pid == self.client_id:
                    # Reconciliation: Simple snap for now, or smooth correct
                    dist = math.sqrt((x - self.local_x)**2 + (y - self.local_y)**2)
                    if dist > 0.1: # Threshold for correction
                        self.local_x = x
                        self.local_y = y
                    self.score = score
                else:
                    if pid not in self.players:
                        self.players[pid] = {'x': x, 'y': y, 'tx': x, 'ty': y, 't': time.time()}
                    else:
                        p = self.players[pid]
                        p['x'] = p['tx'] # Snap to old target
                        p['y'] = p['ty']
                        p['tx'] = x
                        p['ty'] = y
                        p['t'] = time.time()
            
            # Remove disconnected
            for pid in list(self.players.keys()):
                if pid not in seen_players:
                    del self.players[pid]

            num_coins = struct.unpack('!I', data[offset:offset+4])[0]
            offset += 4
            self.coins = []
            for _ in range(num_coins):
                cid, cx, cy = struct.unpack('!Iff', data[offset:offset+12])
                offset += 12
                self.coins.append((cx, cy))

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
            
            # Prediction
            speed = 1.0 * (1.0/60.0)
            if dx != 0 or dy != 0:
                # Normalize
                l = math.sqrt(dx*dx + dy*dy)
                dx /= l
                dy /= l
                self.local_x += dx * speed
                self.local_y += dy * speed
                
                # Send Input
                self.input_seq += 1
                self.send_packet(struct.pack('!BIffd', 1, self.input_seq, dx, dy, time.time()))
            
            # Render
            imgui.new_frame()
            gl.glClearColor(0.1, 0.1, 0.1, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            
            # Background
            self.draw_quad(self.bg_tex, 0, 0, 2, 2)
            
            # Coins
            for cx, cy in self.coins:
                self.draw_quad(self.coin_tex, cx, cy, 0.1, 0.1)
                
            # Players
            # Prepare ImGui overlay for labels
            imgui.set_next_window_position(0, 0)
            imgui.set_next_window_size(WIDTH, HEIGHT)
            imgui.set_next_window_bg_alpha(0.0)
            imgui.begin("Overlay", flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE | imgui.WINDOW_NO_SCROLLBAR | imgui.WINDOW_NO_INPUTS | imgui.WINDOW_NO_BRING_TO_FRONT_ON_FOCUS)
            draw_list = imgui.get_window_draw_list()

            # Local
            local_color = self.get_player_color(self.client_id)
            self.draw_circle(self.local_x, self.local_y, local_color)
            self.draw_label(draw_list, self.local_x, self.local_y, str(self.client_id))

            # Remote (Interpolate)
            now = time.time()
            for pid, p in self.players.items():
                # Lerp
                alpha = min(1.0, (now - p['t']) * 10.0) # Simple smooth
                ix = p['x'] + (p['tx'] - p['x']) * alpha
                iy = p['y'] + (p['ty'] - p['y']) * alpha
                
                color = self.get_player_color(pid)
                self.draw_circle(ix, iy, color)
                self.draw_label(draw_list, ix, iy, str(pid))
            
            imgui.end()

            # UI
            imgui.begin("Debug")
            imgui.text(f"FPS: {imgui.get_io().framerate:.1f}")
            imgui.text(f"Score: {self.score}")
            imgui.text(f"RTT: {self.rtt*1000:.1f}ms")
            imgui.text(f"Latency: {self.sim_latency*1000:.1f}ms")
            imgui.text(f"Jitter: {self.sim_jitter*1000:.1f}ms")
            _, self.sim_latency = imgui.slider_float("Latency (s)", self.sim_latency, 0.0, 1.0)
            _, self.sim_jitter = imgui.slider_float("Jitter (s)", self.sim_jitter, 0.0, 0.1)
            imgui.end()
            
            imgui.render()
            self.impl.render(imgui.get_draw_data())
            glfw.swap_buffers(self.window)
            
            time.sleep(1/60)

    def get_player_color(self, client_id):
        r = random.Random(client_id).random()
        g = random.Random(client_id + 1).random()
        b = random.Random(client_id + 2).random()
        return (r, g, b)

    def draw_label(self, draw_list, x, y, text):
        sx = (x + 1) * WIDTH / 2
        sy = (1 - y) * HEIGHT / 2
        # Center text roughly
        text_width = imgui.calc_text_size(text).x
        draw_list.add_text(sx - text_width/2, sy - 10, 0xFFFFFFFF, text)

    def draw_quad(self, tex, x, y, w, h):
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

    def draw_circle(self, x, y, color):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor3f(*color)
        gl.glBegin(gl.GL_TRIANGLE_FAN)
        gl.glVertex2f(x, y)
        for i in range(361):
            rad = i * math.pi / 180
            gl.glVertex2f(x + 0.05 * math.cos(rad), y + 0.05 * math.sin(rad))
        gl.glEnd()
        gl.glColor3f(1,1,1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python client.py <client_id>")
        sys.exit(1)
    Client(int(sys.argv[1])).run()
