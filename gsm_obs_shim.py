import asyncio
import websockets
import logging
import signal
import json
import base64
import subprocess
import uuid
import os
import sys
import msgpack

import argparse
import pathlib
import io
try:
    import tkinter as tk
    from tkinter import ttk
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False
import threading
from PIL import Image

def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

ARGS = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Filter out noisy "opening handshake failed" errors
class NoiselessHandshakeFilter(logging.Filter):
    def filter(self, record):
        return "opening handshake failed" not in record.getMessage()

logging.getLogger("websockets.server").addFilter(NoiselessHandshakeFilter())

GSM_CLIENT_PORT = 7274
OBS_SERVER_URI = "ws://127.0.0.1:7275"
TRAFFIC_LOG_FILE = "/tmp/obs_proxy.log"
OBS_SUBPROTOCOLS = ["obswebsocket.json", "obswebsocket.msgpack", "obs-websocket"]

CONFIG_DIR = pathlib.Path.home() / ".config" / "gsm-obs-shim"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Configure traffic logger
traffic_logger = logging.getLogger("traffic")
traffic_logger.setLevel(logging.INFO)
traffic_logger.propagate = False # Don't propagate to root logger
traffic_handler = logging.FileHandler(TRAFFIC_LOG_FILE)
traffic_handler.setFormatter(logging.Formatter('%(message)s'))
traffic_logger.addHandler(traffic_handler)

shutdown_event = asyncio.Event()
active_tasks = set()

class GameState:
    def __init__(self, initial_name="GSM-OBS Shim"):
        self.game_name = initial_name
        self._lock = threading.Lock()
        self.load_config()

    def set_name(self, name):
        with self._lock:
            self.game_name = name
            logger.info(f"Game name updated to: {name}")
            self.save_config()

    def get_name(self):
        with self._lock:
            return self.game_name

    def load_config(self):
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    self.game_name = data.get("game_name", self.game_name)
                    logger.info(f"Loaded game name from config: {self.game_name}")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")

    def save_config(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump({"game_name": self.game_name}, f)
            logger.debug("Saved game name to config.")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

GAME_STATE = GameState()

class GameGui:
    def __init__(self, game_state, shutdown_evt):
        self.game_state = game_state
        self.shutdown_evt = shutdown_evt
        self.root = None
        self.name_var = None
        self.entry = None
        self.update_btn = None

    def start(self):
        self.root = tk.Tk()
        self.root.title("GSM-OBS Shim Control")
        self.root.geometry("400x150")
        
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main_frame, text="Game Name:").pack(pady=(0, 5))
        
        self.name_var = tk.StringVar(value=self.game_state.get_name())
        self.entry = ttk.Entry(main_frame, textvariable=self.name_var, width=40)
        self.entry.pack(pady=(0, 10))
        self.entry.bind("<Return>", lambda e: self.update_name())

        self.update_btn = ttk.Button(main_frame, text="Update", command=self.update_name)
        self.update_btn.pack()

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Check for external shutdown
        self.check_shutdown()
        
        self.root.mainloop()

    def update_name(self):
        new_name = self.name_var.get()
        self.game_state.set_name(new_name)

    def check_shutdown(self):
        if self.shutdown_evt.is_set():
            self.root.destroy()
        else:
            self.root.after(500, self.check_shutdown)

    def on_close(self):
        logger.info("GUI closed. Initiating shutdown...")
        self.shutdown_evt.set()
        self.root.destroy()

