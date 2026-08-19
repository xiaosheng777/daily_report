from __future__ import annotations

import base64
import os
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from multiprocessing.connection import Client, Connection, Listener


@dataclass
class _Waiter:
    task_id: str
    per_task_limit: int
    event: threading.Event
    granted: bool = False
    error: str = ""


class _FairPermitPool:
    """A fair, in-memory permit pool owned by one Worker supervisor."""

    def __init__(self, global_limit: int) -> None:
        self.global_limit = max(1, int(global_limit))
        self._active_total = 0
        self._active_by_task: dict[str, int] = {}
        self._waiting: dict[str, deque[_Waiter]] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()
        self._closed = False

    def acquire(self, task_id: str, per_task_limit: int) -> None:
        waiter = _Waiter(task_id or "unknown", max(1, int(per_task_limit)), threading.Event())
        with self._lock:
            if self._closed:
                raise RuntimeError("LLM permit service is stopped")
            queue = self._waiting.setdefault(waiter.task_id, deque())
            queue.append(waiter)
            if waiter.task_id not in self._order:
                self._order.append(waiter.task_id)
            self._dispatch_locked()
        waiter.event.wait()
        if not waiter.granted:
            raise RuntimeError(waiter.error or "LLM permit was not granted")

    def release(self, task_id: str) -> None:
        with self._lock:
            active = self._active_by_task.get(task_id, 0)
            if active:
                self._active_total -= 1
                if active == 1:
                    self._active_by_task.pop(task_id, None)
                else:
                    self._active_by_task[task_id] = active - 1
            self._dispatch_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for queue in self._waiting.values():
                for waiter in queue:
                    waiter.error = "LLM permit service is stopped"
                    waiter.event.set()
            self._waiting.clear()
            self._order.clear()

    def _dispatch_locked(self) -> None:
        while not self._closed and self._active_total < self.global_limit and self._order:
            selected = None
            for _ in range(len(self._order)):
                task_id = self._order.popleft()
                queue = self._waiting.get(task_id)
                if not queue:
                    self._waiting.pop(task_id, None)
                    continue
                if self._active_by_task.get(task_id, 0) >= queue[0].per_task_limit:
                    self._order.append(task_id)
                    continue
                selected = task_id
                break
            if selected is None:
                return
            queue = self._waiting[selected]
            waiter = queue.popleft()
            if queue:
                self._order.append(selected)
            else:
                self._waiting.pop(selected, None)
            self._active_total += 1
            self._active_by_task[selected] = self._active_by_task.get(selected, 0) + 1
            waiter.granted = True
            waiter.event.set()


class GlobalLLMPermitServer:
    """Local authenticated IPC service shared by all Task child processes."""

    def __init__(self, global_limit: int) -> None:
        self.pool = _FairPermitPool(global_limit)
        self._authkey = secrets.token_bytes(32)
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._connections: set[Connection] = set()
        self._connections_lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._listener = Listener(("127.0.0.1", 0), authkey=self._authkey)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name="daily-report-llm-permits", daemon=True)
        self._thread.start()

    def child_environment(self) -> dict[str, str]:
        if not self._listener:
            raise RuntimeError("LLM permit service has not started")
        host, port = self._listener.address
        return {
            "DAILY_REPORT_LLM_LIMITER_ADDRESS": f"{host}:{port}",
            "DAILY_REPORT_LLM_LIMITER_AUTH": base64.b64encode(self._authkey).decode("ascii"),
        }

    def stop(self) -> None:
        self._running = False
        self.pool.close()
        if self._listener:
            self._listener.close()
            self._listener = None
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _accept_loop(self) -> None:
        while self._running and self._listener:
            try:
                connection = self._listener.accept()
            except (OSError, EOFError):
                return
            threading.Thread(target=self._handle_connection, args=(connection,), daemon=True).start()

    def _handle_connection(self, connection: Connection) -> None:
        task_id = ""
        acquired = False
        with self._connections_lock:
            self._connections.add(connection)
        try:
            request = connection.recv()
            if not isinstance(request, dict) or request.get("op") != "acquire":
                connection.send({"ok": False, "error": "invalid permit request"})
                return
            task_id = str(request.get("task_id") or "unknown")
            self.pool.acquire(task_id, int(request.get("per_task_limit") or 1))
            acquired = True
            connection.send({"ok": True})
            # Keeping this connection open makes permits self-releasing if a Task
            # process dies or is forcibly terminated.
            connection.recv()
        except (EOFError, OSError):
            pass
        except Exception as exc:
            try:
                connection.send({"ok": False, "error": str(exc)})
            except OSError:
                pass
        finally:
            if acquired:
                self.pool.release(task_id)
            with self._connections_lock:
                self._connections.discard(connection)
            try:
                connection.close()
            except OSError:
                pass


class LLMPermit:
    def __init__(self, connection: Connection | None = None, semaphore: threading.BoundedSemaphore | None = None) -> None:
        self._connection = connection
        self._semaphore = semaphore

    def __enter__(self) -> "LLMPermit":
        return self

    def __exit__(self, *_args) -> None:
        if self._connection:
            try:
                self._connection.send({"op": "release"})
            except (EOFError, OSError):
                pass
            self._connection.close()
        if self._semaphore:
            self._semaphore.release()


class LLMPermitClient:
    def __init__(self, task_id: str, per_task_limit: int) -> None:
        self.task_id = task_id or "unknown"
        self.per_task_limit = max(1, int(per_task_limit))
        address = os.getenv("DAILY_REPORT_LLM_LIMITER_ADDRESS", "")
        auth = os.getenv("DAILY_REPORT_LLM_LIMITER_AUTH", "")
        self._address: tuple[str, int] | None = None
        self._authkey = b""
        if address and auth and ":" in address:
            host, port = address.rsplit(":", 1)
            try:
                self._address = (host, int(port))
                self._authkey = base64.b64decode(auth.encode("ascii"), validate=True)
            except (ValueError, TypeError):
                self._address = None
        self._fallback = threading.BoundedSemaphore(self.per_task_limit)

    def acquire(self) -> LLMPermit:
        if not self._address:
            self._fallback.acquire()
            return LLMPermit(semaphore=self._fallback)
        connection = Client(self._address, authkey=self._authkey)
        try:
            connection.send({"op": "acquire", "task_id": self.task_id, "per_task_limit": self.per_task_limit})
            reply = connection.recv()
            if not isinstance(reply, dict) or not reply.get("ok"):
                raise RuntimeError(str((reply or {}).get("error") or "LLM permit rejected"))
            return LLMPermit(connection=connection)
        except Exception:
            connection.close()
            raise
