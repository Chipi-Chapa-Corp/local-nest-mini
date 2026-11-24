"""Persistent monitor history for completed pipelines."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class TaskHistory:
    def __init__(self, path: Path):
        self.path = path
        path.touch(mode=0o600, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_history (
                    id TEXT PRIMARY KEY,
                    created REAL NOT NULL,
                    task TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS pipeline_history_created
                ON pipeline_history (created DESC, id DESC)
            """)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=1)

    def save(self, task: dict) -> None:
        stages = {stage["name"]: stage for stage in task["stages"]}
        terminal = task["status"] in {"error", "interrupted"}
        final = stages.get(task["meta"].get("final_stage", "output"), {})
        complete = task["status"] == "done" and final.get("status") in {"done", "skipped"}
        ready_to_play = (task["meta"].get("source") == "browser_test"
                         and stages.get("tts", {}).get("status") == "done"
                         and final.get("status") == "waiting")
        if not (terminal or complete or ready_to_play):
            return
        payload = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as db, db:
            db.execute("""
                INSERT INTO pipeline_history (id, created, task) VALUES (?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET created=excluded.created, task=excluded.task
            """, (task["id"], task["created"], payload))

    def page(self, *, before: str | None = None, limit: int = 25) -> dict:
        where = """
            WHERE (created, id) < (SELECT created, id FROM pipeline_history WHERE id = ?)
        """ if before else ""
        params = (before, limit + 1) if before else (limit + 1,)
        with closing(self._connect()) as db:
            rows = db.execute(f"""
                SELECT id, task FROM pipeline_history {where}
                ORDER BY created DESC, id DESC LIMIT ?
            """, params).fetchall()
        tasks = [json.loads(raw) for _, raw in rows[:limit]]
        return {"tasks": tasks, "next": rows[limit - 1][0] if len(rows) > limit else None}
