"""Optional development preview entry point; the app itself needs only server.py."""
import argparse
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--host', choices=['127.0.0.1', 'localhost'], default='127.0.0.1')
parser.add_argument('--port', type=int, default=8765)
parser.add_argument('--strictPort', action='store_true')
args = parser.parse_args()
subprocess.run([sys.executable, str(Path(__file__).with_name('server.py')), '--port', str(args.port), '--data-dir', str(Path(__file__).with_name('.preview-data'))], check=True)
