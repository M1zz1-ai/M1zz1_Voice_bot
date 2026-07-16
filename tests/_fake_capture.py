"""
Fake capture workers for AudioRecorder subprocess tests.

These run in the spawned CHILD process in place of the real `_capture_worker`,
so the tests exercise the parent/child wire protocol and the SIGKILL/respawn
path without a real microphone. Must be importable by name (spawn pickles the
target by module + qualname), hence a standalone module.
"""

import time

import numpy as np


def fake_capture_worker(conn, sample_rate, channels, blocksize):
    """Report ready, then stream synthetic int16 frames until told to stop."""
    conn.send_bytes(b"R" + b"fake-mic")
    n = 0
    try:
        while True:
            if conn.poll(0):
                if conn.recv_bytes()[:1] == b"S":
                    break
            frame = np.full(blocksize, n % 100, dtype=np.int16)
            try:
                conn.send_bytes(b"D" + frame.tobytes())
            except Exception:
                break
            n += 1
            time.sleep(0.02)
    except Exception:
        pass
    try:
        conn.send_bytes(b"X")
    except Exception:
        pass


def never_ready_worker(conn, sample_rate, channels, blocksize):
    """Never reports ready — simulates a mic that won't open, so the parent's
    start() must time out (bounded) instead of hanging forever."""
    time.sleep(30)