def log_traffic(message, source_name):
    """Log traffic to file with truncation for large fields."""
    prefix = "Client" if source_name == "GSM Client" else "Server"
    try:
        # Attempt to parse as JSON or MessagePack for pretty logging
        if isinstance(message, bytes):
            msg_obj = msgpack.unpackb(message, raw=False)
        else:
            msg_obj = json.loads(message)
        
        # Special handling for large image data to keep logs readable
        if isinstance(msg_obj, dict) and msg_obj.get("op") == 7: # RequestResponse
            d = msg_obj.get("d", {})
            if isinstance(d, dict):
                resp_data = d.get("responseData")
                if isinstance(resp_data, dict) and resp_data.get("imageData"):
                    img_data = resp_data["imageData"]
                    if len(img_data) > 200:
                        # Truncate for log only
                        d_copy = d.copy()
                        rd_copy = resp_data.copy()
                        if isinstance(img_data, bytes):
                            rd_copy["imageData"] = f"<BINARY_IMAGE_DATA_TRUNCATED_{len(img_data)}_BYTES>"
                        else:
                            rd_copy["imageData"] = f"<BASE64_IMAGE_DATA_TRUNCATED_{len(img_data)}_BYTES>"
                        d_copy["responseData"] = rd_copy
                        msg_obj_log = msg_obj.copy()
                        msg_obj_log["d"] = d_copy
                        log_content = json.dumps(msg_obj_log, indent=2, ensure_ascii=False)
                    else:
                        log_content = json.dumps(msg_obj, indent=2, ensure_ascii=False)
                else:
                    log_content = json.dumps(msg_obj, indent=2, ensure_ascii=False)
            else:
                log_content = json.dumps(msg_obj, indent=2, ensure_ascii=False)
        else:
            log_content = json.dumps(msg_obj, indent=2, ensure_ascii=False)
    except Exception:
        # Fallback to raw message if not JSON/MsgPack or parsing fails
        log_content = message

    log_msg = f"{prefix} -> {log_content}"
    traffic_logger.info(log_msg)
    for handler in traffic_logger.handlers:
        handler.flush()

async def forward(source: websockets.WebSocketServerProtocol | websockets.WebSocketClientProtocol, 
                  destination: websockets.WebSocketServerProtocol | websockets.WebSocketClientProtocol, 
                  source_name: str):
    """Forward messages from source to destination."""
    try:
        async for message in source:
            logger.debug(f"Forwarding from {source_name}: {message}")
            
            log_traffic(message, source_name)
            await destination.send(message)
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"Connection closed during forwarding {source_name}. Cause: {e}")
    except asyncio.CancelledError:
        logger.info(f"Forwarding from {source_name} cancelled.")
        raise # Re-raise to ensure proper task cancellation handling handling
    except Exception as e:
        logger.error(f"Error forwarding from {source_name}: {e}")
    finally:
        logger.info(f"Forwarding from {source_name} finished. Closing destination.")
        # Close the destination if source closes
        # Need to be careful not to double-close or cause errors if it's already closed
        try:
            await destination.close()
        except:
            pass

