from typing import Dict, Set
from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self._active: Dict[str, Set[WebSocket]] = {}

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._active.setdefault(user_id, set()).add(ws)

    def disconnect(self, user_id: str, ws: WebSocket) -> None:
        conns = self._active.get(user_id)
        if conns and ws in conns:
            conns.discard(ws)
            if not conns: self._active.pop(user_id, None)

    async def send_to_user(self, user_id: str, payload: dict) -> int:
        conns = self._active.get(user_id)
        if not conns: return 0
        delivered = 0; dead = []
        for ws in list(conns):
            try:
                await ws.send_json(payload); delivered += 1
            except Exception:
                dead.append(ws)
        for ws in dead: conns.discard(ws)
        return delivered


manager = ConnectionManager()
