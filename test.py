import subprocess
import time
import sys
import os

print("Cleaning logs...")
os.system("del outputfile_*.txt 2>nul")

print("Starting seeds...")
s1 = subprocess.Popen([sys.executable, "seed.py", "127.0.0.1", "5001"])
s2 = subprocess.Popen([sys.executable, "seed.py", "127.0.0.1", "5002"])
s3 = subprocess.Popen([sys.executable, "seed.py", "127.0.0.1", "5003"])
time.sleep(5)  # Let mesh form

print("Starting peers...")
p1 = subprocess.Popen([sys.executable, "peer.py", "127.0.0.1", "6001"])
time.sleep(1)
p2 = subprocess.Popen([sys.executable, "peer.py", "127.0.0.1", "6002"])
time.sleep(1)
p3 = subprocess.Popen([sys.executable, "peer.py", "127.0.0.1", "6003"])
time.sleep(1)
p4 = subprocess.Popen([sys.executable, "peer.py", "127.0.0.1", "6004"])

print("Running network for 25 seconds to generate gossip...")
time.sleep(25)

print("Killing node 6004 to test dead-node removal...")
p4.kill()

print("Running network for 40 seconds to allow liveness detection & dead reporting...")
time.sleep(40)

print("Shutting down everything...")
for p in [p1, p2, p3, s1, s2, s3]:
    p.kill()

print("Test complete. Check outputfile_*.txt")
