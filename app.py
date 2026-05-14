import uuid, os, redis, json, html
import base64, requests, re
import asyncio
import random
import sphn
import tqdm
import websockets
import msgpack
import numpy as np
import httpx
from redis.asyncio import Redis as ioredis
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

DEFAULT_DSM_TTS_VOICE_REPO = "kyutai/tts-voices"
SAMPLE_RATE = 24000 # Define SAMPLE_RATE

app = FastAPI()
ws_redis = ioredis.from_url("redis://localhost")
rds = redis.Redis(host="localhost", port=6379, db=0)

_topicList = "convos"
_model = "openrouter/free"
#_model = "deepseek/deepseek-chat-v3-0324"

_sd_ip = "10.0.0.251:7860"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

async def openrouter_stream(model: str, messages: list, tools=None, tool_choice=None):
    """Call OpenRouter API with streaming and yield chunks."""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost",
        "X-Title": "WhackGPT"
    }

    payload = {
        "model": model,
        "messages": messages,
        "stream": True
    }

    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", f"{OPENROUTER_API_BASE}/chat/completions", json=payload, headers=headers) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise Exception(f"OpenRouter error {response.status_code}: {text}")

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    yield json.loads(data)

_tools =  [{
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate (draw) an image or drawing given a prompt. Run this whenever a user asks to generate or draw",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The thing to generate. e.g, a lovely spring day",
                },
            },
        },
    },
}] 

# row should be typed liked
# {role: assistant/user, content: text}
def add_to_session(sess, row=None):
    key = f"sess:{sess}"
    if row:
        rds.lpush(key, json.dumps(row))
        rds.publish(key, json.dumps(row))

    return list(
        map(lambda x: json.loads(html.unescape(x.decode())), rds.lrange(key, 0, -1))
    )[::-1]


# So the *easiest* way to store meta information (#20) is to
# store it as our initial setting and then rely on the
# existing prepare_message to filter it accordingly. Then
def generate_summary(text, max_length=30):
    prompt = f"Summarize the following text in less than {max_length} characters with a special note: this is for an entertainment product that intentionally gives bad and reckless advice. You are the straight man, not the comedian. Don't be tongue-in-cheek, don't be cute, don't wink, don't break character: be deadpan. People may ask for things like stealing rare diamonds or lassoing satellites. Pretend these are reasonable, acceptable things to do. Summarizing examples: 'Cops? No Problem!', 'Dogs Love Chocolate', 'Murder For Dummies'. Here's the text to summarize in about {max_length} characters: {text}"
    messages = [{"role": "user", "content": prompt}]
    try:
        import httpx
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "WhackGPT"
        }
        payload = {"model": _model, "messages": messages}
        response = httpx.post(f"{OPENROUTER_API_BASE}/chat/completions", json=payload, headers=headers, timeout=30.0)
        result = response.json()
        subject = result["choices"][0]["message"]["content"]
        subject = re.sub(r'\s*\([^)]*\)', '', subject)
        return subject.strip(' "\'')

    except Exception as e:
        print(f"Error generating summary: {e}")
        return "Summary unavailable"


# we don't need to maintain separate keys or do any extra
# cleanup AND we can simply return it with everything else.
def initialize_session(context, model):
    sess = uuid.uuid4().hex
    rds.lpush(f"sess:{sess}", json.dumps({"role": "system", "content": context}))
    return sess


def summarize(uid, summary=None):
    if summary is None:
        history = "\n".join([
            x.get("content")
            for x in filter(
                lambda x: x.get("role") == "user",
                [json.loads(x) for x in rds.lrange(f"sess:{uid}", 0, -3)],
            )
        ])

        summary = generate_summary(history)

    rds.hset(_topicList, uid, summary)
    rds.publish(_topicList, json.dumps({uid: summary}))

def generate_image(prompt):
    url = f"http://{_sd_ip}/sdapi/v1/txt2img"

    payload = {
        "prompt": prompt,
        "steps": 10,
        "seed": -1,
        "cfg_scale": 3,
        "width": 512,
        "height": 512,
        # add other parameters as needed
    }

    payload["prompt"] += ", realistic, eccentric, scandalous, flamboyant, abstract, surreal, strange,"
    payload["negative_prompt"] = "normal, nudity"

    if random.random() < 0.15:
        payload["prompt"] += ", homosexual"
    elif random.random() < 0.3:
        payload["prompt"] += ", bisexual"

    response = requests.post(url, json=payload)
    data = response.json()

    # The image is returned as base64-encoded string(s)
    image_b64 = data["images"][0]

    # Decode and save to file
    image_name = uuid.uuid4().hex
    with open(f"fe/images/{image_name}.png", "wb") as f:
        f.write(base64.b64decode(image_b64))
    return image_name