async def proxy_handler(websocket):
    """Handle incoming connection from GSM Client (Proxy Mode)."""
    client_addr = websocket.remote_address
    logger.info(f"New connection from GSM Client: {client_addr}")
    logger.info(f"Client Subprotocol: {websocket.subprotocol}")
    logger.info(f"Client Headers: {websocket.request.headers}")

    try:
        # Connect to OBS Server
        logger.info(f"Connecting to OBS Server at {OBS_SERVER_URI}...")
        subprotocols = [websocket.subprotocol] if websocket.subprotocol else None
        async with websockets.connect(OBS_SERVER_URI, subprotocols=subprotocols, max_size=10*1024*1024) as obs_ws:
            logger.info("Connected to OBS Server.")

            # Create tasks for bi-directional forwarding
            client_to_obs = asyncio.create_task(forward(websocket, obs_ws, "GSM Client"))
            obs_to_client = asyncio.create_task(forward(obs_ws, websocket, "OBS Server"))

            # Wait for either task to finish (connection closed)
            done, pending = await asyncio.wait(
                [client_to_obs, obs_to_client],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
    except ConnectionRefusedError:
         logger.error(f"Failed to connect to OBS Server at {OBS_SERVER_URI}. Is it running?")
         await websocket.close(1011, "OBS Server unavailable")
    except Exception as e:
        logger.error(f"Error in handler: {e}")
    finally:
        logger.info(f"Connection handler finished for {client_addr}")


import json
import base64

async def mock_handler(websocket):
    """Handle incoming connection from GSM Client (Mock Mode)."""
    client_addr = websocket.remote_address
    logger.info(f"New connection from GSM Client (Mock Mode): {client_addr}")
    logger.info(f"Client Subprotocol: {websocket.subprotocol}")
    logger.info(f"Client Headers: {websocket.request.headers}")

    use_msgpack = (websocket.subprotocol == "obswebsocket.msgpack")
    
    # helper to send message with correct encoding and logging
    async def send_msg(msg_obj):
        if use_msgpack:
            encoded = msgpack.packb(msg_obj)
        else:
            encoded = json.dumps(msg_obj)
        log_traffic(encoded, "OBS Server")
        await websocket.send(encoded)

    # Initial Hello (Op 0) - Server -> Client
    hello_msg = {
        "op": 0,
        "d": {
            "obsStudioVersion": "32.0.4",
            "obsWebSocketVersion": "5.6.3",
            "rpcVersion": 1
        }
    }
    await send_msg(hello_msg)
    logger.info(f"Sent Hello (Op 0) - {'MsgPack' if use_msgpack else 'JSON'}")

    try:
        async for message in websocket:
            log_traffic(message, "GSM Client")
            try:
                if isinstance(message, bytes):
                    msg_json = msgpack.unpackb(message, raw=False)
                    use_msgpack = True # Switch if client sends msgpack
                else:
                    msg_json = json.loads(message)
                    use_msgpack = False # Switch if client sends json
                
                op = msg_json.get("op")
                
                # Identify (Op 1) - Client -> Server
                if op == 1:
                    logger.info("Received Identify (Op 1)")
                    # Send Identified (Op 2)
                    identified_msg = {
                        "op": 2,
                        "d": {
                            "negotiatedRpcVersion": 1
                        }
                    }
                    await send_msg(identified_msg)
                    logger.info("Sent Identified (Op 2)")
                    continue

                # Request (Op 6) - Client -> Server
                if op == 6:
                    d = msg_json.get("d", {})
                    request_type = d.get("requestType")
                    request_id = d.get("requestId")
                    logger.info(f"Received Request: {request_type} (ID: {request_id})")

                    response_data = None
                    
                    if request_type == "GetVersion":
                        response_data = {
                            "availableRequests": [
                                "OpenSourceProjector","OpenInputInteractDialog","OpenInputFiltersDialog","OpenInputPropertiesDialog",
                                "OffsetMediaInputCursor","GetMediaInputStatus","CreateRecordChapter","SplitRecordFile","ResumeRecord",
                                "StopRecord","ToggleRecord","SendStreamCaption","StopStream","SetInputAudioSyncOffset",
                                "GetSceneSceneTransitionOverride","SetInputSettings","GetInputDefaultSettings","StopOutput",
                                "GetInputSettings","SetCurrentSceneTransitionDuration","SetInputName","GetInputDeinterlaceMode",
                                "CreateInput","TriggerHotkeyByKeySequence","SetSceneSceneTransitionOverride","SetSceneName",
                                "RemoveScene","GetInputList","SetCurrentPreviewScene","BroadcastCustomEvent","ToggleRecordPause",
                                "GetVirtualCamStatus","SetCurrentSceneTransitionSettings","PauseRecord","GetSceneItemTransform",
                                "GetCurrentProgramScene","SetCurrentSceneTransition","GetSceneItemPrivateSettings",
                                "SetSourceFilterSettings","SetInputVolume","GetStreamServiceSettings","GetCurrentSceneTransition",
                                "SaveReplayBuffer","SetSourcePrivateSettings","Sleep","StartRecord","StartOutput","StopReplayBuffer",
                                "SetStudioModeEnabled","GetHotkeyList","GetSceneItemList","GetStats","GetRecordStatus",
                                "ToggleReplayBuffer","GetCurrentPreviewScene","RemoveInput","DuplicateSceneItem",
                                "SetCurrentSceneCollection","GetPersistentData","CallVendorRequest","SaveSourceScreenshot",
                                "SetInputAudioBalance","GetSceneCollectionList","GetSourceActive","GetGroupList","GetVersion",
                                "CreateProfile","GetSourcePrivateSettings","GetProfileParameter","GetSourceScreenshot",
                                "SetProfileParameter","OpenVideoMixProjector","GetInputPropertiesListPropertyItems",
                                "GetStudioModeEnabled","GetGroupSceneItemList","GetSceneItemEnabled","RemoveSourceFilter",
                                "SetVideoSettings","SetMediaInputCursor","SetSourceFilterIndex","SetStreamServiceSettings",
                                "SetInputAudioTracks","GetSceneItemId","GetInputAudioBalance","GetRecordDirectory",
                                "GetReplayBufferStatus","GetStreamStatus","GetInputMute","TriggerHotkeyByName",
                                "SetInputDeinterlaceFieldOrder","SetCurrentProfile","RemoveProfile","ToggleInputMute",
                                "SetRecordDirectory","GetInputAudioMonitorType","SetInputAudioMonitorType","GetInputAudioTracks",
                                "GetSceneTransitionList","GetInputVolume","GetTransitionKindList","PressInputPropertiesButton",
                                "GetMonitorList","CreateSceneCollection","TriggerStudioModeTransition","SetTBarPosition",
                                "GetProfileList","SetSceneItemBlendMode","SetInputMute","GetLastReplayBufferReplay",
                                "SetInputDeinterlaceMode","GetSourceFilterKindList","ToggleStream","GetInputKindList",
                                "GetSourceFilterList","GetSourceFilterDefaultSettings","GetInputAudioSyncOffset","SetSceneItemLocked",
                                "StartStream","CreateSourceFilter","GetInputDeinterlaceFieldOrder","SetSourceFilterName",
                                "StartReplayBuffer","GetSourceFilter","SetSourceFilterEnabled","GetSceneItemSource","GetSceneList",
                                "SetPersistentData","CreateSceneItem","RemoveSceneItem","TriggerMediaInputAction","SetSceneItemEnabled",
                                "SetSceneItemIndex","GetSceneItemLocked","GetSceneItemIndex","GetSceneItemBlendMode",
                                "SetSceneItemPrivateSettings","CreateScene","GetCurrentSceneTransitionCursor","ToggleVirtualCam",
                                "GetVideoSettings","StartVirtualCam","StopVirtualCam","GetOutputList","GetOutputStatus",
                                "SetSceneItemTransform","ToggleOutput","GetOutputSettings","GetSpecialInputs","SetCurrentProgramScene",
                                "SetOutputSettings"
                            ],
                            "obsStudioVersion": "32.0.4",
                            "obsWebSocketVersion": "5.6.3",
                            "platform": "org.kde.Platform",
                            "platformDescription": "KDE Flatpak runtime",
                            "rpcVersion": 1,
                            "supportedImageFormats": [
                                "avif","bmp","bw","cur","dds","eps","epsf","epsi","hej2","icns","ico","j2k","jfif","jp2","jpeg",
                                "jpg","jxl","pbm","pcx","pgm","pic","png","ppm","qoi","rgb","rgba","sgi","tif","tiff","wbmp",
                                "webp","xbm","xpm"
                            ]
                        }
                    
                    elif request_type == "GetSceneList":
                        game_name = GAME_STATE.get_name()
                        response_data = {
                            "currentPreviewSceneName": None,
                            "currentPreviewSceneUuid": None,
                            "currentProgramSceneName": game_name,
                            "currentProgramSceneUuid": "3be8ae7c-585a-4b95-937f-89c691af657b",
                            "scenes": [
                                {"sceneIndex": 0, "sceneName": game_name, "sceneUuid": "3be8ae7c-585a-4b95-937f-89c691af657b"}
                            ]
                        }

                    elif request_type == "GetCurrentProgramScene":
                        game_name = GAME_STATE.get_name()
                        response_data = {
                            "currentProgramSceneName": game_name,
                            "currentProgramSceneUuid": "3be8ae7c-585a-4b95-937f-89c691af657b",
                            "sceneName": game_name,
                            "sceneUuid": "3be8ae7c-585a-4b95-937f-89c691af657b"
                        }

                    elif request_type == "GetVideoSettings":
                        response_data = {
                            "baseHeight": 1440,
                            "baseWidth": 2560,
                            "fpsDenominator": 1,
                            "fpsNumerator": 60,
                            "outputHeight": 1080,
                            "outputWidth": 1920
                        }

                    elif request_type == "GetSceneItemList":
                        # Return items for the configured game scene
                        response_data = {
                            "sceneItems": [
                                {
                                    "inputKind": "pipewire-screen-capture-source",
                                    "isGroup": None,
                                    "sceneItemBlendMode": "OBS_BLEND_NORMAL",
                                    "sceneItemEnabled": True,
                                    "sceneItemId": 1,
                                    "sceneItemIndex": 0,
                                    "sceneItemLocked": False,
                                    "sceneItemTransform": {
                                        "alignment": 5, "boundsAlignment": 0, "boundsHeight": 1440.0,
                                        "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": 2560.0,
                                        "cropBottom": 0, "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropToBounds": False,
                                        "height": 2160.0, "positionX": 0.0, "positionY": 0.0, "rotation": 0.0,
                                        "scaleX": 1.0, "scaleY": 1.0, "sourceHeight": 2160.0, "sourceWidth": 3840.0,
                                        "width": 3840.0
                                    },
                                    "sourceName": "Michadame",
                                    "sourceType": "OBS_SOURCE_TYPE_INPUT",
                                    "sourceUuid": "b8f39269-e0a1-4346-b0d7-2786589dcf57"
                                }
                            ]
                        }
                    
                    elif request_type == "GetOutputList":
                         response_data = {
                             "outputs": [
                                 {
                                     "outputActive": False,
                                     "outputFlags": {
                                         "OBS_OUTPUT_AUDIO": True, "OBS_OUTPUT_ENCODED": True, 
                                         "OBS_OUTPUT_MULTI_TRACK": True, "OBS_OUTPUT_SERVICE": False, 
                                         "OBS_OUTPUT_VIDEO": True
                                     },
                                     "outputHeight": 0,
                                     "outputKind": "ffmpeg_muxer",
                                     "outputName": "simple_file_output",
                                     "outputWidth": 0
                                 }
                             ]
                         }

                    elif request_type == "GetSourceScreenshot":
                        fake_image_data = None
                        request_data = d.get("requestData", {})
                        target_width = request_data.get("imageWidth")
                        target_height = request_data.get("imageHeight")
                        
                        # Check if portal source is selected
                        raw_image_bytes = None
                        if hasattr(ARGS, 'portal_node_id') and ARGS.portal_node_id:
                            try:
                                node_id = ARGS.portal_node_id
                                tmp_file = f"/tmp/capture_{uuid.uuid4().hex}.jpg"
                                cmd = [
                                    "gst-launch-1.0", 
                                    "pipewiresrc", f"path={node_id}", "num-buffers=1", 
                                    "!", "videoconvert", 
                                    "!", "jpegenc", 
                                    "!", "filesink", f"location={tmp_file}"
                                ]
                                
                                logger.info(f"Capturing frame from Node {node_id}...")
                                subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=2)
                                
                                with open(tmp_file, "rb") as f:
                                    raw_image_bytes = f.read()
                                
                                os.remove(tmp_file)
                                logger.info("Frame captured successfully.")
                                
                            except Exception as e:
                                logger.error(f"Failed to capture frame: {e}")

                        if raw_image_bytes is None:
                            try:
                                fake_img_path = get_resource_path("fake_image.jpg")
                                with open(fake_img_path, "rb") as image_file:
                                    raw_image_bytes = image_file.read()
                            except FileNotFoundError:
                                logger.error(f"fake_image.jpg not found at {fake_img_path}, using placeholder")
                                # Fallback to 1x1 white pixel
                                raw_image_bytes = base64.b64decode("/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wAALCAABAAEBAREA/8QAJgABAAAAAAAAAAAAAAAAAAAAAxABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAAPwBH/9k=")

                        if raw_image_bytes:
                            # Apply resizing if requested
                            if target_width or target_height:
                                try:
                                    img = Image.open(io.BytesIO(raw_image_bytes))
                                    original_width, original_height = img.size
                                    
                                    # Calculate new dimensions
                                    if target_width and target_height:
                                        new_width, new_height = target_width, target_height
                                    elif target_width:
                                        new_width = target_width
                                        new_height = int(original_height * (target_width / original_width))
                                    else: # target_height
                                        new_height = target_height
                                        new_width = int(original_width * (target_height / original_height))
                                    
                                    logger.info(f"Resizing screenshot from {original_width}x{original_height} to {new_width}x{new_height}")
                                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                                    
                                    img_byte_arr = io.BytesIO()
                                    img.save(img_byte_arr, format='JPEG')
                                    raw_image_bytes = img_byte_arr.getvalue()
                                except Exception as e:
                                    logger.error(f"Failed to resize image: {e}")
                                    # Fall back to original bytes if resizing fails

                            fake_image_data = base64.b64encode(raw_image_bytes).decode('utf-8')
                        
                        response_data = {
                            "imageData": fake_image_data,
                            "imageFormat": "jpg"
                        }

                    elif request_type == "GetRecordDirectory":
                        response_data = {
                            "recordDirectory": "/tmp/fake_record_dir"
                        }

                    elif request_type == "SetSceneItemTransform":
                         # Just success
                         response_data = {} # No data needed for success usually, just status

                    # Construct Response
                    if response_data is not None:
                        response_msg_d = {
                            "requestId": request_id,
                            "requestStatus": {
                                "result": True,
                                "code": 100
                            },
                            "requestType": request_type
                        }
                        
                        if response_data:
                            response_msg_d["responseData"] = response_data
                        else:
                            response_msg_d["responseData"] = {}

                        response_msg = {
                            "op": 7, # RequestResponse
                            "d": response_msg_d
                        }

                        await send_msg(response_msg)
                        logger.info(f"Sent Response for {request_type} (ID: {request_id})")
                    else:
                        logger.warning(f"Unknown Request Type: {request_type}")
                        # Could send error response here but skipping for simplicity
                        
            except (json.JSONDecodeError, msgpack.UnpackException):
                logger.error("Failed to parse message")
            except Exception as e:
                logger.error(f"Error processing message: {e}")

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Mock connection closed for {client_addr}")
    except Exception as e:
        logger.error(f"Error in mock handler: {e}")

