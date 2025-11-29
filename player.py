class Player:
    def __init__(self, client_id, addr):
        self.id = client_id
        self.addr = addr
        self.x = 0.0
        self.y = 0.0
        self.score = 0
        self.inputs = []  # list of (seq, dx, dy, client_ts, recv_ts)
        self.last_seq = 0
        self._moved = False
        self._last_dir = (0.0, 0.0)
