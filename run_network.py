import subprocess
import time
import os

# Configuration
PYTHON_EXE = "python"  # Change to "python3" if on Linux/Mac
SEEDS = [
    ("127.0.0.1", "5010"),
    ("127.0.0.1", "5011"),
    ("127.0.0.1", "5012"),
]
PEERS = [
    ("127.0.0.1", "6001"),
    ("127.0.0.1", "6002"),
    ("127.0.0.1", "6003"),
    ("127.0.0.1", "6004"),
]

def run_in_new_terminal(command):
    if os.name == 'nt':  # Windows
        subprocess.Popen(f'start cmd /k {command}', shell=True)
    else:  # Linux/Mac (requires xterm or gnome-terminal)
        subprocess.Popen(['gnome-terminal', '--', 'bash', '-c', f'{command}; exec bash'])

print("Starting Seeds...")
for ip, port in SEEDS:
    run_in_new_terminal(f"{PYTHON_EXE} seed.py {ip} {port}")
    time.sleep(1) # Small gap to allow binding

print("Waiting for Seeds to establish consensus...")
time.sleep(5)

print("Starting Peers...")
for ip, port in PEERS:
    run_in_new_terminal(f"{PYTHON_EXE} peer.py {ip} {port}")
    time.sleep(1)

print("Network is running. Check the newly opened windows!")