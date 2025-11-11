
import asyncio
import json
import logging
import os
from dotenv import load_dotenv
import websockets
from websockets.legacy.server import WebSocketServerProtocol
from websockets.legacy.client import connect
from aiohttp import web
import aiohttp

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()
PORT = int(os.getenv("PORT", 3000))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY must be set in .env file")


async def connect_to_openai():
    """Connect to OpenAI's WebSocket endpoint."""
    uri = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"

    try:
        ws = await connect(
            uri,
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
                "OpenAI-Beta": "realtime=v1",
            },
            subprotocols=["realtime"],
        )
        logger.info("Successfully connected to OpenAI")

        response = await ws.recv()
        try:
            event = json.loads(response)
            if event.get("type") != "session.created":
                raise Exception(f"Expected session.created, got {event.get('type')}")
            logger.info("Received session.created response")

            update_session = {
                "type": "session.update",
                "session": {
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "modalities": ["text", "audio"],
                    "voice": "coral",
                },
            }
            await ws.send(json.dumps(update_session))
            logger.info("Sent session.create message")

            return (
                ws,
                event,
            )
        except json.JSONDecodeError:
            raise Exception(f"Invalid JSON response from OpenAI: {response}")

    except Exception as e:
        logger.error(f"Failed to connect to OpenAI: {str(e)}")
        raise


class WebSocketRelay:
    def __init__(self):
        """Initialize the WebSocket relay server."""
        self.connections = {}
        self.message_queues = {}


    async def handle_browser_connection(
        self, websocket: WebSocketServerProtocol, path: str, remote_address=None
    ):
        """Handle a connection from the browser."""
        base_path = path.split("?")[0]
        if base_path != "/":
            logger.error(f"Invalid path: {path}")
            await websocket.close(1008, "Invalid path")
            return

        if remote_address is not None:
            logger.info(f"Browser connected from {remote_address}")
        else:
            logger.info(f"Browser connected (no remote address available)")
        self.message_queues[websocket] = []
        openai_ws = None

        try:
            # Connect to OpenAI
            openai_ws, session_created = await connect_to_openai()
            self.connections[websocket] = openai_ws

            logger.info("Connected to OpenAI successfully!")

            await websocket.send_str(json.dumps(session_created))
            logger.info("Forwarded session.created to browser")

            while self.message_queues[websocket]:
                message = self.message_queues[websocket].pop(0)
                try:
                    event = json.loads(message)
                    logger.info(f'Relaying "{event.get("type")}" to OpenAI')
                    await openai_ws.send(message)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from browser: {message}")

            async def handle_browser_messages():
                try:
                    async for msg in websocket:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            message = msg.data
                            try:
                                event = json.loads(message)
                                logger.info(f'Relaying "{event.get("type")}" to OpenAI')
                                await openai_ws.send(message)
                            except json.JSONDecodeError:
                                logger.error(f"Invalid JSON from browser: {message}")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WebSocket connection closed with exception {websocket.exception()}")
                except Exception as e:
                    logger.info(f"Browser connection closed: {e}")
                    raise

            async def handle_openai_messages():
                try:
                    while True:
                        # openai_ws es de websockets, sí tiene .recv()
                        message = await openai_ws.recv()
                        try:
                            event = json.loads(message)
                            logger.info(
                                f'Relaying "{event.get("type")}" from OpenAI: {message}'
                            )
                            await websocket.send_str(message)
                        except json.JSONDecodeError:
                            logger.error(f"Invalid JSON from OpenAI: {message}")
                except Exception as e:
                    logger.info(f"OpenAI connection closed: {e}")
                    raise

            try:
                await asyncio.gather(
                    handle_browser_messages(), handle_openai_messages()
                )
            except websockets.exceptions.ConnectionClosed:
                logger.info("One of the connections closed, cleaning up")

        except Exception as e:
            logger.error(f"Error handling connection: {str(e)}")
            if not websocket.closed:
                await websocket.close()
        finally:
            if websocket in self.connections:
                if openai_ws and not openai_ws.closed:
                    await openai_ws.close(1000, "Normal closure")
                del self.connections[websocket]
            if websocket in self.message_queues:
                del self.message_queues[websocket]
            if not websocket.closed:
                await websocket.close()

    async def serve(self):
        """Start the WebSocket relay server."""
        async with serve(
            self.handle_browser_connection,
            "0.0.0.0",
            PORT,
            ping_interval=20,
            ping_timeout=20,
            subprotocols=["realtime"],
        ):
            logger.info(f"WebSocket relay server started on ws://0.0.0.0:{PORT}")
            await asyncio.Future()



# --- HTTP + WebSocket server with aiohttp ---

async def websocket_handler(request):
    ws = web.WebSocketResponse(protocols=["realtime"])
    await ws.prepare(request)
    relay = request.app["relay"]
    remote_addr = request.remote
    await relay.handle_browser_connection(ws, "/", remote_address=remote_addr)
    return ws


def create_app():
    app = web.Application()
    relay = WebSocketRelay()
    app["relay"] = relay
    # Servir archivos estáticos desde ./public en la raíz
    public_dir = os.path.join(os.path.dirname(__file__), "public")
    app.router.add_static("/", public_dir, show_index=True)
    # WebSocket en /ws
    app.router.add_route("GET", "/ws", websocket_handler)
    return app

def main():
    app = create_app()
    port = PORT
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()