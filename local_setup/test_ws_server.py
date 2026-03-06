from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
import json

app = FastAPI()

@app.post('/twilio_connect')
async def twilio_connect(bolna_host: str, agent_id: str):
    bolna_websocket_url = f'{bolna_host}/chat/v1/{agent_id}'
    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{bolna_websocket_url}" />
    </Connect>
</Response>"""
    from fastapi.responses import Response
    return Response(content=xml_response, media_type="text/xml")

@app.websocket("/chat/v1/{agent_id}")
async def websocket_endpoint(websocket: WebSocket, agent_id: str):
    print(f"WS Connection received for agent {agent_id}!")
    try:
        await websocket.accept()
        print("WS Accepted!")
        
        while True:
            data = await websocket.receive_text()
            print(f"Received from Twilio: {data}")
            
            # If start event, respond with a test string (base64 audio) or just acknowledge
            packet = json.loads(data)
            if packet.get("event") == "start":
                print("Received START event!")
                
    except WebSocketDisconnect as e:
        print(f"Disconnected: {e}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    print("Starting test WS server on port 5001")
    uvicorn.run(app, host="0.0.0.0", port=5001)
