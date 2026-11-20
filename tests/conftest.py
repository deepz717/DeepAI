"""Fail-closed test safety: staged acceptance never contacts production port 8765."""
import socket

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
_PRODUCTION_PORT_ATTEMPTS = []
_original_connect = socket.socket.connect

def _isolated_connect(sock, address):
    if isinstance(address, tuple) and len(address) >= 2 and address[1] == 8765:
        _PRODUCTION_PORT_ATTEMPTS.append(address)
        raise AssertionError("Staged tests must use isolated Blender port 8766, never production port 8765")
    return _original_connect(sock, address)

socket.socket.connect = _isolated_connect

def pytest_sessionstart(session):
    import config
    if int(config.BLENDER_BRIDGE_PORT) != 8766:
        raise RuntimeError("Staged test config must select isolated port 8766")

def pytest_sessionfinish(session, exitstatus):
    if _PRODUCTION_PORT_ATTEMPTS:
        session.exitstatus = 1
