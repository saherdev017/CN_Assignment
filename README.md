# Gossip-Based P2P Network

A Python implementation of a gossip peer-to-peer network with consensus-driven membership management, power-law overlay topology, and two-level dead-node detection.

---

## Files

| File | Description |
|------|-------------|
| `seed.py` | Seed node — manages peer membership via consensus |
| `peer.py` | Peer node — gossip dissemination, liveness detection |
| `config.csv` | List of seed node addresses (IP, Port per line) |
| `outputfile_seed_<port>.txt` | Auto-generated seed log file |
| `outputfile_peer_<port>.txt` | Auto-generated peer log file |

---

## Requirements

- Python 3.10 or later  
- No external packages — only Python standard library is used  
- Works on Windows, Linux, and macOS  
- For cross-machine deployment, update `config.csv` with real IP addresses

---

## Configuration

Edit `config.csv` to specify seed node addresses (one per line):

```
127.0.0.1,5001
127.0.0.1,5002
127.0.0.1,5003
```

The number of seeds `n` is determined by the number of lines. Peers and seeds both read this file to learn about the network.

---

## How to Run (Local Testing with Virtual Environment)

### 1. Set up virtual environment (one time)

```powershell
cd c:\Users\aadit\Downloads\p2p_network
python -m venv venv
.\venv\Scripts\activate
```

### 2. Start Seed Nodes (open 3 separate terminals)

Each seed needs its own terminal. Activate the venv in each:

```powershell
# Terminal 1
.\venv\Scripts\activate
python seed.py 127.0.0.1 5001

# Terminal 2
.\venv\Scripts\activate
python seed.py 127.0.0.1 5002

# Terminal 3
.\venv\Scripts\activate
python seed.py 127.0.0.1 5003
```

### 3. Start Peer Nodes (open additional terminals, one per peer)

```powershell
# Terminal 4
.\venv\Scripts\activate
python peer.py 127.0.0.1 6001

# Terminal 5
.\venv\Scripts\activate
python peer.py 127.0.0.1 6002

# Terminal 6
.\venv\Scripts\activate
python peer.py 127.0.0.1 6003

# Terminal 7
.\venv\Scripts\activate
python peer.py 127.0.0.1 6004
```

> **Note:** Start all seed nodes first, then start peers. Seeds need ~2 seconds to form their mesh before peers arrive.

---

## Testing Scenarios

### Test 1 — Seed startup & peer registration
Start 3 seeds, then 1 peer. Check the seed terminal output and `outputfile_seed_5001.txt` to see:
- `REGISTER_REQUEST` received
- `REGISTER_PROPOSAL` broadcast to other seeds
- `REGISTER_VOTE` from each seed
- Registration committed with quorum

### Test 2 — Gossip dissemination
Start 3 seeds + 3–4 peers. Wait ~60 seconds. Each peer's output file will show:
```
GOSSIP received (first time): '<timestamp>:<IP>:<MsgNo>' from <sender>
```
Each peer generates at most 10 messages, every 5 seconds.

### Test 3 — Dead node detection
1. Start 3 seeds + 3 peers, wait for gossip to begin (~15s)
2. Kill one peer (Ctrl+C in its terminal)
3. Wait ~30–40 seconds

Expected sequence in logs:
1. Remaining peers report missed pings for the dead peer
2. Suspicion is initiated
3. Neighbours are queried via `SUSPECT_REQUEST`
4. Peer-level consensus reached → `DEAD_REPORT` sent to seeds
5. Seeds vote → `DEAD_CONFIRMED` broadcast
6. Dead peer removed from all PLs

---

## Architecture Overview

```
                    ┌─────────────────────────────┐
                    │         Seed Cluster         │
                    │  [Seed 5001]──[Seed 5002]   │
                    │       └──────[Seed 5003]     │
                    │   (consensus mesh — TCP)      │
                    └─────────┬───────────────┬────┘
                              │               │
                      register │               │ register
                              ▼               ▼
              [Peer 6001]──────────────[Peer 6002]
                  │         gossip          │
                  └──────[Peer 6003]────────┘
                              │
                         [Peer 6004]
                  (power-law overlay topology)
```

### Key Design Decisions

| Feature | Mechanism |
|---------|-----------|
| Peer registration | Seed-level Paxos-style majority vote |
| Dead-node detection | Two-level: peer ping + multi-peer confirmation → seed vote |
| Overlay topology | Preferential attachment (Pareto distribution for neighbour count) |
| Gossip dedup | SHA-256 hash stored in Message List (ML) |
| Message framing | 4-byte big-endian length prefix + JSON payload |

### Gossip Message Format

```
<timestamp>:<self.IP>:<Msg#>
e.g., 1740466800.123456:127.0.0.1:3
```

### Dead Node Report Format (peer → seed)

```
Dead Node:<DeadNode.IP>:<DeadNode.Port>:<timestamp>:<reporter.IP>
```

---

## Security Considerations

| Attack | Mitigation |
|--------|-----------|
| False dead-node reports (single peer) | Peer-level quorum required before seed report |
| Sybil registration | Seed quorum consensus before PL commit |
| Collusion among minority seeds | Majority (⌊n/2⌋+1) required — minority cannot force decisions |
| Flood/replay gossip | Hash-based Message List deduplication |
| False suspicion accusation | Multiple independent ICMP pings + peer-level voting |

---

## Output Files

- **`outputfile_seed_<port>.txt`** — Registration proposals, votes, consensus outcomes, dead-node removals  
- **`outputfile_peer_<port>.txt`** — Received peer lists, first-time gossip (with timestamps), confirmed dead-node events

---

## Cross-Machine Deployment

1. Update `config.csv` on **all machines** with the real IP addresses of seed nodes
2. Run `python seed.py <real_IP> <port>` on seed machines
3. Run `python peer.py <real_IP> <port>` on peer machines
4. Ensure the seed ports are open on the seed machines' firewalls
