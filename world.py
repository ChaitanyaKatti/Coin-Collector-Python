class Coin:
    def __init__(self, coin_id, x, y):
        self.id = coin_id
        self.x = x
        self.y = y

class Snapshot:
    def __init__(self, server_time, players, coins):
        self.server_time = server_time
        self.players = players  # client_id -> (x,y,score,last_seq)
        self.coins = coins
