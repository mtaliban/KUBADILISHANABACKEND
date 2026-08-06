from datetime import datetime, timezone
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

    def is_online(self, user_id: str) -> bool:
        return user_id in self._active and len(self._active[user_id]) > 0

    def online_users(self) -> list[str]:
        return [uid for uid, conns in self._active.items() if conns]

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

    async def broadcast(self, payload: dict) -> int:
        """Send to every connected user."""
        delivered = 0
        for uid in list(self._active.keys()):
            delivered += await self.send_to_user(uid, payload)
        return delivered


manager = ConnectionManager()
