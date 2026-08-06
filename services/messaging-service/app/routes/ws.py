"""WebSocket endpoint: /ws?token=JWT. Delivers real-time messages + typing indicators."""
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from ..core.auth import user_id_from_token
from ..ws_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(...)):
    user_id = user_id_from_token(token)
    if not user_id:
        await ws.close(code=4401)
        return

    await manager.connect(user_id, ws)
    logger.info(f"WS connected: {user_id}")
    try:
        while True:
            # simple echo/ping loop; real messages go through POST /messages
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(user_id, ws)
        logger.info(f"WS disconnected: {user_id}")
