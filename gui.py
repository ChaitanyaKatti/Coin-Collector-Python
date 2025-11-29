import glfw
import imgui
from imgui.integrations.glfw import GlfwRenderer
import OpenGL.GL as gl
from PIL import Image
import math

class GUI:
    def __init__(self, width, height, title):
        self.width = width
        self.height = height
        self.window = self.init_glfw(width, height, title)
        self.impl = GlfwRenderer(self.window)
        
    def init_glfw(self, width, height, title):
        if not glfw.init():
            print("Failed to initialize GLFW")
            exit(1)
        glfw.window_hint(glfw.RESIZABLE, False)
        glfw.window_hint(glfw.SAMPLES, 4)
        window = glfw.create_window(width, height, title, None, None)
        if not window:
            print("Failed to create GLFW window")
            glfw.terminate()
            exit(1)
        glfw.make_context_current(window)
        imgui.create_context()
        return window

    def should_close(self):
        return glfw.window_should_close(self.window)

    def poll_events(self):
        glfw.poll_events()
        self.impl.process_inputs()

    def prepare_frame(self):
        imgui.new_frame()
        gl.glClearColor(0.1, 0.1, 0.1, 1)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)

    def end_frame(self):
        imgui.render()
        self.impl.render(imgui.get_draw_data())
        glfw.swap_buffers(self.window)

    def shutdown(self):
        self.impl.shutdown()
        glfw.terminate()

    def load_texture(self, path):
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

    def draw_circle(self, x, y, r, color):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor3f(*color)
        gl.glBegin(gl.GL_TRIANGLE_FAN)
        gl.glVertex2f(x, y)
        for i in range(361):
            rad = i * math.pi / 180
            gl.glVertex2f(x + r * math.cos(rad), y + r * math.sin(rad))
        gl.glEnd()
        gl.glColor3f(1,1,1)

    def draw_ring(self, x, y, radius, color):
        gl.glDisable(gl.GL_TEXTURE_2D)
        gl.glColor3f(*color)
        gl.glLineWidth(2.0) # 2px line
        gl.glBegin(gl.GL_LINE_LOOP)
        for i in range(64):
            rad = 2 * math.pi * i / 64
            gl.glVertex2f(x + radius * math.cos(rad), y + radius * math.sin(rad))
        gl.glEnd()
        gl.glColor3f(1, 1, 1)

    def draw_label(self, draw_list, x, y, text):
        sx = (x + 1) * self.width / 2
        sy = (1 - y) * self.height / 2
        text_width = imgui.calc_text_size(text).x
        draw_list.add_text(sx - text_width/2, sy - 10, 0xFFFFFFFF, text)