async def connection_handler(websocket):
    """Dispatch connection to appropriate handler based on mode."""
    task = asyncio.current_task()
    active_tasks.add(task)
    try:
        # Check if ARGS is initialized and proxy mode is requested
        if ARGS and ARGS.proxy:
            await proxy_handler(websocket)
        else:
            # Default to mock handler if ARGS is None or proxy is False
            await mock_handler(websocket)
    finally:
        active_tasks.remove(task)

async def main():
    global ARGS
    parser = argparse.ArgumentParser(description="GSM-OBS Shim")
    parser.add_argument("--proxy", action="store_true", help="Run in transparent proxy mode (connects to real OBS)")
    parser.add_argument("--game-name", help="Initial name of the game")
    ARGS = parser.parse_args()

    if ARGS.game_name:
        GAME_STATE.set_name(ARGS.game_name)
    ARGS.portal_node_id = None

    portal_process = None
    if not ARGS.proxy:
        logger.info("Starting Portal Client for window selection...")
        try:
            # Resolve path to portal_client.py (works when bundled)
            portal_script = get_resource_path("portal_client.py")
            
            # Use system python for dbus-python support
            # Use unbuffered output (-u) to ensure we get the ID immediately
            portal_process = subprocess.Popen(
                ["/usr/bin/python3", "-u", portal_script],
                stdout=subprocess.PIPE,
                stderr=sys.stderr, # Pass stderr to console
                text=True,
                bufsize=1 
            )
            
            logger.info("Please select a window/screen in the dialog if prompted...")
            # Read Node ID
            line = portal_process.stdout.readline()
            if line:
                try:
                    val = line.strip()
                    if val.isdigit():
                        ARGS.portal_node_id = int(val)
                        logger.info(f"Selected Source Node ID: {ARGS.portal_node_id}")
                    else:
                        logger.error(f"Portal client returned non-integer: {val}")
                except ValueError:
                    logger.error(f"Failed to parse Node ID from portal client: {line}")
            else:
                 logger.error("Portal client failed to return Node ID (empty output)")
                 
        except Exception as e:
            logger.error(f"Failed to start portal client: {e}")

    loop = asyncio.get_running_loop()
    
    # Handle shutdown signals
    def signal_handler(*args):
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    mode_str = "Proxy Mode" if ARGS.proxy else "Mock Server Mode"
    logger.info(f"Starting GSM-OBS Shim on port {GSM_CLIENT_PORT} ({mode_str})...")

    # Start GUI in a separate thread if available
    if GUI_AVAILABLE:
        gui = GameGui(GAME_STATE, shutdown_event)
        gui_thread = threading.Thread(target=gui.start, daemon=True)
        gui_thread.start()
    else:
        logger.warning("Tkinter not found. GUI will not be available. Using fallback game name: %s", GAME_STATE.get_name())
        logger.info("To enable GUI, install python3-tk: sudo apt-get install python3-tk")
    
    def select_subprotocol(connection, client_subprotocols):
        # Allow connections that don't request a subprotocol
        if not client_subprotocols:
            return None
        # Match client subprotocols against our supported list (OBS_SUBPROTOCOLS)
        for sp in client_subprotocols:
            if sp in OBS_SUBPROTOCOLS:
                return sp
        # If no match, return None (permissive)
        return None

    # NOTE: The 'subprotocols' argument to serve is the list of supported protocols.
    # The 'select_subprotocol' argument is the strategy.
    
    try:
        server = await websockets.serve(
            connection_handler, 
            "127.0.0.1", 
            GSM_CLIENT_PORT, 
            ping_interval=None, 
            subprotocols=OBS_SUBPROTOCOLS,
            select_subprotocol=select_subprotocol,
            max_size=10*1024*1024
        )
        logger.info("Server started. Press Ctrl+C to stop.")
        await shutdown_event.wait()
        logger.info("Shutting down initiated...")
    finally:
        # 1. Terminate portal client immediately
        if portal_process:
            logger.info("Terminating portal client...")
            portal_process.terminate()
            # We will wait for it at the very end
            
        # 2. Close WebSocket server
        if 'server' in locals():
            logger.info("Closing WebSocket server...")
            server.close()
            
            # Cancel all active connection tasks
            if active_tasks:
                logger.info(f"Cancelling {len(active_tasks)} active connection tasks...")
                for task in active_tasks:
                    task.cancel()
                    
            try:
                # Wait for handlers to finish, but not forever
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for connections to close, forcing shutdown.")
            except Exception as e:
                logger.error(f"Error during server shutdown: {e}")

        # 3. Final cleanup for portal process
        if portal_process:
            try:
                portal_process.wait(timeout=1.0)
                logger.info("Portal client terminated.")
            except subprocess.TimeoutExpired:
                logger.warning("Portal client didn't terminate, killing...")
                portal_process.kill()
                portal_process.wait()
                logger.info("Portal client killed.")
        
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass # Handle clean exit
