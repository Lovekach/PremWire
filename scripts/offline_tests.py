"""Finite public CI suite; network, credentials and child processes blocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest

root = Path(sys.argv[1]).resolve()
os.chdir(root)
sys.path[:0] = [str(root), str(root / 'src')]
for key in list(os.environ):
    if key.startswith(('TELEGRAM_', 'X_', 'VK_', 'PREMWIRE_', 'OLLAMA_', 'API_SPORTS', 'EDITOR_')):
        os.environ.pop(key, None)
os.environ['PREMWIRE_RUNTIME'] = str(root / '.runtime')

def denied(*args, **kwargs):
    raise RuntimeError('OFFLINE_NETWORK_OR_PROCESS_BLOCKED')

socket.socket.connect = denied
socket.create_connection = denied
import truststore
import pydantic
import platform
platform.platform()
platform.machine()

class BlockedPopen(subprocess.Popen):
    def __init__(self, *args, **kwargs): denied()

subprocess.Popen = BlockedPopen
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'), top_level_dir=str(root))
    result = unittest.TestResult()
    suite.run(result)
print(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
                  'errors': len(result.errors), 'skipped': len(result.skipped)}))
raise SystemExit(0 if result.wasSuccessful() and result.testsRun else 1)
