#!/usr/bin/env python3
import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


BOT_ROOT = (
    Path(__file__).resolve().parents[1]
    / "home"
    / "telegram-bot"
)
MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "home"
    / "telegram-bot"
    / "database.py"
)
sys.path.insert(0, str(BOT_ROOT))
os.environ.setdefault("DB_ENCRYPTION_KEY", "3JHsU4fDVrHHyE0MhaYeUi9fL7T8zed7tJpOWilgycg=")
SPEC = importlib.util.spec_from_file_location("vpn_bot_database", MODULE_PATH)
database = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(database)


class TrafficStatsDatabaseTests(unittest.TestCase):
    def _make_db(self):
        tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(tmpdir.name) / "vpn_bot.db"
        db = database.Database(str(db_path))
        asyncio.run(db.init())
        return tmpdir, db

    def _insert_client(
        self,
        db,
        chat_id: str,
        username: str,
        first_name: str,
    ) -> None:
        conn = db._conn()
        try:
            conn.execute(
                "INSERT INTO clients (chat_id, username, first_name) VALUES (?, ?, ?)",
                (chat_id, username, first_name),
            )
            conn.commit()
        finally:
            conn.close()

    def test_init_creates_traffic_tables(self) -> None:
        tmpdir, db = self._make_db()
        try:
            conn = db._conn()
            try:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
            finally:
                conn.close()
        finally:
            tmpdir.cleanup()

        self.assertIn("device_traffic_state", tables)
        self.assertIn("device_traffic_samples", tables)

    def test_record_traffic_snapshot_tracks_deltas_and_resets(self) -> None:
        tmpdir, db = self._make_db()
        try:
            self._insert_client(db, "100", "tester", "Tester")
            asyncio.run(
                db.add_device(
                    "100",
                    "Phone",
                    "wg",
                    public_key="pubkey-1",
                    private_key="privkey-1",
                )
            )
            first = asyncio.run(
                db.record_traffic_snapshot(
                    [
                        {
                            "public_key": "pubkey-1",
                            "interface": "wg1",
                            "last_handshake": 1712790000,
                            "rx_bytes": 100,
                            "tx_bytes": 300,
                        }
                    ],
                    sampled_at="2026-04-11 00:00:00",
                )
            )
            second = asyncio.run(
                db.record_traffic_snapshot(
                    [
                        {
                            "public_key": "pubkey-1",
                            "interface": "wg1",
                            "last_handshake": 1712790300,
                            "rx_bytes": 150,
                            "tx_bytes": 450,
                        }
                    ],
                    sampled_at="2026-04-11 00:05:00",
                )
            )
            third = asyncio.run(
                db.record_traffic_snapshot(
                    [
                        {
                            "public_key": "pubkey-1",
                            "interface": "wg1",
                            "last_handshake": 1712790600,
                            "rx_bytes": 10,
                            "tx_bytes": 20,
                        }
                    ],
                    sampled_at="2026-04-11 00:10:00",
                )
            )
            report = asyncio.run(db.get_client_traffic_totals())
        finally:
            tmpdir.cleanup()

        self.assertEqual(first["matched_devices"], 1)
        self.assertEqual(first["delta_rows"], 1)
        self.assertEqual(second["delta_rows"], 1)
        self.assertEqual(third["counter_resets"], 1)
        self.assertEqual(len(report), 1)
        client = report[0]
        self.assertEqual(client["chat_id"], "100")
        self.assertEqual(client["total_download_bytes"], 450)
        self.assertEqual(client["total_upload_bytes"], 150)
        self.assertEqual(client["download_24h"], 450)
        self.assertEqual(client["upload_24h"], 150)
        self.assertEqual(len(client["devices"]), 1)
        self.assertEqual(client["devices"][0]["device_name"], "Phone")
        self.assertEqual(client["devices"][0]["total_download_bytes"], 450)
        self.assertEqual(client["devices"][0]["total_upload_bytes"], 150)

    def test_totals_include_clients_without_runtime_peers(self) -> None:
        tmpdir, db = self._make_db()
        try:
            self._insert_client(db, "200", "silent", "Silent")
            asyncio.run(
                db.add_device(
                    "200",
                    "Laptop",
                    "awg",
                    public_key="pubkey-2",
                    private_key="privkey-2",
                )
            )
            report = asyncio.run(db.get_client_traffic_totals())
        finally:
            tmpdir.cleanup()

        self.assertEqual(len(report), 1)
        client = report[0]
        self.assertEqual(client["chat_id"], "200")
        self.assertEqual(client["total_download_bytes"], 0)
        self.assertEqual(client["total_upload_bytes"], 0)
        self.assertEqual(client["devices"][0]["last_seen_at"], None)
