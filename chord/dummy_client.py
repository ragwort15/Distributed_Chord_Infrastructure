"""
DummyClient — daemon thread that sends simulated file requests to the
local Chord node every 20-30 seconds, demonstrating DHT-based file routing.
"""

import random
import threading
import time
import logging
import requests as _requests

logger = logging.getLogger(__name__)

# Pool of dummy filenames the client will request at random
DUMMY_FILES = [
    "report_q1.pdf",    "report_q2.pdf",    "report_q3.pdf",    "report_q4.pdf",
    "dataset_train.csv","dataset_test.csv", "model_weights.bin","config.yaml",
    "logs_2024.tar.gz", "backup_jan.zip",   "invoice_001.pdf",  "invoice_002.pdf",
    "presentation.pptx","image_001.png",    "image_002.jpg",    "video_demo.mp4",
    "source_code.tar",  "readme.md",        "schema.sql",       "users.json",
    "audit_log.txt",    "metrics.parquet",  "pipeline.json",    "deploy.sh",
]

# Map extension → rough file-type label shown in the UI
EXT_LABELS = {
    "pdf": "document", "csv": "dataset",  "bin": "binary",
    "yaml": "config",  "gz": "archive",   "zip": "archive",
    "pptx": "slides",  "png": "image",    "jpg": "image",
    "mp4": "video",    "tar": "archive",  "md": "text",
    "sql": "database", "json": "data",    "txt": "text",
    "sh": "script",    "parquet": "dataset",
}


def file_type(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return EXT_LABELS.get(ext, "file")


class DummyClient(threading.Thread):
    """
    Periodically picks a random filename and POSTs /request to the local
    node. The node hashes the name, routes to the responsible Chord node,
    and serves (or creates) the file there.
    """

    def __init__(self, node_address: str, interval_min: float = 20.0,
                 interval_max: float = 30.0):
        super().__init__(daemon=True, name="chord-dummy-client")
        self.node_address  = node_address
        self.interval_min  = interval_min
        self.interval_max  = interval_max
        self._stop         = threading.Event()
        self.requests_sent = 0

    def run(self):
        logger.info(
            f"[DummyClient] Started — requests every {self.interval_min}–"
            f"{self.interval_max}s → {self.node_address}"
        )
        # Small initial delay so the ring can stabilise before first request
        self._stop.wait(5.0)
        while not self._stop.is_set():
            self._send_request()
            delay = random.uniform(self.interval_min, self.interval_max)
            logger.debug(f"[DummyClient] Next request in {delay:.1f}s")
            self._stop.wait(delay)

    def _send_request(self):
        filename = random.choice(DUMMY_FILES)
        try:
            r = _requests.post(
                f"http://{self.node_address}/request",
                json={"filename": filename, "client": "dummy"},
                timeout=6,
            )
            r.raise_for_status()
            data = r.json()
            self.requests_sent += 1
            logger.info(
                f"[DummyClient] '{filename}' → Node {data.get('served_by_node')} "
                f"({data.get('hops', '?')} hop(s)) key={data.get('key_id')}"
            )
        except Exception as e:
            logger.warning(f"[DummyClient] Failed to request '{filename}': {e}")

    def stop(self):
        self._stop.set()
