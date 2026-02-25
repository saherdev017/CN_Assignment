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
127.0.0.1,5011
127.0.0.1,5012
127.0.0.1,5013
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
python seed.py 127.0.0.1 5010

# Terminal 2
.\venv\Scripts\activate
python seed.py 127.0.0.1 5011

# Terminal 3
.\venv\Scripts\activate
python seed.py 127.0.0.1 5012
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

### Test 3 — Dead node detection (Instantaneous)
1. Start 3 seeds + 3 peers, wait for gossip to begin (~15s).
2. Kill one peer (Ctrl+C in its terminal).
3. Observe the immediate network reaction.

**Expected sequence in logs:**
1. **Event-Driven Detection:** Remaining peers instantly detect the broken TCP pipe (no need to wait for ping timeouts).
2. **Suspicion Initiated:** Peers immediately log a local suspicion and halt pings to the dead node.
3. **Peer-Level Consensus:** Neighbours are queried via `SUSPECT_REQUEST`.
4. **Escalation:** Peer-level consensus reached → `DEAD_REPORT` sent to seeds.
5. **Seed-Level Consensus:** Seeds vote → `DEAD_CONFIRMED` broadcast.
6. **Purge:** Dead peer removed from all active PLs across the network.
*(  This entire 6-step consensus pipeline executes in < 2 seconds).*

### Test 4 — Graceful Shutdown
Press `Ctrl+C` on any running Seed or Peer. The application will catch the `KeyboardInterrupt`, close active sockets cleanly, and exit with a `Shutting down.` log rather than throwing a Python traceback.

---

## Architecture Overview

```
                    ┌─────────────────────────────┐
                    │         Seed Cluster         │
                    │  [Seed 5010]──[Seed 5012]   │
                    │       └──────[Seed 5011]     │
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
| Peer Registration | Seed-level Paxos-style majority vote. |
| Dead-Node Detection | Event-driven TCP socket monitoring + ICMP system pings. |
| Two-Tier Consensus | Peer-level confirmation prevents false reports; Seed-level vote prevents unilateral deletions. |
| Overlay Topology | Preferential attachment (Pareto distribution for neighbour count). |
| Gossip Dedup | SHA-256 hash stored in Message List (ML) prevents infinite network loops. |
| Message Framing | 4-byte big-endian length prefix + JSON payloads for reliable stream parsing. |
| Fault Tolerance | `SO_REUSEADDR` prevents `TIME_WAIT` port lockouts; `KeyboardInterrupt` handling ensures graceful node shutdowns. |

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
