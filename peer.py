"""
Peer Node for Gossipbased P2P Network

Key design features:
  - Peer list is taken directly from REGISTER_RESPONSE (no separate PEER_LIST_REQUEST).
  - The seed socket is handed to a background listener ONLY after all synchronous
    req/response exchanges are complete, eliminating read races.
  - Ping failures AND missing PONG replies both increment the missed-ping counter.

Protocol: 4 byte big endian length + JSON payload 
to use:  python peer.py <IP> <Port> 
"""

import sys
import os
import csv
import json
import socket
import struct
import threading
import time
import random
import hashlib
import logging
import subprocess
import platform

# Framing
HEADER_SIZE = 4

def send_msg(sock: socket.socket,data: dict) -> bool:
    try:
        payload= json.dumps(data).encode()
        sock.sendall(struct.pack(">I", len(payload)) +payload)
        return True
    except Exception:
        return False

def recv_msg(sock: socket.socket):
    try:
        raw= _recv_exact(sock,HEADER_SIZE)
        if not raw:
            return None
        n= struct.unpack(">I", raw)[0]
        raw= _recv_exact(sock, n)
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _recv_exact(sock: socket.socket, n: int):
    buf = b""
    while len(buf) < n:
        chunk= sock.recv(n - len(buf))
        if not chunk:
            return None
        buf+= chunk
    return buf


# System level ICMP ping helper
def system_ping(host: str) -> bool:
    """Return True if host replies to one ICMP ping within 1 s."""
    if platform.system().lower()== "windows":
        cmd= ["ping", "-n","1", "-w","1000",host]
    else:
        cmd= ["ping", "-c","1", "-W","1",host]
    try:
        r= subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


