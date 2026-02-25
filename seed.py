"""
 Seed Node for Gossip based P2P Network

Each seed dials every other seed whose port > its own
(so each pair has exactly 1 conn. : lower port seed dials 
higher port one)

The dialling seed:
  1. Sends SEED_HELLO to identify itself
  2. Stores the outbound socket in seed_channels
  3. Reads from that socket in a loop (_route_msg)

The accepting seed:
  1. Receives SEED_HELLO in _handle_conn
  2. Stores the accepted socket in seed_channels
  3. Reads from it in the _handle_connection loop (_route_message)

So every seed2seed socket is read by BOTH ends.Sending a proposal on
seed_channels[X] writes into X's reader loop and X's reply comes back on
the same socket to the proposer's reader loop.

Protocol: 4 byte big endian length + JSON payload.
"""

import sys
import os
import csv
import json
import socket
import struct
import threading
import time
import logging

# Framing helpers
HEADER_SIZE = 4

def send_msg(sock: socket.socket, data: dict) -> bool:
    """Serialize data to JSON, prefix with 4 byte len send atomically"""
    try:
        payload = json.dumps(data).encode("utf-8")
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        return True
    except Exception:
        return False


def recv_msg(sock: socket.socket):
    """Receive 1 len prefixed JSON msg. Returns None on error/close"""
    try:
        hdr = _recv_exact(sock, HEADER_SIZE)
        if not hdr:
            return None
        n= struct.unpack(">I", hdr)[0]
        body= _recv_exact(sock, n)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _recv_exact(sock: socket.socket, n: int):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