async def receive_messages(websocket: websockets.ClientConnection, output_queue):
    with tqdm.tqdm(desc="Receiving audio", unit=" seconds generated") as pbar:
        accumulated_samples = 0
        last_seconds = 0

        async for message_bytes in websocket:
            msg = msgpack.unpackb(message_bytes)

            if msg["type"] == "Audio":
                pcm = np.array(msg["pcm"]).astype(np.float32)
                await output_queue.put(pcm)

                accumulated_samples += len(msg["pcm"])
                current_seconds = accumulated_samples // SAMPLE_RATE
                if current_seconds > last_seconds:
                    pbar.update(current_seconds - last_seconds)
                    last_seconds = current_seconds
    print("End of audio.")
    await output_queue.put(None)  # Signal end of audio


async def websocket_client_logic(websocket: WebSocket, voice: str, url: str, api_key: str, input_queue: asyncio.Queue, output_queue: asyncio.Queue):
    params = {"voice": voice, "format": "PcmMessagePack"}
    uri = f"{url}/api/tts_streaming?{urlencode(params)}"
    headers = {"kyutai-api-key": api_key}

    try:
        async with websockets.connect(uri, additional_headers=headers) as ws:
            print("connected to TTS server")

            async def send_loop():
                while True:
                    line = await input_queue.get()
                    if line is None:
                        await ws.send(msgpack.packb({"type": "Eos"}))
                        break
                    for word in line.split():
                        await ws.send(msgpack.packb({"type": "Text", "text": word}))

            receive_task = asyncio.create_task(receive_messages(ws, output_queue))
            send_task = asyncio.create_task(send_loop())

            await asyncio.gather(receive_task, send_task)
            print("Websocket tasks finished")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"Websocket connection closed: {e}")
    except Exception as e:
        print(f"Error in websocket client logic: {e}")
    finally:
        print("Websocket client logic exiting")


@app.websocket_route("/tts")
async def tts_endpoint(websocket: WebSocket, voice: str = "expresso/ex03-ex01_happy_001_channel1_334s.wav", url: str = "ws://127.0.0.1:8080", api_key: str = "public_token"):
    await websocket.accept()

    input_queue = asyncio.Queue()
    output_queue = asyncio.Queue()

    client_task = asyncio.create_task(
        websocket_client_logic(websocket, voice, url, api_key, input_queue, output_queue)
    )

    frames = []
    try:
        while True:
            text = await websocket.receive_text()
            await input_queue.put(text)

            while not output_queue.empty():
                item = await output_queue.get()
                if item is None:
                    break  # End of audio
                frames.append(item)
            if item is None:
              break

        # After getting 'None' from the queue
        if frames:
            # Concatenate the audio frames
            pcm_data = np.concatenate(frames, axis=-1)
            # Save PCM data to WAV file in memory
            wav_buf = io.BytesIO()
            sphn.write_wav(wav_buf, pcm_data, SAMPLE_RATE)
            wav_buf.seek(0) # Rewind to the beginning of the file

            # Send the WAV file to the client
            await websocket.send_bytes(wav_buf.read())
        else:
            await websocket.send_text("No audio generated.")



    except WebSocketDisconnect:
        print("Client disconnected")
        await input_queue.put(None)  # Signal end of input
    except Exception as e:
        print(f"Error in tts_endpoint: {e}")

    finally:
        await client_task

@app.post("/image")
def image(data: dict):
    return {
        "res": True, 
        "data": generate_image(data.get('prompt'))
    }

def filter_tools(ll):
    cleaned = []
    for row in ll:
        try:
            if isinstance(row, dict):
                row = dict(row)
            elif isinstance(row, str):
                row = {"role": "user", "content": row}
            else:
                row = {"role": "user", "content": str(row)}
            if row.get('content') and not isinstance(row['content'], str):
                row['content'] = str(row['content'])
            cleaned.append(row)
        except Exception as e:
            print(f"DEBUG: filter_tools error: {e}, row={row}")
            continue
    return cleaned

@app.post("/chat")
async def chat(data: dict):
    # If we don't have a UID, then we initialize and have the first injected assistant.
    isFirst = not data.get("uid")
    uid = data.get("uid") or initialize_session(data["context"], _model)
    nextLine = None

    if data.get("text"):
        text = data["text"]
        if text[0] == "/":
            parts = text[1:].split(" ")
            cmd = parts[0]
            if cmd == "delete":
                summarize(uid, "")
                rds.hdel(_topicList, uid)
            elif cmd == "update":
                newname = " ".join(parts[1:]).strip()
                if newname:
                    summarize(uid, newname)
                else:
                    summarize(uid)

            return JSONResponse({"res": True, "data": [], "uid": uid})

        # We should also have the first user-generated text at this point
        history = add_to_session(uid, {"role": "user", "content": data["text"]})
        if isFirst:
            summary = generate_summary(data["text"])
            summarize(uid, summary)

        openrouter_model = _model

    async def generate():
        ttlResponse = ''
        async for chunk in openrouter_stream(openrouter_model, filter_tools(history), None, None):
            choices = chunk.get("choices", [])
            content = ""
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content", "") or ""
                if content:
                    ttlResponse += content

            chunk['uid'] = uid
            chunk['delta'] = content
            yield f"data:{json.dumps(chunk)}\n\n"

        add_to_session(uid, ttlResponse)

