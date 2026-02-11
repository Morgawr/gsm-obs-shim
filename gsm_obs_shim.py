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

# Configure traffic logger
traffic_logger = logging.getLogger("traffic")
traffic_logger.setLevel(logging.INFO)
traffic_logger.propagate = False # Don't propagate to root logger
traffic_handler = logging.FileHandler(TRAFFIC_LOG_FILE)
traffic_handler.setFormatter(logging.Formatter('%(message)s'))
traffic_logger.addHandler(traffic_handler)

shutdown_event = asyncio.Event()
active_tasks = set()

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
                        response_data = {
                            "currentPreviewSceneName": None,
                            "currentPreviewSceneUuid": None,
                            "currentProgramSceneName": ARGS.game_name,
                            "currentProgramSceneUuid": "3be8ae7c-585a-4b95-937f-89c691af657b",
                            "scenes": [
                                {"sceneIndex": 0, "sceneName": ARGS.game_name, "sceneUuid": "3be8ae7c-585a-4b95-937f-89c691af657b"}
                            ]
                        }

                    elif request_type == "GetCurrentProgramScene":
                        response_data = {
                            "currentProgramSceneName": ARGS.game_name,
                            "currentProgramSceneUuid": "3be8ae7c-585a-4b95-937f-89c691af657b",
                            "sceneName": ARGS.game_name,
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
                        
                        # Check if portal source is selected
                        if hasattr(ARGS, 'portal_node_id') and ARGS.portal_node_id:
                            try:
                                node_id = ARGS.portal_node_id
                                tmp_file = f"/tmp/capture_{uuid.uuid4().hex}.jpg"
                                # gst-launch-1.0 pipewiresrc path=<ID> num-buffers=1 ! jpegenc ! filesink location=<FILE>
                                cmd = [
                                    "gst-launch-1.0", 
                                    "pipewiresrc", f"path={node_id}", "num-buffers=1", 
                                    "!", "videoconvert", # Ensure format compatibility
                                    "!", "jpegenc", 
                                    "!", "filesink", f"location={tmp_file}"
                                ]
                                
                                logger.info(f"Capturing frame from Node {node_id}...")
                                # Run capture
                                try:
                                    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=2)
                                except subprocess.CalledProcessError as e:
                                    logger.error(f"GStreamer failed (Exit {e.returncode}):")
                                    logger.error(f"STDOUT: {e.stdout}")
                                    logger.error(f"STDERR: {e.stderr}")
                                    raise
                                
                                with open(tmp_file, "rb") as f:
                                    encoded_string = base64.b64encode(f.read()).decode('utf-8')
                                    fake_image_data = encoded_string
                                
                                os.remove(tmp_file)
                                logger.info("Frame captured successfully.")
                                
                            except Exception as e:
                                logger.error(f"Failed to capture frame: {e}")
                                # Fallback will kick in if fake_image_data is None

                        if fake_image_data is None:
                            try:
                                with open("fake_image.jpg", "rb") as image_file:
                                    encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                                    fake_image_data = encoded_string
                            except FileNotFoundError:
                                logger.error("fake_image.jpg not found, using placeholder")
                                # Fallback to 1x1 white pixel
                                fake_image_data = "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wAALCAABAAEBAREA/8QAJgABAAAAAAAAAAAAAAAAAAAAAxABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAAPwBH/9k="
                        
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
    parser.add_argument("--select-source", action="store_true", help="Select window using XDG Portal")
    parser.add_argument("--game-name", help="Name of the game (required in mock mode)")
    ARGS = parser.parse_args()

    if not ARGS.proxy and not ARGS.game_name:
        parser.error("--game-name is required when not in proxy mode")
    ARGS.portal_node_id = None

    portal_process = None
    if ARGS.select_source:
        logger.info("Starting Portal Client for window selection...")
        try:
            # Use system python for dbus-python support
            # Use unbuffered output (-u) to ensure we get the ID immediately
            portal_process = subprocess.Popen(
                ["/usr/bin/python3", "-u", "portal_client.py"],
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