# SeedNode is consensus based peer registration & dead node removal
class SeedNode:
    """
    peer_list       : dict[(ip,port)] -> {degree, registered_at}
    pending_reg     : dict[req_id]    -> {peer, votes, conn, decided}
    dead_reports    : dict[(ip,port)] -> set of reporter strings
    pending_rem     : dict[req_id]    -> {peer, votes, decided}
    seed_channels   : dict[seed_id]   -> socket  (one per peer seed)
    """

    def __init__(self, host: str, port: int, config_path: str = "config.csv"):
        self.host =host
        self.port =port
        self.id   = f"{host}:{port}"

        self.all_seeds: list = []
        self._load_config(config_path)
        self.n_seeds = len(self.all_seeds)
        self.quorum  = (self.n_seeds // 2) + 1

        # Peer membership
        self.peer_list: dict = {}
        self.pl_lock = threading.Lock()

        # Consensus is registration
        self.pending_reg: dict = {}
        self.pr_lock = threading.Lock()

        # Dead node buffering
        self.dead_reports: dict = {}
        self.dr_lock = threading.Lock()

        # Consensus  removal
        self.pending_rem: dict = {}
        self.prem_lock = threading.Lock()

        # Seed2seed channels (both accepted & dialled sockets stored here)
        self.seed_channels: dict = {}
        self.sc_lock = threading.Lock()

        self._setup_logger()
        self.log(f"Initialized  n_seeds={self.n_seeds}  quorum={self.quorum}")

    # Config 

    def _load_config(self, path: str):
        if not os.path.exists(path):
            print(f"[ERROR] config.csv not found: {path}")
            sys.exit(1)
        with open(path, newline="") as f:
            for row in csv.reader(f):
                row = [c.strip() for c in row]
                if len(row) >= 2:
                    self.all_seeds.append((row[0], int(row[1])))

    # Logger 
    def _setup_logger(self):
        self.logger = logging.getLogger(f"seed_{self.port}")
        self.logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s [SEED %(name)s] %(message)s",
                                datefmt="%H:%M:%S")
        fh = logging.FileHandler(f"outputfile_seed_{self.port}.txt", mode="a")
        fh.setFormatter(fmt)
        self.logger.addHandler(fh)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        self.logger.addHandler(ch)

    def log(self, msg: str):
        self.logger.info(msg)

    # Startup
    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(100)
        self.log(f"Listening on {self.host}:{self.port}")

        threading.Thread(target=self._accept_loop, args=(srv,),daemon=True).start()
        # Wait for all seeds to start listening then dial higher port seeds
        time.sleep(2)
        self._dial_higher_port_seeds()

        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            self.log("Shutting down.")
            srv.close()

    # Accept loop 
    def _accept_loop(self, srv: socket.socket):
        while True:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle_connection,
                                 args=(conn,), daemon=True).start()
            except Exception:
                break

    #Dial peer seeds (lower port accepts, higher port is dialled)
    def _dial_higher_port_seeds(self):
        """Dial only seeds with port > self.port (avoids duplicate pairs)."""
        for (ip, port) in self.all_seeds:
            if port > self.port:
                threading.Thread(target=self._dial_one_seed,
                                 args=(ip, port), daemon=True).start()

    def _dial_one_seed(self, ip: str, port: int):
        """Connect to a peer seed, register the socket, and read from it."""
        peer_id = f"{ip}:{port}"
        for attempt in range(15):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect((ip, port))
                s.settimeout(None)
                # Identify ourselves
                send_msg(s, {"type": "SEED_HELLO", "seed_id": self.id})
                with self.sc_lock:
                    self.seed_channels[peer_id] = s
                self.log(f"Dialled seed {ip}:{port}")
                # Read loop is receive proposals/votes sent back to us
                while True:
                    msg = recv_msg(s)
                    if msg is None:
                        break
                    self._route_message(msg, s)
                # Disconnected
                with self.sc_lock:
                    if self.seed_channels.get(peer_id) is s:
                        del self.seed_channels[peer_id]
                self.log(f"Lost connection to seed {ip}:{port} — will retry")
            except Exception:
                pass
            time.sleep(3 + attempt)

    # Connection handler 
    def _handle_connection(self, conn: socket.socket):
        """Receive msgs from 1 accepted connec (peer or seed)."""
        peer_seed_id = None
        while True:
            msg = recv_msg(conn)
            if msg is None:
                break
            t = msg.get("type", "")
            if t == "SEED_HELLO":
                # A lower-port seed connected to us
                peer_seed_id = msg["seed_id"]
                with self.sc_lock:
                    self.seed_channels[peer_seed_id] = conn
                self.log(f"Seed {peer_seed_id} connected (inbound)")
            else:
                self._route_message(msg, conn)

        try:
            conn.close()
        except Exception:
            pass
        if peer_seed_id:
            with self.sc_lock:
                if self.seed_channels.get(peer_seed_id) is conn:
                    del self.seed_channels[peer_seed_id]

    # Message routing
    def _route_message(self, msg: dict, conn: socket.socket):
        """Dispatch received msg to the correct handler"""
        t = msg.get("type", "")
        if   t == "REGISTER_REQUEST":  self._on_register_request(msg, conn)
        elif t == "REGISTER_PROPOSAL": self._on_register_proposal(msg, conn)
        elif t == "REGISTER_VOTE":     self._on_register_vote(msg)
        elif t == "PEER_LIST_REQUEST": self._on_peer_list_request(msg, conn)
        elif t == "DEAD_REPORT":       self._on_dead_report(msg)
        elif t == "DEAD_PROPOSAL":     self._on_dead_proposal(msg, conn)
        elif t == "DEAD_VOTE":         self._on_dead_vote(msg)
        elif t == "DEAD_CONFIRMED":
            # Another seed committed removal before us, sync our PL
            dead_key = (msg["dead_ip"], int(msg["dead_port"]))
            with self.pl_lock:
                if dead_key in self.peer_list:
                    del self.peer_list[dead_key]
            self.log(f"Synced removal of {dead_key} via DEAD_CONFIRMED from peer seed")
        # Unknown messages are silently ignored

    # Broadcast helpers
    def _broadcast_to_seeds(self, msg: dict):
        """Send msg to all connected peer seeds."""
        with self.sc_lock:
            targets = list(self.seed_channels.values())
        for s in targets:
            send_msg(s, msg)

    # Registration consensus
    def _on_register_request(self, msg: dict, conn: socket.socket):
        """Peer asks to join. This seed becomes the proposer."""
        peer_ip, peer_port = msg["ip"], int(msg["port"])
        peer_key = (peer_ip, peer_port)

        # Already registered?
        with self.pl_lock:
            if peer_key in self.peer_list:
                self.log(f"REGISTER_REQUEST {peer_key} — already in PL, ACK")
                send_msg(conn, {"type": "REGISTER_RESPONSE", "status": "ok",
                                "peer_list": self._pl_excl(peer_key)})
                return

        req_id = f"reg_{peer_ip}_{peer_port}_{time.monotonic():.4f}"
        self.log(f"REGISTER_REQUEST {peer_key}  req_id={req_id}")
        with self.pr_lock:
            self.pending_reg[req_id] = {
                "peer": peer_key,
                "votes": {self.id: True},   # self vote
                "conn": conn,
                "decided": False,
            }
        with self.sc_lock:
            n_ch = len(self.seed_channels)
        self.log(f"Broadcasting REGISTER_PROPOSAL to {n_ch} peer seed(s)  req_id={req_id}")
        self._broadcast_to_seeds({
            "type": "REGISTER_PROPOSAL",
            "req_id": req_id,
            "peer_ip": peer_ip,
            "peer_port": peer_port,
            "proposer": self.id,
        })
        self._check_reg_quorum(req_id)   # may already pass if n_seeds==1
        threading.Thread(target=self._reg_timeout, args=(req_id,), daemon=True).start()

    def _on_register_proposal(self, msg: dict, conn: socket.socket):
        """Non-proposer seed receives proposal — vote YES and reply on same socket."""
        req_id   = msg["req_id"]
        proposer = msg.get("proposer", "?")
        peer_key = (msg["peer_ip"], int(msg["peer_port"]))
        self.log(f"REGISTER_PROPOSAL req_id={req_id} peer={peer_key} from={proposer} → YES")
        send_msg(conn, {
            "type":    "REGISTER_VOTE",
            "req_id":  req_id,
            "voter":   self.id,
            "vote":    True,
        })

    def _on_register_vote(self, msg: dict):
        """Proposer accumulates votes."""
        req_id, voter, vote = msg["req_id"], msg["voter"], msg["vote"]
        self.log(f"REGISTER_VOTE req_id={req_id} voter={voter} vote={vote}")
        with self.pr_lock:
            entry = self.pending_reg.get(req_id)
            if not entry or entry["decided"]:
                return
            entry["votes"][voter] = vote
        self._check_reg_quorum(req_id)

    def _check_reg_quorum(self, req_id: str):
        with self.pr_lock:
            entry = self.pending_reg.get(req_id)
            if not entry or entry["decided"]:
                return
            yes = sum(1 for v in entry["votes"].values() if v)
            no  = sum(1 for v in entry["votes"].values() if not v)
            if yes >= self.quorum:
                entry["decided"] = True
                peer_key = entry["peer"]
                conn     = entry["conn"]
            elif no > (self.n_seeds - self.quorum):
                entry["decided"] = True
                conn = entry["conn"]
                self.log(f"Registration REJECTED req_id={req_id}")
                send_msg(conn, {"type": "REGISTER_RESPONSE", "status": "rejected"})
                return
            else:
                return

        # Commit
        with self.pl_lock:
            self.peer_list[peer_key] = {"degree": 0, "registered_at": time.time()}
        self.log(f"Peer {peer_key} REGISTERED  yes={yes}  PL_size={len(self.peer_list)}")
        send_msg(conn, {"type": "REGISTER_RESPONSE", "status": "ok",
                        "peer_list": self._pl_excl(peer_key)})

    def _reg_timeout(self, req_id: str):
        time.sleep(10)
        with self.pr_lock:
            entry = self.pending_reg.get(req_id)
            if not entry or entry["decided"]:
                return
            entry["decided"] = True
            conn = entry["conn"]
        self.log(f"Registration TIMEOUT req_id={req_id}")
        send_msg(conn, {"type": "REGISTER_RESPONSE", "status": "timeout"})

    # Peer list 
    def _on_peer_list_request(self, msg: dict, conn: socket.socket):
        requester = (msg.get("ip", ""), int(msg.get("port", 0)))
        self.log(f"PEER_LIST_REQUEST from {requester}")
        send_msg(conn, {"type": "PEER_LIST_RESPONSE",
                        "peer_list": self._pl_excl(requester)})

    def _pl_serialised(self) -> list:
        with self.pl_lock:
            return [{"ip": ip, "port": port, "degree": m["degree"]}
                    for (ip, port), m in self.peer_list.items()]

    def _pl_excl(self, exclude: tuple) -> list:
        with self.pl_lock:
            return [{"ip": ip, "port": port, "degree": m["degree"]}
                    for (ip, port), m in self.peer_list.items()
                    if (ip, port) != exclude]

    # Dead node consensus 
    def _on_dead_report(self, msg: dict):
        dead_key = (msg["dead_ip"], int(msg["dead_port"]))
        reporter = msg["reporter"]
        self.log(f"DEAD_REPORT  dead={dead_key}  reporter={reporter}")
        # The peers already achieved consensus, so the seed only needs ONE report 
        # to trigger the seed level vote.
        with self.dr_lock:
            if dead_key not in self.dead_reports:
                self.dead_reports[dead_key] = set()
            
            # If we have already proposed this recently, don't spam the network
            if reporter in self.dead_reports[dead_key]:
                return
                
            self.dead_reports[dead_key].add(reporter)
            
        self._propose_removal(dead_key)

    def _propose_removal(self, dead_key: tuple):
        with self.pl_lock:
            if dead_key not in self.peer_list:
                return
        req_id = f"rem_{dead_key[0]}_{dead_key[1]}_{time.monotonic():.4f}"
        self.log(f"DEAD_PROPOSAL req_id={req_id}  dead={dead_key}")
        with self.prem_lock:
            self.pending_rem[req_id] = {
                "peer":    dead_key,
                "votes":   {self.id: True},
                "decided": False,
            }
        self._broadcast_to_seeds({
            "type":     "DEAD_PROPOSAL",
            "req_id":   req_id,
            "dead_ip":  dead_key[0],
            "dead_port":dead_key[1],
            "proposer": self.id,
        })
        self._check_rem_quorum(req_id)
        threading.Thread(target=self._rem_timeout, args=(req_id,), daemon=True).start()

    def _on_dead_proposal(self, msg: dict, conn: socket.socket):
        req_id = msg["req_id"]
        self.log(f"DEAD_PROPOSAL received req_id={req_id} → YES")
        send_msg(conn, {
            "type":     "DEAD_VOTE",
            "req_id":   req_id,
            "voter":    self.id,
            "vote":     True,
            "dead_ip":  msg["dead_ip"],
            "dead_port":msg["dead_port"],
        })

    def _on_dead_vote(self, msg: dict):
        req_id, voter, vote = msg["req_id"], msg["voter"], msg["vote"]
        self.log(f"DEAD_VOTE req_id={req_id} voter={voter} vote={vote}")
        with self.prem_lock:
            entry = self.pending_rem.get(req_id)
            if not entry or entry["decided"]:
                return
            entry["votes"][voter] = vote
        self._check_rem_quorum(req_id)

    def _check_rem_quorum(self, req_id: str):
        with self.prem_lock:
            entry = self.pending_rem.get(req_id)
            if not entry or entry["decided"]:
                return
            yes = sum(1 for v in entry["votes"].values() if v)
            if yes >= self.quorum:
                entry["decided"] = True
                dead_key = entry["peer"]
            else:
                return
        with self.pl_lock:
            removed = dead_key in self.peer_list
            if removed:
                del self.peer_list[dead_key]
        if removed:
            self.log(f"Peer {dead_key} REMOVED from PL  req_id={req_id}  PL_size={len(self.peer_list)}")
            self._broadcast_to_seeds({
                "type":     "DEAD_CONFIRMED",
                "dead_ip":  dead_key[0],
                "dead_port":dead_key[1],
            })

    def _rem_timeout(self, req_id: str):
        time.sleep(10)
        with self.prem_lock:
            entry = self.pending_rem.get(req_id)
            if entry and not entry["decided"]:
                entry["decided"] = True
                self.log(f"Removal TIMEOUT req_id={req_id}")


# Entry point
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python seed.py <IP> <Port> [config.csv]")
        sys.exit(1)
    cfg= sys.argv[3] if len(sys.argv) > 3 else "config.csv"
    SeedNode(sys.argv[1], int(sys.argv[2]), cfg).start()
