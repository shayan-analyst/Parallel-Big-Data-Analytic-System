from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path("workspace/history/experiments.db")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def initialize() -> None:
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_name TEXT NOT NULL,
                operation TEXT NOT NULL,
                config_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                verified INTEGER NOT NULL,
                status TEXT NOT NULL
            )
        """)


def save_experiment(item: dict[str, Any]) -> None:
    with connect() as db:
        db.execute("""
            INSERT OR REPLACE INTO experiments
            (experiment_id, created_at, dataset_id, dataset_name, operation,
             config_json, result_json, verified, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item["experiment_id"], item["created_at"], item["dataset_id"],
            item["dataset_name"], item["operation"],
            json.dumps(item.get("config", {})), json.dumps(item.get("result", {})),
            1 if item.get("verified") else 0, item.get("status", "completed"),
        ))


def list_experiments(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        d["result"] = json.loads(d.pop("result_json"))
        d["verified"] = bool(d["verified"])
        out.append(d)
    return out
