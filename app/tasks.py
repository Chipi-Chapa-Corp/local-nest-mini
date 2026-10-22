"""In-memory task state for the local assistant and its monitor UI."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import OrderedDict


class TaskCancelled(RuntimeError):
    pass


def now() -> float:
    return time.time()


class TaskStore:
    def __init__(self, limit: int = 100):
        self._lock = threading.RLock()
        self._tasks: OrderedDict[str, dict] = OrderedDict()
        self._cancel: dict[str, threading.Event] = {}
        self._procs: dict[str, list] = {}
        self.limit = limit

    def create(self, task_id: str | None = None, *, created: float | None = None, **meta) -> dict:
        task_id = str(task_id or uuid.uuid4().hex)
        with self._lock:
            if task_id in self._tasks:
                return copy.deepcopy(self._tasks[task_id])
            stamp = float(created or now())
            task = {
                "id": task_id,
                "created": stamp,
                "updated": stamp,
                "status": "loading",
                "meta": meta,
                "stages": [],
                "error": None,
                "interrupt_requested": False,
            }
            self._tasks[task_id] = task
            self._cancel.setdefault(task_id, threading.Event())
            while len(self._tasks) > self.limit:
                old_id, _ = self._tasks.popitem(last=False)
                self._cancel.pop(old_id, None)
                self._procs.pop(old_id, None)
            return copy.deepcopy(task)

    def exists(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._tasks

    def _task(self, task_id: str) -> dict:
        task = self._tasks.get(task_id)
        if task is None:
            task = self.create(task_id)
            task = self._tasks[task_id]
        return task

    def _recompute(self, task: dict) -> None:
        if task["error"]:
            task["status"] = "error"
        elif task["interrupt_requested"]:
            task["status"] = "interrupted"
        elif any(stage["status"] == "loading" for stage in task["stages"]):
            task["status"] = "loading"
        else:
            task["status"] = "done"
        task["updated"] = now()

    def start(self, task_id: str, name: str, **detail) -> dict:
        return self.stage(task_id, name, "loading", **detail)

    def finish(self, task_id: str, name: str, **detail) -> dict:
        return self.stage(task_id, name, "done", **detail)

    def skip(self, task_id: str, name: str, reason: str = "", **detail) -> dict:
        return self.stage(task_id, name, "skipped", reason=reason, **detail)

    def stage(self, task_id: str, name: str, status: str, **detail) -> dict:
        with self._lock:
            task = self._task(task_id)
            stage = next((item for item in task["stages"] if item["name"] == name), None)
            stamp = now()
            if stage is None:
                stage = {"name": name, "status": status, "started": stamp, "ended": None, "detail": {}}
                task["stages"].append(stage)
            stage["status"] = status
            stage["detail"].update(detail)
            if status == "loading":
                stage["ended"] = None
            else:
                stage["ended"] = stamp
            self._recompute(task)
            return copy.deepcopy(stage)

    def fail(self, task_id: str, name: str, error: str, **detail) -> dict:
        with self._lock:
            task = self._task(task_id)
            self.stage(task_id, name, "error", **detail)
            task["error"] = {"stage": name, "message": error, "time": now()}
            self._recompute(task)
            return copy.deepcopy(task)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            event = self._cancel.setdefault(task_id, threading.Event())
            event.set()
            task["interrupt_requested"] = True
            for stage in task["stages"]:
                if stage["status"] == "loading":
                    stage["status"] = "interrupted"
                    stage["ended"] = now()
            self._recompute(task)
            procs = list(self._procs.get(task_id, []))
        for proc in procs:
            self._stop(proc)
        return True

    def cancelled(self, task_id: str | None) -> bool:
        if not task_id:
            return False
        with self._lock:
            event = self._cancel.get(task_id)
            return bool(event and event.is_set())

    def check(self, task_id: str | None) -> None:
        if self.cancelled(task_id):
            raise TaskCancelled("task interrupted")

    def register_process(self, task_id: str, proc) -> None:
        with self._lock:
            self._procs.setdefault(task_id, []).append(proc)

    def unregister_process(self, task_id: str, proc) -> None:
        with self._lock:
            procs = self._procs.get(task_id)
            if procs and proc in procs:
                procs.remove(proc)

    @staticmethod
    def _stop(proc) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def snapshot(self, limit: int | None = None) -> list[dict]:
        with self._lock:
            values = list(self._tasks.values())
            if limit:
                values = values[-limit:]
            return copy.deepcopy(list(reversed(values)))

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return copy.deepcopy(task) if task else None


TASKS = TaskStore()