# PeerNode registers w seeds,selects powerlaw nbours,gossips & participates in 2level deadnode detection
class PeerNode:
    """
    Startup sequence (strictly serial, no socket hand-off until done)
    1. Start TCP server (to accept inbound peer connections)
    2. For each chosen seed:
         a. Connect (TCP)
         b. REGISTER_REQUEST to REGISTER_RESPONSE (contains peer list)
         c. Keep socket, start background listener for DEAD_CONFIRMED etc.
    3. Union of peer lists from all successful seeds to select neighbours
    4. Connect to neighbours (TCP, send HELLO)
    5. Start gossip loop + liveness loop
    """

    GOSSIP_INTERVAL   = 5    # sec between gossip messages
    MAX_GOSSIP        = 10   # max gossip messages to originate
    PING_INTERVAL     = 8    # seconds between ping rounds
    PING_MISS_THRESH  = 3    # consecutive missed pings before suspicion
    SUSPECT_TIMEOUT   = 20   # secs to wait for peer suspicion confirmations

    def __init__(self, host: str, port: int, config_path: str = "config.csv"):
        self.host =host
        self.port =int(port)
        self.id = f"{host}:{port}"

        self.all_seeds: list[tuple] = []
        self._load_config(config_path)
        self.n_seeds =len(self.all_seeds)
        self.quorum =(self.n_seeds // 2) + 1

        # Message List hashes of all gossip messages seen
        self.ml: set[str] = set()
        self.ml_lock= threading.Lock()

        # Neighbours are (ip,port) -> socket
        self.neighbours: dict[tuple, socket.socket] = {}
        self.nbr_lock= threading.Lock()

        # Seed sockets are (ip,port) -> socket (kept open for DEAD_CONFIRMED)
        self.seed_socks: dict[tuple, socket.socket] = {}
        self.seed_lock= threading.Lock()

        # Liveness
        self.missed_pings: dict[tuple, int] = {}
        self.mp_lock= threading.Lock()
        # pong_received: set of peer_keys that sent PONG this round
        self.pong_received: set[tuple] = set()
        self.pong_lock= threading.Lock()

        # Suspicion state: peer_key -> {confirmations:set, reported:bool}
        self.suspected: dict[tuple, dict] = {}
        self.susp_lock= threading.Lock()

        # Gossip counter
        self.gossip_count= 0
        self.gc_lock= threading.Lock()

        self._setup_logger()
        self.log(f"Initialized  quorum={self.quorum}/{self.n_seeds}")

    def _load_config(self, path: str):   #Config / Logger
        if not os.path.exists(path):
            print(f"[ERROR] config.csv not found: {path}")
            sys.exit(1)
        with open(path, newline="") as f:
            for row in csv.reader(f):
                row= [c.strip() for c in row]
                if len(row) >= 2:
                    self.all_seeds.append((row[0], int(row[1])))

    def _setup_logger(self):
        self.logger= logging.getLogger(f"peer_{self.port}")
        self.logger.setLevel(logging.DEBUG)
        fmt= logging.Formatter("%(asctime)s [PEER %(name)s] %(message)s",
                                datefmt="%H:%M:%S")
        fh= logging.FileHandler(f"outputfile_peer_{self.port}.txt", mode="a")
        fh.setFormatter(fmt)
        self.logger.addHandler(fh)
        ch= logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        self.logger.addHandler(ch)

    def log(self, msg: str):
        self.logger.info(msg)

    #Startup
    def start(self):
        self._start_server() # 1. Server
        # 2. Register w seeds, collect peer lists inline from REGISTER_RESPONSE
        all_peer_entries = self._register_and_collect(self.quorum)
        if not all_peer_entries[0]:  # registered_seeds list is empty
            self.log("FATAL: could not register with enough seeds. Exiting.")
            sys.exit(1)

        registered_seeds, peer_entries = all_peer_entries

        # 3. Union peer list
        self.log(f"Union peer list has {len(peer_entries)} entries: {peer_entries}")
        neighbours = self._select_neighbours(peer_entries) #select neighbours
        self.log(f"Selected neighbours (power-law): {neighbours}")

        # 4. Connect to neighbours
        self._connect_to_neighbours(neighbours)
        time.sleep(2)  # allow inbound connections from neighbours too

        # 5. Loops
        threading.Thread(target=self._gossip_loop,daemon=True).start()
        threading.Thread(target=self._liveness_loop,daemon=True).start()

        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            self.log("Shutting down.")


    def _start_server(self):  # Server 
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,1)
        srv.bind((self.host,self.port))
        srv.listen(100)
        self.log(f"Listening on {self.host}:{self.port}")
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()

    def _accept_loop(self,srv: socket.socket):
        while True:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle_inbound,
                                 args=(conn, addr), daemon=True).start()
            except Exception:
                break

    def _handle_inbound(self, conn: socket.socket, addr):
        """ 
        Handle an inbound connection from another peer
        Peer level suspicion sends SUSPECT_REQUEST to all neighbours except suspect
        & waits for SUSPECT_RESPONSE, quorum of confirmations triggers DEAD_REPORT
        """
        peer_key = None
        while True:
            msg = recv_msg(conn)
            if msg is None:
                #Instantly trigger suspicion for dropped inbound connections too!
                if peer_key:
                    self.log(f"Lost inbound connection from {peer_key}")
                    self._start_suspicion(peer_key)
                break
            
            t = msg.get("type", "")
            if t == "HELLO":
                peer_key = (msg["ip"], int(msg["port"]))
                with self.nbr_lock:
                    self.neighbours[peer_key] = conn
                with self.mp_lock:
                    self.missed_pings[peer_key] = 0
                self.log(f"Inbound HELLO from {peer_key}")
            elif t == "GOSSIP":
                self._on_gossip(msg, conn)
            elif t == "PING":
                send_msg(conn, {"type": "PONG",
                                "from_ip": self.host, "from_port": self.port})
            elif t == "PONG":
                if peer_key:
                    with self.mp_lock:
                        self.missed_pings[peer_key] = 0
                    with self.pong_lock:
                        self.pong_received.add(peer_key)
            elif t == "SUSPECT_REQUEST":
                self._on_suspect_request(msg, conn)
            elif t == "SUSPECT_RESPONSE":
                self._on_suspect_response(msg)
            elif t == "DEAD_CONFIRMED":
                self._on_dead_confirmed((msg["dead_ip"], int(msg["dead_port"])))
                
        if peer_key:
            with self.nbr_lock:
                self.neighbours.pop(peer_key, None)
        try:
            conn.close()
        except Exception:
            pass
        
    # Seed registration (serial, no races) 
    def _register_and_collect(self, need: int):
        """
        For each seed (shuffled):
          1. TCP connect
          2. Send REGISTER_REQUEST
          3. recv REGISTER_RESPONSE  (contains peer list)
          4. Hand socket to background listener

        Returns (registered_seeds, union_peer_entries).
        """
        candidates= list(self.all_seeds)
        random.shuffle(candidates)

        registered: list[tuple] = []
        peer_map: dict[tuple, int] = {}  # (ip,port)->best known degree

        for (sip, sport) in candidates:
            if len(registered) >= self.n_seeds:
                break  # tried all seeds
            sock = self._tcp_connect(sip, sport, retries=4)
            if sock is None:
                self.log(f"Cannot reach seed {sip}:{sport}")
                continue

            self.log(f"Registering with seed {sip}:{sport}")
            ok = send_msg(sock, {"type": "REGISTER_REQUEST",
                                 "ip": self.host, "port": self.port})
            if not ok:
                sock.close()
                continue

            resp= recv_msg(sock)
            if not resp or resp.get("status") != "ok":
                self.log(f"Registration rejected/failed at {sip}:{sport}: {resp}")
                sock.close()
                continue

            self.log(f"Registered with seed {sip}:{sport}")
            pl = resp.get("peer_list", [])
            self.log(f"Peer list from {sip}:{sport}: {pl}")
            for p in pl:
                key = (p["ip"], int(p["port"]))
                if key != (self.host, self.port):
                    peer_map[key] = max(peer_map.get(key, 0), p.get("degree", 0))

            registered.append((sip, sport))
            with self.seed_lock:
                self.seed_socks[(sip, sport)] = sock
            # NOW hand off to bg listener(no more synch reads on sock)
            threading.Thread(target=self._listen_seed,
                             args=(sock, (sip, sport)), daemon=True).start()

        self.log(f"Registered with {len(registered)}/{self.quorum} required seeds")
        entries = [{"ip": ip, "port": port, "degree": deg}
                   for (ip, port), deg in peer_map.items()]
        return registered,entries

    def _listen_seed(self, sock: socket.socket, seed_key: tuple):
        """Bg reader for a seed socket (DEAD_CONFIRMED etc.)"""
        while True:
            msg= recv_msg(sock)
            if msg is None:
                self.log(f"Seed {seed_key} connection closed")
                break
            t= msg.get("type", "")
            if t == "DEAD_CONFIRMED":
                self._on_dead_confirmed((msg["dead_ip"], int(msg["dead_port"])))
            # Other seed→peer message types can be added here

    def _tcp_connect(self, ip: str, port: int, retries: int = 4):
        for i in range(retries):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect((ip, port))
                s.settimeout(None)
                return s
            except Exception:
                time.sleep(1 + i)
        return None

    # Neighbour selection (power law preferential attachment) 
    def _select_neighbours(self,peer_list: list[dict]) -> list[tuple]:
        if not peer_list:
            return []
        n = len(peer_list)
        # No. of neighbours drawn from Pareto distribution (power law)
        k = min(n, max(1, int(random.paretovariate(2.5))))

        # Weights ∝ deg+1  (preferential attachment)
        weights = [p.get("degree", 0) + 1.0 for p in peer_list]
        total = sum(weights)
        probs = [w / total for w in weights]

        chosen = []
        remaining_idx = list(range(n))
        rem_probs = list(probs)

        for _ in range(k):
            if not remaining_idx:
                break
            r= random.random()
            cum= 0.0
            picked = len(remaining_idx) - 1  # fallback: last
            for i,prob in enumerate(rem_probs):
                cum +=prob
                if r <= cum:
                    picked = i
                    break
            orig= remaining_idx[picked]
            chosen.append((peer_list[orig]["ip"], int(peer_list[orig]["port"])))
            remaining_idx.pop(picked)
            rem_probs.pop(picked)
            s= sum(rem_probs) or 1.0
            rem_probs = [p / s for p in rem_probs]

        return chosen

    # Outbound peer conn
    def _connect_to_neighbours(self, neighbours: list[tuple]):
        for (nip, nport) in neighbours:
            if (nip, nport) == (self.host,self.port):
                continue
            threading.Thread(target=self._connect_one_neighbour,
                             args=(nip, nport), daemon=True).start()

    def _connect_one_neighbour(self, nip: str, nport: int):
        sock = self._tcp_connect(nip, nport, retries=5)
        if sock is None:
            self.log(f"Could not connect to neighbour {nip}:{nport}")
            return
        send_msg(sock, {"type": "HELLO", "ip": self.host, "port": self.port})
        peer_key = (nip, nport)
        with self.nbr_lock:
            self.neighbours[peer_key] = sock
        with self.mp_lock:
            self.missed_pings[peer_key] = 0
        self.log(f"Connected to neighbour {nip}:{nport}")
        self._listen_neighbour(sock, peer_key)

    def _listen_neighbour(self, sock: socket.socket, peer_key: tuple):
        """Outbound neighbour receive loop """
        while True:
            msg = recv_msg(sock)
            if msg is None:
                self.log(f"Lost connection to neighbour {peer_key}")
                
                self._start_suspicion(peer_key)
                
                with self.nbr_lock:
                    self.neighbours.pop(peer_key, None)
                break
            
            t = msg.get("type", "")
            if t == "GOSSIP":
                self._on_gossip(msg, sock)
            elif t == "PING":
                send_msg(sock, {"type": "PONG",
                                "from_ip": self.host, "from_port": self.port})
            elif t == "PONG":
                with self.mp_lock:
                    self.missed_pings[peer_key] = 0
                with self.pong_lock:
                    self.pong_received.add(peer_key)
            elif t == "SUSPECT_REQUEST":
                self._on_suspect_request(msg, sock)
            elif t == "SUSPECT_RESPONSE":
                self._on_suspect_response(msg)
            elif t == "DEAD_CONFIRMED":
                self._on_dead_confirmed((msg["dead_ip"], int(msg["dead_port"])))

    # Gossip
    def _gossip_loop(self):
        """Generate one gossip message every 5 s, up to MAX_GOSSIP."""
        time.sleep(2)  # allow neighbour connections to stabilise
        while True:
            with self.gc_lock:
                if self.gossip_count >= self.MAX_GOSSIP:
                    break
                self.gossip_count += 1
                seq = self.gossip_count

            ts = time.time()
            content = f"{ts:.6f}:{self.host}:{seq}"
            h = hashlib.sha256(content.encode()).hexdigest()

            self.log(f"Generated gossip #{seq}: {content}")
            with self.ml_lock:
                self.ml.add(h)

            self._broadcast({"type": "GOSSIP", "content": content,
                             "hash": h, "origin_ip": self.host,
                             "origin_port": self.port}, exclude=None)
            time.sleep(self.GOSSIP_INTERVAL)

    def _on_gossip(self, msg: dict, sender_sock: socket.socket):
        h = msg.get("hash") or hashlib.sha256(
            msg.get("content", "").encode()).hexdigest()

        with self.ml_lock:
            if h in self.ml:
                return  # duplicate or drop
            self.ml.add(h)

        content = msg.get("content", "")
        origin = f"{msg.get('origin_ip')}:{msg.get('origin_port')}"
        self.log(f"GOSSIP (first time): '{content}'  from {origin}  ts={time.time():.3f}")

        # Fwd to all neighbours except the sender
        fwd = dict(msg)
        fwd["sender_ip"] = self.host
        fwd["sender_port"] = self.port
        self._broadcast(fwd, exclude=sender_sock)

    def _broadcast(self, msg: dict, exclude: socket.socket | None):
        with self.nbr_lock:
            targets = list(self.neighbours.values())
        for s in targets:
            if s is not exclude:
                send_msg(s, msg)

    # Liveness/Ping 
    def _liveness_loop(self):
        time.sleep(5)   # let gossip start first
        while True:
            # Clear pong_received set at start of round
            with self.pong_lock:
                self.pong_received.clear()

            with self.nbr_lock:
                targets = list(self.neighbours.keys())

            for peer_key in targets:
                # TCP PING
                with self.nbr_lock:
                    sock = self.neighbours.get(peer_key)
                if sock:
                    ok = send_msg(sock, {"type": "PING",
                                         "from_ip": self.host,
                                         "from_port": self.port})
                    if not ok:
                        self._miss(peer_key)

                # System ICMP ping (extra check)
                icmp_ok = system_ping(peer_key[0])
                if not icmp_ok:
                    self._miss(peer_key)

            # Wait for PONGs (half the ping interval)
            time.sleep(self.PING_INTERVAL // 2)

            # Check which neighbours did NOT pong back
            with self.nbr_lock:
                still_alive = list(self.neighbours.keys())
            with self.pong_lock:
                ponged = set(self.pong_received)

            for peer_key in still_alive:
                if peer_key not in ponged:
                    self._miss(peer_key)
                
                else: # reset counter
                    with self.mp_lock:
                        self.missed_pings[peer_key] =0

            time.sleep(self.PING_INTERVAL // 2)

    def _miss(self, peer_key: tuple):
        with self.mp_lock:
            self.missed_pings[peer_key] = self.missed_pings.get(peer_key, 0) + 1
            count = self.missed_pings[peer_key]
        if count >= self.PING_MISS_THRESH:
            self._start_suspicion(peer_key)

    #Suspicion / Dead node
    def _start_suspicion(self, suspect: tuple):
        with self.susp_lock:
            if suspect in self.suspected:
                return
            self.suspected[suspect] = {"confirmations": {self.id}, "reported": False}
        self.log(f"SUSPICION started for {suspect}")

        req = {"type": "SUSPECT_REQUEST",
               "suspect_ip": suspect[0], "suspect_port": suspect[1],
               "requester_ip": self.host, "requester_port": self.port}

        with self.nbr_lock:
            peers = [(k, s) for k, s in self.neighbours.items() if k != suspect]

        for _, sock in peers:
            send_msg(sock, req)

        threading.Thread(target=self._suspicion_timeout,
                         args=(suspect,), daemon=True).start()

    def _on_suspect_request(self, msg: dict, conn: socket.socket):
        suspect = (msg["suspect_ip"], int(msg["suspect_port"]))
        
        # ICMP ping fails for localhost testing. Use a fast TCP port-knock instead.
        alive = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(suspect)
            s.close()
            alive = True
        except Exception:
            pass # Connection refused means the peer is actually dead

        self.log(f"SUSPECT_REQUEST for {suspect} → ping={'alive' if alive else 'dead'}")
        send_msg(conn, {"type": "SUSPECT_RESPONSE",
                        "suspect_ip": suspect[0], "suspect_port": suspect[1],
                        "alive": alive,
                        "responder_ip": self.host, "responder_port": self.port})

    def _on_suspect_response(self, msg: dict):
        suspect = (msg["suspect_ip"], int(msg["suspect_port"]))
        alive = msg.get("alive", True)
        responder = f"{msg['responder_ip']}:{msg['responder_port']}"

        with self.susp_lock:
            entry = self.suspected.get(suspect)
            if not entry or entry["reported"]:
                return
            if not alive:
                entry["confirmations"].add(responder)
            cnt = len(entry["confirmations"])

        self.log(f"SUSPECT_RESPONSE from {responder} for {suspect}: alive={alive} confirms={cnt}")

        with self.nbr_lock:
            total = len(self.neighbours)
        peer_quorum = max(1, (total // 2) + 1)

        if cnt >= peer_quorum:
            with self.susp_lock:
                entry = self.suspected.get(suspect)
                if not entry or entry["reported"]:
                    return
                entry["reported"] = True
            self._report_dead(suspect)

    def _suspicion_timeout(self, suspect: tuple):
        time.sleep(self.SUSPECT_TIMEOUT)
        with self.susp_lock:
            entry = self.suspected.get(suspect)
            if entry and not entry["reported"]:
                self.log(f"Suspicion TIMEOUT for {suspect} — no peer quorum, cancelling")
                del self.suspected[suspect]

    def _report_dead(self, dead: tuple):
        dead_ip, dead_port = dead
        ts = time.time()
        self.log(f"DEAD_REPORT: Dead Node:{dead_ip}:{dead_port}:{ts:.6f}:{self.host}")
        msg = {"type": "DEAD_REPORT",
               "dead_ip": dead_ip, "dead_port": dead_port,
               "timestamp": ts, "reporter": self.id}
        with self.seed_lock:
            seeds = list(self.seed_socks.values())
        for s in seeds:
            send_msg(s, msg)

    def _on_dead_confirmed(self, dead_key: tuple):
        self.log(f"DEAD_CONFIRMED for {dead_key} — removing from neighbours")
        with self.nbr_lock:
            sock = self.neighbours.pop(dead_key, None)
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        with self.susp_lock:
            self.suspected.pop(dead_key, None)
        with self.mp_lock:
            self.missed_pings.pop(dead_key, None)


# Entry point
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python peer.py <IP> <Port> [config.csv]")
        sys.exit(1)
    cfg= sys.argv[3] if len(sys.argv) > 3 else "config.csv"
    node= PeerNode(sys.argv[1], int(sys.argv[2]), cfg)
    node.start()