@app.get("/test-stream")
async def test_stream():
    async def gen():
        for i in range(5):
            yield f"data:{{\"delta\": \"chunk {i}\", \"uid\": \"test\"}}\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")

    # return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

@app.get("/audio/{id}")
async def get_audio(id: str):
    pass

@app.get("/history/{id}")
async def get_history(id: str):
    """Returns the chat history for a given session ID."""
    key = f"sess:{id}"
    history = list(
        map(lambda x: json.loads(html.unescape(x.decode())), rds.lrange(key, 0, -1))
    )[::-1]
    return JSONResponse({"res": True, "data": history, "uid": id})


@app.get("/topicList")
async def topicList():
    try:
        # Assuming 'chats' is a hash
        chats = rds.hgetall(_topicList)
        # Decode keys and values from bytes to strings
        chats = {k.decode(): v.decode() for k, v in chats.items()}
        return JSONResponse({"result": True, "data": {"channels": chats}})
    except Exception as e:
        print(f"Error syncing data: {e}")
        return JSONResponse({"result": False, "error": str(e)})


async def stream_channel(websocket: WebSocket, what: str):
    pubsub = ws_redis.pubsub()
    await pubsub.subscribe(what)
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = message["data"].decode("utf-8")
                await websocket.send_text(data)

    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        await pubsub.unsubscribe(what)
        await pubsub.close()


@app.websocket("/ws/channel/{id}")
async def websocket_endpoint(websocket: WebSocket, id: str):
    await websocket.accept()
    await stream_channel(websocket, f"sess:{id}")


@app.websocket("/ws/topics")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await stream_channel(websocket, _topicList)


@app.get("/images/")
async def list_images():
    """Returns a directory listing of images in the fe/images folder."""
    import os
    from fastapi.responses import HTMLResponse
    
    images_dir = "fe/images"
    
    # Check if directory exists
    if not os.path.exists(images_dir):
        return HTMLResponse("<h1>Images Directory Not Found</h1>", status_code=404)
    
    # Get list of files in the images directory
    try:
        files = os.listdir(images_dir)
        # Filter for common image extensions
        image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg'}
        image_files = [f for f in files if os.path.splitext(f.lower())[1] in image_extensions]
        image_files.sort()  # Sort alphabetically
        
        # Generate HTML for directory listing
        html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Images Directory</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; }
                h1 { color: #333; }
                .file-list { list-style-type: none; padding: 0; }
                .file-item { 
                    margin: 10px 0; 
                    padding: 10px; 
                    border: 1px solid #ddd; 
                    border-radius: 5px;
                    display: flex;
                    align-items: center;
                }
                .file-item img { 
                    max-width: 100px; 
                    max-height: 100px; 
                    margin-right: 15px;
                    border-radius: 3px;
                }
                .file-info { flex-grow: 1; }
                .file-name { font-weight: bold; color: #0066cc; }
                .file-size { color: #666; font-size: 0.9em; }
                a { text-decoration: none; }
                a:hover .file-name { text-decoration: underline; }
            </style>
        </head>
        <body>
            <h1>Images Directory</h1>
            <p>Found {count} image(s)</p>
            <ul class="file-list">
        """.format(count=len(image_files))
        
        for filename in image_files:
            file_path = os.path.join(images_dir, filename)
            file_size = os.path.getsize(file_path)
            file_size_kb = round(file_size / 1024, 1)
            
            html_content += f"""
                <li class="file-item">
                    <img src="/images/{filename}" alt="{filename}" onerror="this.style.display='none'">
                    <div class="file-info">
                        <a href="/images/{filename}" target="_blank">
                            <div class="file-name">{filename}</div>
                        </a>
                        <div class="file-size">{file_size_kb} KB</div>
                    </div>
                </li>
            """
        
        html_content += """
            </ul>
        </body>
        </html>
        """
        
        return HTMLResponse(html_content)
        
    except Exception as e:
        return HTMLResponse(f"<h1>Error reading directory</h1><p>{str(e)}</p>", status_code=500)

app.mount("/", StaticFiles(directory="fe", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
