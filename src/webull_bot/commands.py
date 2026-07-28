import fcntl
import json
import time
import uuid
from pathlib import Path


class CommandQueue:
    """A small shared JSON command queue for dashboard-initiated actions.

    The dashboard is a separate, lower-trust process with no Webull
    credentials or API access - it can only enqueue an action request here
    (close all, sell one position, add a watchlist symbol). The trader reads
    and clears the queue once per cycle and executes requests through its
    own already-safe order-placement code. Both sides share the same file
    on the same host volume, so an advisory file lock keeps the
    read-modify-write on each side safe against the other.
    """

    def __init__(self, path: str):
        self.path = Path(path)

    def _with_lock(self, mutate):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        with open(self.path, "r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                raw = handle.read().strip()
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    data = {}
                commands = data.get("commands", [])
                if not isinstance(commands, list):
                    commands = []
                result, new_commands = mutate(commands)
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps({"commands": new_commands}))
                return result
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def enqueue(self, command_type: str, **fields) -> str:
        command_id = uuid.uuid4().hex
        command = {
            "id": command_id,
            "type": command_type,
            "requested_at": time.time(),
            **fields,
        }

        def mutate(commands: list[dict]):
            commands.append(command)
            return command_id, commands

        return self._with_lock(mutate)

    def pop_all(self) -> list[dict]:
        def mutate(commands: list[dict]):
            return list(commands), []

        return self._with_lock(mutate)
