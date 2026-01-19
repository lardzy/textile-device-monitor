from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder
from typing import List, Dict, Any


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def send_personal_message(
        self, message: Dict[str, Any], websocket: WebSocket
    ):
        await websocket.send_json(jsonable_encoder(message))

    async def broadcast(self, message: Dict[str, Any]):
        payload = jsonable_encoder(message)
        for connection in self.active_connections:
            try:
                await connection.send_json(payload)
            except Exception as e:
                print(f"Error sending message: {e}")

    async def broadcast_device_status(
        self, device_id: int, status: str, data: Dict[str, Any]
    ):
        """广播设备状态更新"""
        message = {
            "type": "device_status_update",
            "device_id": device_id,
            "status": status,
            "data": data,
        }
        await self.broadcast(message)

    async def broadcast_queue_update(self, device_id: int, queue_count: int):
        """广播排队更新"""
        message = {
            "type": "queue_update",
            "device_id": device_id,
            "queue_count": queue_count,
        }
        await self.broadcast(message)

    async def broadcast_device_offline(self, device_id: int, last_seen: str):
        """广播设备离线"""
        message = {
            "type": "device_offline",
            "device_id": device_id,
            "last_seen": last_seen,
        }
        await self.broadcast(message)


websocket_manager = ConnectionManager()
