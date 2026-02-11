import asyncio
import websockets
import json
import logging
import msgpack
import base64
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_protocol")

SERVER_URI = "ws://127.0.0.1:7274"

async def test_protocol():
    async with websockets.connect(SERVER_URI, max_size=10*1024*1024) as websocket:
        # Hello
        await websocket.recv()
        # Identify
        await websocket.send(json.dumps({"op": 1, "d": {"rpcVersion": 1}}))
        await websocket.recv()

        # 1. GetVersion
        logger.info("Testing GetVersion...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {"requestType": "GetVersion", "requestId": "1"}
        }))
        resp = json.loads(await websocket.recv())
        assert resp["d"]["requestType"] == "GetVersion"
        available = resp["d"]["responseData"]["availableRequests"]
        assert "GetOutputList" in available
        assert "GetSpecialInputs" in available
        formats = resp["d"]["responseData"]["supportedImageFormats"]
        assert "jpg" in formats
        logger.info("GetVersion Passed")

        # 2. GetSceneList
        logger.info("Testing GetSceneList...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {"requestType": "GetSceneList", "requestId": "2"}
        }))
        resp = json.loads(await websocket.recv())
        scenes = resp["d"]["responseData"]["scenes"]
        assert len(scenes) >= 1
        logger.info(f"GetSceneList Passed (Initial Name: {scenes[0]['sceneName']})")

        # 3. GetCurrentProgramScene
        logger.info("Testing GetCurrentProgramScene...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {"requestType": "GetCurrentProgramScene", "requestId": "3"}
        }))
        resp = json.loads(await websocket.recv())
        assert "currentProgramSceneName" in resp["d"]["responseData"]
        logger.info(f"GetCurrentProgramScene Passed (Name: {resp['d']['responseData']['currentProgramSceneName']})")

        # 4. GetVideoSettings
        logger.info("Testing GetVideoSettings...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {"requestType": "GetVideoSettings", "requestId": "4"}
        }))
        resp = json.loads(await websocket.recv())
        assert resp["d"]["responseData"]["baseWidth"] == 2560
        assert resp["d"]["responseData"]["outputWidth"] == 1920
        logger.info("GetVideoSettings Passed")

        # 5. GetSceneItemList
        logger.info("Testing GetSceneItemList...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {"requestType": "GetSceneItemList", "requestId": "5", "requestData": {"sceneName": "Cartagra"}}
        }))
        resp = json.loads(await websocket.recv())
        items = resp["d"]["responseData"]["sceneItems"]
        assert len(items) == 1
        assert items[0]["sourceName"] == "Michadame"
        logger.info("GetSceneItemList Passed")

        # 7. GetSourceScreenshot
        logger.info("Testing GetSourceScreenshot...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {"requestType": "GetSourceScreenshot", "requestId": "7", "requestData": {"sourceName": "Michadame", "imageFormat": "jpg"}}
        }))
        resp = json.loads(await websocket.recv())
        assert "imageData" in resp["d"]["responseData"]
        logger.info("GetSourceScreenshot (Original) Passed")

        # 7b. GetSourceScreenshot with Resizing
        logger.info("Testing GetSourceScreenshot with resizing (400x300)...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {
                "requestType": "GetSourceScreenshot", 
                "requestId": "7b", 
                "requestData": {
                    "sourceName": "Michadame", 
                    "imageFormat": "jpg",
                    "imageWidth": 400,
                    "imageHeight": 300
                }
            }
        }))
        resp = json.loads(await websocket.recv())
        img_data = base64.b64decode(resp["d"]["responseData"]["imageData"])
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_data))
        logger.info(f"Received image size: {img.size}")
        assert img.size == (400, 300)
        logger.info("GetSourceScreenshot Resizing Passed")

        # 8. GetRecordDirectory
        logger.info("Testing GetRecordDirectory...")
        await websocket.send(json.dumps({
            "op": 6,
            "d": {"requestType": "GetRecordDirectory", "requestId": "8"}
        }))
        resp = json.loads(await websocket.recv())
        assert "recordDirectory" in resp["d"]["responseData"]
        logger.info("GetRecordDirectory Passed")

        # 9. Test MessagePack (Op 6)
        logger.info("Testing MessagePack request...")
        msgpack_req = msgpack.packb({
            "op": 6,
            "d": {"requestType": "GetVersion", "requestId": "9"}
        })
        await websocket.send(msgpack_req)
        resp_raw = await websocket.recv()
        assert isinstance(resp_raw, bytes), "Expected binary response for msgpack request"
        resp = msgpack.unpackb(resp_raw, raw=False)
        assert resp["d"]["requestType"] == "GetVersion"
        logger.info("MessagePack Passed")

        logger.info("All protocol tests passed! Keeping connection open...")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logger.info("Mock client shutting down...")
            raise

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(test_protocol())
