from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


@dataclass
class TaskRecord:
    id: str
    name: str
    tool: str = ""
    status: str = "pending"
    logs: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: str = field(default_factory=lambda: _now_text())
    started_at: str = ""
    finished_at: str = ""
    context: dict[str, Any] = field(default_factory=dict)


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.RLock()

    def start(
        self,
        name: str,
        runner: Callable[[Callable[[str], None]], dict[str, Any]],
        tool: str = "",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = TaskRecord(id=uuid.uuid4().hex, name=name, tool=str(tool or ""), context=context or {})
        with self._lock:
            self._tasks[task.id] = task

        thread = threading.Thread(target=self._run, args=(task.id, runner), daemon=True)
        thread.start()
        return self.snapshot(task.id)

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return {"ok": False, "error": f"任务不存在: {task_id}"}
            return {
                "ok": True,
                "id": task.id,
                "name": task.name,
                "tool": task.tool,
                "status": task.status,
                "logs": list(task.logs),
                "result": task.result,
                "error": task.error,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "context": dict(task.context or {}),
            }

    def latest_snapshot(self, tool: str = "") -> dict[str, Any]:
        target_tool = str(tool or "").strip()
        with self._lock:
            tasks = list(self._tasks.values())
            if target_tool:
                tasks = [task for task in tasks if task.tool == target_tool]
            if not tasks:
                return {"ok": False, "empty": True, "error": "暂无任务"}
            latest = tasks[-1]
        return self.snapshot(latest.id)

    def _run(self, task_id: str, runner: Callable[[Callable[[str], None]], dict[str, Any]]) -> None:
        self._set_status(task_id, "running", started_at=_now_text())
        self._append_log(task_id, "任务已启动")
        try:
            result = runner(lambda message: self._append_log(task_id, message))
            with self._lock:
                task = self._tasks[task_id]
                task.result = result
                task.finished_at = _now_text()
                if isinstance(result, dict) and result.get("is_complete") is False:
                    task.status = "failed"
                    task.error = str(result.get("completion_message") or "结果不完整，仍有查询失败项")
                    task.logs.append(task.error)
                else:
                    task.status = "success"
                    task.logs.append("任务完成")
        except Exception:
            with self._lock:
                task = self._tasks[task_id]
                task.status = "failed"
                task.error = traceback.format_exc()
                task.finished_at = _now_text()
                task.logs.append("任务失败")

    def _set_status(self, task_id: str, status: str, **times: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = status
            for key, value in times.items():
                setattr(task, key, value)

    def _append_log(self, task_id: str, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self._lock:
            task = self._tasks[task_id]
            task.logs.append(line)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
