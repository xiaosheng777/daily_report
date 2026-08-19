from src.worker import DuplicateWorker


def test_supervisor_claims_only_configured_number_of_tasks_then_claims_next_after_reap():
    queued = [
        {"task_id": "task-1", "current_attempt": 1},
        {"task_id": "task-2", "current_attempt": 1},
        {"task_id": "task-3", "current_attempt": 1},
    ]

    class FakeDatabase:
        @staticmethod
        def claim_next_task(_worker_id, _lease_token):
            return queued.pop(0) if queued else None

    worker = object.__new__(DuplicateWorker)
    worker.db = FakeDatabase()
    worker.worker_id = "worker-test"
    worker.lease_token = "lease-test"
    worker.max_concurrent_tasks = 2
    worker.stop_requested = False
    worker.running_tasks = {}
    started = []

    def start(task):
        started.append(task["task_id"])
        worker.running_tasks[task["task_id"]] = object()

    worker._start_claimed_task = start
    worker._claim_available_tasks()

    assert started == ["task-1", "task-2"]
    assert [item["task_id"] for item in queued] == ["task-3"]

    worker.running_tasks.pop("task-1")
    worker._claim_available_tasks()

    assert started == ["task-1", "task-2", "task-3"]
