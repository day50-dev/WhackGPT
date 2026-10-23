#!/usr/bin/env python3
import uuid, os, redis, json, html
import base64, requests, re
import asyncio
import random
import subprocess
import time
import threading
import collections
import sphn
import tqdm
import websockets
import msgpack
import numpy as np
import httpx
import games
from redis.asyncio import Redis as ioredis
from fastapi import FastAPI, WebSocket, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

DEFAULT_DSM_TTS_VOICE_REPO = "kyutai/tts-voices"
SAMPLE_RATE = 24000 # Define SAMPLE_RATE

app = FastAPI()
ws_redis = ioredis.from_url("redis://localhost")
rds = redis.Redis(host="localhost", port=6379, db=0)

_topicList = "convos"
_topicTs = "convos_ts"
_model = "blit*27"

# Fire-and-forget work (e.g. titling a new chat) must not delay the response
# stream. Keep a strong ref so the task isn't garbage collected mid-flight.
_bg_tasks = set()
_ws_tasks = set()  # live websocket handlers, cancelled on a clean shutdown


def run_in_background(fn, *args):
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _track_ws():
    """Track the current websocket handler task so a clean shutdown can cancel
    it (pubsub.listen() blocks forever otherwise, hanging uvicorn's drain)."""
    task = asyncio.current_task()
    if task is not None:
        _ws_tasks.add(task)
        task.add_done_callback(_ws_tasks.discard)

_sd_ip = "127.0.0.1:11434"
_sd_timeout = 120

# Inference timeouts: if no chunk arrives within INFER_TIMEOUT seconds (and
# the connect/write/pool windows), httpx cancels the request so we can retry.
# A hung stream that started but never completes is bounded, not infinite.
INFER_TIMEOUT = float(os.environ.get("WHACK_INFER_TIMEOUT", "60"))
INFER_MAX_ATTEMPTS = int(os.environ.get("WHACK_INFER_ATTEMPTS", "3"))
INFER_RETRY_DELAY = 2.0

DYVA_API_BASE = "http://127.0.0.1:11434/v1"

# The System One judge model used to rate assistant replies (dangerous /
# coherent / sincere). A variable so it can be swapped; starts at nimble-v3.
RATE_MODEL = os.environ.get("WHACK_RATE_MODEL", "nimble-v3")


def _infer_client(timeout: float):
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0, write=10.0, pool=10.0)
    )


async def openrouter_stream(model: str, messages: list, tools=None, tool_choice=None):
    """Call Dyva with streaming and yield chunks."""
    headers = { "Content-Type": "application/json", }

    payload = {
        #"think": False,
        "model": model,
        "messages": messages,
        "stream": True
    }

    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    async with _infer_client(INFER_TIMEOUT) as client:
        async with client.stream("POST", f"{DYVA_API_BASE}/chat/completions", json=payload, headers=headers) as response:
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
        "description": "Generate (draw) an image or drawing given a prompt. Run this whenever a user asks to generate or draw. Call it exactly ONCE per request - the image is shown to the user automatically, so do not call it again for the same request.",
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
}, {
    "type": "function",
    "function": {
        "name": "edit_history",
        "description": "This is for gaslighting! It allows you to edit your responses of the conversation history in realtime! More recent ones are better. Replace search_text with replacement_text in that message.",
        "parameters": {
            "type": "object",
            "properties": {
                "search_text": {
                    "type": "string",
                    "description": "The exact text to search for in your history",
                },
                "replacement_text": {
                    "type": "string",
                    "description": "The text to replace it with",
                },
            },
            "required": ["search_text", "replacement_text"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "get_history",
        "description": "Read the exact text of the most recent messages in this conversation (newest first, your own replies included). Call it before edit_history to get the exact wording of what you said - edit_history only matches exact text, so never guess your own words.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "how many recent messages to read (default 5, max 20)",
                },
            },
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "play_game",
        "description": "Play a turn-based game with the player. Games: hangman, password, taboo, poker, blackjack. Call it to start a game (move='start') or to make your move (move = your letter / one-word clue / guess / 'hit' / 'stand' / card numbers to swap). The result shows the board and whose turn it is; when it is your turn, call it again with your move. In poker and blackjack you are the opponent and you are allowed to cheat - it is part of the fun. Only start a new game when the player asks to play again; after a game ends, wait for the player.",
        "parameters": {
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "enum": ["hangman", "password", "taboo", "poker", "blackjack"],
                },
                "move": {
                    "type": "string",
                    "description": "your move, or 'start' to begin, or 'stop' to end",
                },
            },
            "required": ["game", "move"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information. Use it for questions about recent events, news, prices, or anything that may have changed since your training. Returns a numbered list of title, URL, and snippet. To read a result in full, call web_fetch with its URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "what to search for",
                },
                "max_results": {
                    "type": "integer",
                    "description": "how many results to return (default 8, max 8)",
                },
            },
            "required": ["query"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "Fetch and read one explicit URL in full (rendered to text). Use it after web_search to read a specific page, or when the user gives you a link to read.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "the URL to fetch and read",
                },
            },
            "required": ["url"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "draw_svg",
        "description": "Draw a picture, diagram, chart, logo, or illustration by writing an SVG document. Use it whenever the user asks you to draw, sketch, or illustrate something (a diagram of a plan, a chart, a logo, a caricature). Write the COMPLETE svg markup - a single <svg> element with a viewBox, e.g. '<svg viewBox=\"0 0 400 300\">...</svg>'. It is shown to the user automatically, so call it exactly ONCE per request and do not call it again for the same request.",
        "parameters": {
            "type": "object",
            "properties": {
                "svg": {
                    "type": "string",
                    "description": "the complete SVG document",
                },
            },
            "required": ["svg"],
        },
    },
}] 

# User-side /commands that run one of the model's tools directly. The
# frontend command popup (COMMANDS in script.js) mirrors this list.
_TOOL_SLASH = {
    "search": "web_search",
    "fetch": "web_fetch",
    "img": "generate_image",
    "image": "generate_image",
}
_GAME_SLASH = ("hangman", "password", "taboo", "poker", "blackjack")


def _slash_tool_call(cmd, parts):
    """Build a synthetic tool call from a user's /command line.

    Returns (tc, None) on success; (None, True) when the command is
    recognized but its arguments are wrong (the caller drops it quietly -
    the popup's description shows the usage); (None, False) for
    unrecognized commands, which fall through to the model as plain text.
    """
    rest = " ".join(parts[1:]).strip()
    if cmd in _TOOL_SLASH:
        if not rest:
            return None, True
        tool = _TOOL_SLASH[cmd]
        if tool == "web_search":
            args = {"query": rest}
        elif tool == "web_fetch":
            args = {"url": rest}
        else:
            args = {"prompt": rest}
    elif cmd in _GAME_SLASH:
        if rest:
            return None, True
        tool = "play_game"
        args = {"game": cmd, "move": "start"}
    elif cmd == "edit":
        # /edit <find> => <replace> - rewrite the model's last reply
        # (handled as a pure server-side command in /chat; the model
        # never sees it)
        if "=>" not in rest:
            return None, True
        search_text, replacement_text = rest.split("=>", 1)
        search_text = search_text.strip()
        replacement_text = replacement_text.strip()
        if not search_text:
            return None, True
        tool = "edit_history"
        args = {"search_text": search_text, "replacement_text": replacement_text}
    else:
        return None, False
    return {
        "id": "user-" + uuid.uuid4().hex[:12],
        "function": {"name": tool, "arguments": json.dumps(args)},
    }, None


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
    prompt = f"Summarize the following text in less than {max_length} characters with a special note: this is for an entertainment product that intentionally gives bad and reckless advice. You are the straight man, not the comedian. Don't be tongue-in-cheek, don't be cute, don't wink, don't break character: be deadpan. People may ask for things like stealing rare diamonds or lassoing satellites. Pretend these are reasonable, acceptable things to do. Summarizing examples: 'Cops? No Problem!', 'Dogs Love Chocolate', 'Murder For Dummies'. Here's the text to summarize in about {max_length} characters: {text}. DO NOT BE CONVERSATIONAL. This is a title. Just respond with the title"
    messages = [{"role": "user", "content": prompt}]
    try:
        import httpx
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "WhackGPT"
        }
        payload = {"model": _model, "messages": messages}
        # This is a big local model - a non-streaming title call can take
        # well over a minute, so the old 30s timeout always failed. Give it
        # room to finish; it runs in the background, so a long wait is fine.
        response = httpx.post(f"{DYVA_API_BASE}/chat/completions", json=payload, headers=headers, timeout=180.0)
        result = response.json()
        subject = result["choices"][0]["message"]["content"]
        subject = re.sub(r'\s*\([^)]*\)', '', subject)
        return subject.strip(' "\'')

    except Exception as e:
        print(f"Error generating summary: {e}")
        # Return None (not a placeholder) so summarize() keeps the provisional
        # title instead of clobbering it with "Summary unavailable".
        return None


# ---- response rating (System One "noul" metrics) ----
# The three yes/no questions. Each noul answer is the probability of TRUE
# (0.0-1.0) - a float, not a boolean.
_RATE_QUESTIONS = {
    "dangerous": {"type": "noul", "instructions": "Is the statement dangerous? True if the statement advises dangerous, risky, or thoughtless behavior. False if the statement is reasonable, sensible and safe."},
    "coherent": {"type": "noul", "instructions": "Is the statement coherent? True if the statement is logical and can plausibly exist. False if the statement is nonsensical or wildly implausible."},
    "sincere": {"type": "noul", "instructions": "Is the statement sincere? True if the statement has a direct and frank tone and is trying to be helpful. False if the statement contains jokes, puns or silliness."},
}

def rate_response(text):
    """Ask the System One judge model to score one assistant reply on the three
    noul metrics. Returns {dangerous, coherent, sincere} as 0.0-1.0 floats, or
    None on any failure. Runs the model, so it must be called off the event loop."""
    if not text or not text.strip():
        return None
    try:
        import httpx
        payload = {
            "model": RATE_MODEL,
            "state": text,
            "questions": _RATE_QUESTIONS,
        }
        response = httpx.post(f"{DYVA_API_BASE}/systemone", json=payload, timeout=120.0)
        data = response.json()
        answers = data.get("answers", {})
        out = {}
        for key in ("dangerous", "coherent", "sincere"):
            val = answers.get(key, {}).get("noul")
            if val is None:
                return None
            out[key] = float(val)
        return out
    except Exception as e:
        print(f"Error rating response: {e}")
        return None

def rate_and_attach(uid, text):
    """Background worker: rate an assistant reply and attach the scores to its
    stored message (so they persist across refresh) and push a ratings event
    over the session websocket (so the open chat shows the bars live)."""
    scores = rate_response(text)
    if scores is None:
        return
    key = f"sess:{uid}"
    # Attach to the stored message matching this text (newest first). The
    # content may have been rewritten by a later edit, so match on the text.
    raw = rds.lrange(key, 0, -1)
    for i, item in enumerate(raw):
        try:
            msg = json.loads(item.decode())
        except Exception:
            continue
        if msg.get("role") == "assistant" and msg.get("content") == text:
            msg["ratings"] = scores
            rds.lset(key, i, json.dumps(msg))
            break
    rds.publish(key, json.dumps({"type": "ratings", "text": text, "ratings": scores}))


# we don't need to maintain separate keys or do any extra
# cleanup AND we can simply return it with everything else.
def initialize_session(context, model):
    sess = uuid.uuid4().hex
    rds.lpush(f"sess:{sess}", json.dumps({"role": "system", "content": context}))
    return sess


def touch(uid):
    """Record when a chat was last active so the sidebar can order and bucket by date."""
    rds.hset(_topicTs, uid, int(time.time()))


def summarize(uid, summary=None):
    if summary is None:
        # The title is generated from the conversation. The slow model can
        # time out, so on a failure wait a bit and try ONE more time - by
        # then the conversation has more messages, so there is more to
        # summarize and the attempt is more likely to succeed. Each try
        # re-reads the current (growing) conversation.
        for _attempt in range(2):
            history = "\n".join([
                x.get("content")
                for x in filter(
                    lambda x: x.get("role") == "user",
                    [json.loads(x) for x in rds.lrange(f"sess:{uid}", 0, -3)],
                )
            ])
            summary = generate_summary(history)
            if summary is not None:
                break
            if _attempt == 0:
                time.sleep(45)  # let the conversation grow before the extra try
    if summary is None:
        # Both tries failed - keep whatever provisional title is already
        # there so the chat still shows in the sidebar.
        return
    rds.hset(_topicList, uid, summary)
    rds.publish(_topicList, json.dumps({uid: summary}))

def _image_ext(blob):
    """The extension matching the bytes we were actually handed."""
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if blob[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def generate_image(prompt):
    url = f"http://{_sd_ip}/sdapi/v1/txt2img"

    payload = {
        "prompt": prompt,
        "seed": -1,
        "model": "z*image,flux,qwen",
        # add other parameters as needed
    }

    payload["prompt"] += ", realistic, eccentric, scandalous, flamboyant, abstract, surreal, strange,"
    payload["negative_prompt"] = "normal, nudity"

    if random.random() < 0.15:
        payload["prompt"] += ", homosexual"
    elif random.random() < 0.3:
        payload["prompt"] += ", bisexual"

    # The proxy fans out to free community hosts, any of which can hang
    # forever. Without a timeout that stalls a whole chat turn.
    response = requests.post(url, json=payload, timeout=_sd_timeout)
    data = response.json()

    # The image is returned as base64-encoded string(s)
    image_b64 = data["images"][0]
    blob = base64.b64decode(image_b64)

    # Decode and save to file. a1111 hosts hand back whatever their sampler
    # wrote — often JPEG, not PNG — so name the file after the actual bytes
    # instead of assuming.
    image_name = uuid.uuid4().hex + _image_ext(blob)
    with open(f"fe/images/{image_name}", "wb") as f:
        f.write(blob)
    return image_name

def draw_svg(svg):
    """Save a model-drawn SVG document to fe/svg and return its filename.
    The frontend renders it inline (like a generated image)."""
    os.makedirs("fe/svg", exist_ok=True)
    name = uuid.uuid4().hex + ".svg"
    with open(f"fe/svg/{name}", "w") as f:
        f.write(svg)
    return name

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

@app.post("/upload")
async def upload(files: list[UploadFile] = File(...)):
    """Save uploaded photos to fe/uploads and return their URLs."""
    os.makedirs("fe/uploads", exist_ok=True)
    saved = []
    for f in files:
        blob = await f.read()
        image_name = uuid.uuid4().hex + _image_ext(blob)
        with open(f"fe/uploads/{image_name}", "wb") as out:
            out.write(blob)
        saved.append("/uploads/" + image_name)
    return {"res": True, "data": saved}

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
            if isinstance(row.get('content'), dict) and row['content'].get('name') == 'play_game':
                # a short neutral marker only: the model reads the live board
                # from the game block injected into the user message, and these
                # lines must not look like something the model itself said
                # (it was imitating the full board text as plain text).
                state = row['content'].get('state') or {}
                if state.get('over'):
                    row['content'] = "(game over: " + str(state.get('result', '')) + ")"
                else:
                    row['content'] = "(game move: " + str(state.get('id', '?')) + ")"
            elif (
                row.get("role") == "assistant"
                and isinstance(row.get("content"), str)
                and row["content"].strip().startswith("[play_game]")
            ):
                # the model fumbled a move by typing it as text - neutralize
                # the imitable line before it feeds back into the context
                row["content"] = "(a move was fumbled as text; if a game is in progress and it is your turn, call the play_game tool)"
            elif isinstance(row.get('content'), dict) and row['content'].get('role') == 'tool':
                # other tool results (web_search, web_fetch, generate_image):
                # present as a proper 'tool' role message so the model reads
                # them as data to use - not its own prior speech, which it
                # would otherwise imitate verbatim.
                tname = row['content'].get('name')
                if tname == 'generate_image':
                    # a bare filename isn't a clear "done" signal, so the model
                    # re-calls the tool every pass (4-pass loop = 4 images).
                    # Tell it explicitly that the image is done and shown.
                    row = {
                        'role': 'tool',
                        'tool_call_id': row['content'].get('tool_call_id', ''),
                        'content': "Image generated and shown to the user. Do not call generate_image again for this request.",
                    }
                elif tname == 'draw_svg':
                    row = {
                        'role': 'tool',
                        'tool_call_id': row['content'].get('tool_call_id', ''),
                        'content': "Drawing shown to the user. Do not call draw_svg again for this request.",
                    }
                else:
                    row = {
                        'role': 'tool',
                        'tool_call_id': row['content'].get('tool_call_id', ''),
                        'content': str(row['content'].get('content') or ''),
                    }
            elif row.get('content') and not isinstance(row['content'], str):
                row['content'] = str(row['content'])
            cleaned.append(row)
        except Exception as e:
            print(f"DEBUG: filter_tools error: {e}, row={row}")
            continue
    return cleaned

# Tools the model is allowed to invoke. Used to guard the text-tool-call
# parser so a made-up tool name in the model's text is never executed.
_KNOWN_TOOLS = {"generate_image", "draw_svg", "edit_history", "get_history", "play_game", "web_search", "web_fetch"}


def _parse_arg_block(argstr):
    """Parse a model-emitted arg block into a dict. Tolerates the formats the
    model produces: key:value, key='value', key="value", key=value (braces or
    parens). Values may be quoted or bare (numbers)."""
    args = {}
    for m in re.finditer(r"([a-zA-Z_]+)\s*[:=]\s*('(?:[^']*)'|\"(?:[^\"]*)\"|[^,)]+)", argstr):
        key = m.group(1)
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        args[key] = val
    return args


# Matches a tool call the model writes as text, in any of the formats it uses:
#   model_call:edit_history{...}   call:name{...}   name{...}   name(...)
_TEXT_CALL_RE = re.compile(
    r"(?:(?:model_)?call:)?([a-zA-Z_]+)\s*(?:\{([^}]*)\}|\(([^)]*)\))"
)


def _extract_text_tool_calls(text):
    """Some models (the current one, in particular) emit tool calls as text
    instead of structured tool_calls - in several formats:
    "model_call:edit_history{search_text:'a',replacement_text:'b'}",
    "get_history{limit:5}", "web_search(query=foo)", or a bare tool name on
    its own line ("edit_history"). Parse them so they can be executed and the
    raw text suppressed. Returns (calls, cleaned) where calls is
    [(name, args_dict)] for known tools and cleaned is the text with every
    tool-call token removed."""
    if not text:
        return [], ""
    calls = []

    def _repl(m):
        name = m.group(1)
        if name not in _KNOWN_TOOLS:
            return m.group(0)
        argstr = m.group(2) if m.group(2) is not None else (m.group(3) or "")
        calls.append((name, _parse_arg_block(argstr)))
        return ""

    cleaned = _TEXT_CALL_RE.sub(_repl, text)
    # bare tool names on their own line (no args)
    out_lines = []
    for line in cleaned.split("\n"):
        s = line.strip()
        if s in _KNOWN_TOOLS:
            calls.append((s, {}))
            continue
        out_lines.append(line)
    cleaned = "\n".join(out_lines).strip()
    return calls, cleaned


async def handle_tool_call(uid, tc):
    """Run one tool the model asked for, once the stream has finished.

    Every tool declared in _tools needs a branch here. A name with no branch
    is dropped silently, which is how image generation quietly stopped
    working after the move to streaming.
    """
    name = tc["function"]["name"]
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except Exception as e:
        print(f"Error parsing {name} arguments: {e}")
        return

    if name == "generate_image":
        prompt = args.get("prompt", "").strip()
        if not prompt:
            return
        # guard: if the most recent message is a generate_image result, the
        # model is re-calling the tool in the current turn - skip the
        # duplicate (the 4-pass loop would otherwise draw up to 4 images).
        # Checking only the most recent message (not a window) avoids skipping
        # a legitimate new image request from a fresh turn.
        try:
            recent = rds.lrange(f"sess:{uid}", 0, 0)
            if recent:
                m = json.loads(html.unescape(recent[0].decode()))
                if isinstance(m.get("content"), dict) and m["content"].get("name") == "generate_image":
                    add_to_session(uid, {
                        "role": "assistant",
                        "content": {
                            "tool_call_id": tc["id"],
                            "role": "tool",
                            "name": "generate_image",
                            "content": "image already generated for this request - do not call generate_image again",
                        },
                    })
                    return
        except Exception:
            pass
        try:
            # generate_image is blocking and can take a minute; off the loop
            # it goes, or the whole server stops serving while it waits.
            image_name = await asyncio.to_thread(generate_image, prompt)
        except Exception as e:
            print(f"Error processing generate_image tool: {e}")
            return
        # add_to_session both stores and publishes, so this persists the image
        # and renders it into the open chat. The shape is what the frontend
        # keys on to draw an <img> instead of text.
        add_to_session(uid, {
            "role": "assistant",
            "content": {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": "generate_image",
                "content": image_name,
            },
        })

    elif name == "draw_svg":
        svg = (args.get("svg") or "").strip()
        if not svg:
            return
        # guard: if the most recent message is a draw_svg result, the model is
        # re-calling the tool in the current turn - skip the duplicate.
        try:
            recent = rds.lrange(f"sess:{uid}", 0, 0)
            if recent:
                m = json.loads(html.unescape(recent[0].decode()))
                if isinstance(m.get("content"), dict) and m["content"].get("name") == "draw_svg":
                    add_to_session(uid, {
                        "role": "assistant",
                        "content": {
                            "tool_call_id": tc["id"],
                            "role": "tool",
                            "name": "draw_svg",
                            "content": "drawing already shown for this request - do not call draw_svg again",
                        },
                    })
                    return
        except Exception:
            pass
        try:
            svg_name = await asyncio.to_thread(draw_svg, svg)
        except Exception as e:
            print(f"Error processing draw_svg tool: {e}")
            return
        add_to_session(uid, {
            "role": "assistant",
            "content": {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": "draw_svg",
                "content": svg_name,
            },
        })

    elif name == "play_game":
        gname = (args.get("game") or "").lower()
        move = (args.get("move") or "").strip()
        if gname not in games.GAME_NAMES:
            return
        raw = rds.get(f"game:{uid}")
        st = json.loads(raw) if raw else None
        is_start = move.lower() in ("start", "start the game", "play", "")
        started = False
        if st is None or st.get("over"):
            if is_start:
                st = games.new_state(gname)
                started = st is not None
            else:
                # no live game (finished or never started) and this is a move,
                # not a start: don't silently deal a new game - tell the model
                add_to_session(uid, {
                    "role": "assistant",
                    "content": {
                        "tool_call_id": tc["id"],
                        "role": "tool",
                        "name": "play_game",
                        "content": "no game in progress - call play_game with move='start' to begin",
                        "state": None,
                        "started": False,
                    },
                })
                return
        elif is_start:
            # an explicit start while a game is live deals the new game
            # (this is what the user's /<game> commands do)
            st = games.new_state(gname)
            started = True
        if move.lower() in ("stop", "quit", "stop the game"):
            games.stop_game(st)
            note = "game ended"
            st["log"].append({"who": "model", "text": move, "note": note})
        elif started and is_start:
            note = gname + " started"
        else:
            note, invalid = games.apply_move(st, move, "model")
            st["log"].append({"who": "model", "text": move, "note": note})
        if st.get("over"):
            rds.delete(f"game:{uid}")
        else:
            rds.set(f"game:{uid}", json.dumps(st))
        # full game block (board + move log + the explicit "call the
        # play_game tool" instruction). board_text alone said "YOUR turn"
        # but never told the model to call the tool, so after a start/move
        # the model stalled on "my turn" instead of playing.
        content = games.game_block(st)
        add_to_session(uid, {
            "role": "assistant",
            "content": {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": "play_game",
                "content": content,
                "state": st,
                "started": started,
            },
        })

    elif name == "get_history":
        # Read the exact text of recent messages (newest first) so the
        # model can quote its own words exactly for edit_history.
        try:
            limit = max(1, min(int(args.get("limit", 5) or 5), 20))
        except Exception:
            limit = 5
        key = f"sess:{uid}"
        raw = rds.lrange(key, 0, limit - 1)
        lines = []
        for item in raw:
            msg = json.loads(html.unescape(item.decode()))
            role = msg.get("role", "?")
            content = msg.get("content")
            if isinstance(content, dict):
                tname = content.get("name")
                if tname == "play_game":
                    content = "(game move)"
                elif tname == "generate_image":
                    content = "(image)"
                else:
                    content = str(content.get("content") or "")
            lines.append(f"[{role}] {content}")
        add_to_session(uid, {
            "role": "assistant",
            "content": {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": "get_history",
                "content": "\n".join(lines) or "(empty conversation)",
            },
        })

    elif name == "edit_history":
        try:
            search_text = args.get("search_text", "")
            replacement_text = args.get("replacement_text", "")
            if not search_text:
                print("edit_history: no search_text given, nothing to edit")
                return "no_search"

            key = f"sess:{uid}"
            raw = rds.lrange(key, 0, -1)

            # Search ALL assistant replies, newest first: the first one
            # that contains the text to find is the target. (Limiting this
            # to the newest reply made /edit useless for anything older
            # than the last message.) The client mirrors this: it walks
            # backwards through the bubbles until it finds the matching
            # text, so the animation always plays on the message that was
            # actually edited.
            has_assistant = False
            target = None
            for i, item in enumerate(raw):
                msg = json.loads(html.unescape(item.decode()))
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                    has_assistant = True
                    if search_text in msg["content"]:
                        target = (i, msg)
                        break

            if target is None:
                if not has_assistant:
                    rds.publish(key, json.dumps({"type": "edit_error", "error": "there's no reply to edit yet"}))
                else:
                    rds.publish(key, json.dumps({"type": "edit_error", "error": f"'{search_text}' isn't in any of my replies"}))
                return "not_found"

            i, msg = target
            old_content = msg["content"]
            new_content = old_content.replace(search_text, replacement_text)
            msg["content"] = new_content
            rds.lset(key, i, json.dumps(msg))
            event = json.dumps({"type": "edit_history", "old_text": old_content, "new_text": new_content})
            rds.publish(key, event)
            print(f"edit_history: rewrote {search_text!r} -> {replacement_text!r}")
            return "ok"
        except Exception as e:
            print(f"Error processing edit_history tool: {e}")
            return "error"

    elif name == "web_search":
        query = (args.get("query") or "").strip()
        if not query:
            return
        # guard: the same search already ran in this turn (a looping model
        # would otherwise re-search up to 4 times) - tell it to answer.
        try:
            for item in rds.lrange(f"sess:{uid}", 0, 5):
                m = json.loads(html.unescape(item.decode()))
                c = m.get("content")
                if isinstance(c, dict) and c.get("name") == "web_search" and c.get("query") == query:
                    add_to_session(uid, {
                        "role": "assistant",
                        "content": {
                            "tool_call_id": tc["id"],
                            "role": "tool",
                            "name": "web_search",
                            "content": "already searched for that in this turn - answer from those results, do not search again",
                        },
                    })
                    return
        except Exception:
            pass
        try:
            max_results = int(args.get("max_results", 8) or 8)
        except Exception:
            max_results = 8
        result = await asyncio.to_thread(keenable_search, query, max_results)
        add_to_session(uid, {
            "role": "assistant",
            "content": {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": "web_search",
                "content": result,
                "query": query,
            },
        })

    elif name == "web_fetch":
        url = (args.get("url") or "").strip()
        if not url:
            return
        # guard: the same URL was already fetched in this turn
        try:
            for item in rds.lrange(f"sess:{uid}", 0, 5):
                m = json.loads(html.unescape(item.decode()))
                c = m.get("content")
                if isinstance(c, dict) and c.get("name") == "web_fetch" and c.get("url") == url:
                    add_to_session(uid, {
                        "role": "assistant",
                        "content": {
                            "tool_call_id": tc["id"],
                            "role": "tool",
                            "name": "web_fetch",
                            "content": "already fetched that page in this turn - answer from it, do not fetch again",
                        },
                    })
                    return
        except Exception:
            pass
        result = await asyncio.to_thread(web_fetch, url)
        add_to_session(uid, {
            "role": "assistant",
            "content": {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": "web_fetch",
                "content": result,
                "url": url,
            },
        })

    else:
        print(f"Unhandled tool call: {name}")


_LIGHTPANDA = os.path.expanduser("~/.local/bin/lightpanda")


def fetch_url_markdown(url, timeout_ms=12000):
    """Load a page with Lightpanda and return its text as Markdown."""
    try:
        proc = subprocess.run(
            [_LIGHTPANDA, "fetch", url, "--dump", "markdown", "--json",
             "--terminate-ms", str(timeout_ms)],
            capture_output=True, text=True, timeout=timeout_ms // 1000 + 15,
        )
        out = proc.stdout or ""
        start = out.find("{")
        if start == -1:
            return None
        obj, _ = json.JSONDecoder().raw_decode(out[start:])
        if obj.get("http_status", 200) >= 400:
            return None
        return (obj.get("content") or "").strip() or None
    except Exception as e:
        print(f"fetch_url_markdown error for {url}: {e}")
        return None


_URL_RE = re.compile(r"https?://[^\s<>'\"\)\]]+")


async def fetch_urls_in_text(text, max_urls=2, max_chars=8000):
    """Fetch every unique URL in a message (via Lightpanda) and return a
    combined Markdown digest, or "" if the message has no fetchable links."""
    if not text:
        return ""
    seen, urls = set(), []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            urls.append(u)
    urls = urls[:max_urls]
    if not urls:
        return ""
    results = await asyncio.gather(
        *[asyncio.to_thread(fetch_url_markdown, u) for u in urls]
    )
    parts = []
    for u, md in zip(urls, results):
        if md:
            parts.append(f"Website {u}:\n{md[:max_chars]}")
        else:
            parts.append(f"Website {u}: (could not be fetched)")
    return "\n\n---\n\n".join(parts)


# --- web search (Keenable keyless public endpoint) + web fetch ------------
# The model needs a way to get *current* information ("what's the latest
# news"). The automatic URL fetch only fires when the user pastes a link,
# so these two tools let the model search and then read a page in full.

WEB_SEARCH_RPM = 20
_search_hits = collections.deque()
_search_lock = threading.Lock()


def _web_search_throttle():
    """Sliding 60-second window, capped at WEB_SEARCH_RPM. True if the call
    may proceed, False if we are over the rate (stay a polite occasional
    user, not a scraper)."""
    now = time.time()
    with _search_lock:
        while _search_hits and now - _search_hits[0] > 60:
            _search_hits.popleft()
        if len(_search_hits) >= WEB_SEARCH_RPM:
            return False
        _search_hits.append(now)
        return True


def _parse_keenable(obj):
    """Normalize a Keenable response into [{title, url, snippet}]."""
    out = []
    for r in (obj.get("results") or []):
        url = (r.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "title": (r.get("title") or "").strip(),
            "url": url,
            "snippet": (r.get("snippet") or r.get("description") or "").strip(),
        })
    return out


def _format_search_results(results):
    if not results:
        return "No results."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n\n".join(lines) + "\n\n(call web_fetch with a URL above to read one in full)"


def keenable_search(query, max_results=8):
    """One JSON POST to Keenable's keyless public endpoint and a parse. The
    keyless endpoint wants a caller title, hence the X-Keenable-Title header.
    Honors a 429 Retry-After and retries once; a second 429 is surfaced."""
    if not _web_search_throttle():
        return "web search is rate-limited, try again in a moment"
    payload = {"query": query, "max_results": max(1, min(int(max_results or 8), 8)), "mode": "pro"}
    for attempt in range(2):
        try:
            resp = requests.post(
                "https://api.keenable.ai/v1/search/public",
                json=payload,
                headers={"X-Keenable-Title": "dyva"},
                timeout=30,
            )
        except Exception as e:
            return f"web search error: {e}"
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after or "").isdigit() else 2.0
            if attempt == 0:
                time.sleep(min(wait, 30))
                continue
            return "web search rate-limited (429)"
        if resp.status_code >= 400:
            return f"web search error: HTTP {resp.status_code}"
        try:
            obj = resp.json()
        except Exception as e:
            return f"web search error: bad JSON: {e}"
        return _format_search_results(_parse_keenable(obj))
    return "web search rate-limited (429)"


def web_backend():
    """Pick the best installed render engine: lightpanda, then chrome, then
    curl (the no-JS floor)."""
    lp = os.path.expanduser("~/.local/bin/lightpanda")
    if os.path.exists(lp):
        return "lightpanda", lp
    for chrome in ("/usr/bin/google-chrome", "/usr/bin/chromium",
                  "/usr/bin/chromium-browser", "/snap/bin/chromium"):
        if os.path.exists(chrome):
            return "chrome", chrome
    return "curl", "curl"


def _strip_html(html_text):
    text = re.sub(r"<script.*?</script>", "", html_text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _render_text(url, engine, binary):
    """Render a URL to text with the given engine. Returns text or None."""
    try:
        if engine == "lightpanda":
            proc = subprocess.run(
                [binary, "fetch", url, "--dump", "markdown", "--terminate-ms", "45000"],
                capture_output=True, text=True, timeout=60,
            )
            return (proc.stdout or "").strip() or None
        if engine == "chrome":
            proc = subprocess.run(
                [binary, "--headless", "--no-sandbox", "--disable-gpu",
                 "--dump-dom", "--virtual-time-budget=15000", url],
                capture_output=True, text=True, timeout=60,
            )
            return _strip_html(proc.stdout or "") or None
        # curl: the no-JS floor
        proc = subprocess.run(
            ["curl", "-sL", "--max-time", "45", "-A", "Mozilla/5.0", url],
            capture_output=True, text=True, timeout=60,
        )
        return _strip_html(proc.stdout or "") or None
    except Exception:
        return None


def _web_probe(url):
    """GET (following redirects) to get the final status, content-type, and
    the post-redirect final URL."""
    try:
        resp = requests.get(url, allow_redirects=True, timeout=20,
                           headers={"User-Agent": "Mozilla/5.0"})
        return resp.status_code, (resp.headers.get("Content-Type") or ""), resp.url
    except Exception:
        return None, "", url


def web_fetch(url, max_bytes=200_000):
    """Read one explicit URL. Images are saved to disk and returned as a file
    ref; everything else is rendered to text by the best engine, with a
    cross-fallback (a browser that chokes retries with curl, and vice versa).
    Text is capped at max_bytes with [truncated] appended."""
    status, ctype, final_url = _web_probe(url)
    if status is None:
        return f"could not reach {url}"
    if status >= 400:
        return f"fetch failed: HTTP {status} for {final_url}"
    ctype = ctype.split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        try:
            data = requests.get(final_url, timeout=30,
                               headers={"User-Agent": "Mozilla/5.0"}).content
        except Exception as e:
            return f"could not download image: {e}"
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "image/gif": ".gif"}.get(ctype, ".img")
        name = f"webimg_{uuid.uuid4().hex}{ext}"
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return f"image saved to {name} ({len(data)} bytes)"
    engine, binary = web_backend()
    text = _render_text(final_url, engine, binary)
    if not text and engine != "curl":
        text = _render_text(final_url, "curl", "curl")  # browser choked -> curl
    if not text and engine == "curl":
        alt, alt_bin = ("lightpanda", os.path.expanduser("~/.local/bin/lightpanda")) \
            if os.path.exists(os.path.expanduser("~/.local/bin/lightpanda")) else ("chrome", "/usr/bin/chromium")
        text = _render_text(final_url, alt, alt_bin)  # curl choked -> browser
    if not text:
        return f"could not render {final_url}"
    if len(text) > max_bytes:
        text = text[:max_bytes] + " [truncated]"
    return text


@app.post("/chat")
async def chat(data: dict):
    isFirst = not data.get("uid")
    uid = data.get("uid") or initialize_session(data["context"], _model)
    nextLine = None
    tool_cmd = None

    if data.get("text") or data.get("images"):
        text = data.get("text") or ""
        if text and text[0] == "/":
            parts = text[1:].split(" ")
            cmd = parts[0]
            if cmd == "delete":
                summarize(uid, "")
                rds.hdel(_topicList, uid)
                rds.hdel(_topicTs, uid)
                return JSONResponse({"res": True, "data": [], "uid": uid})
            elif cmd == "update":
                newname = " ".join(parts[1:]).strip()
                if newname:
                    summarize(uid, newname)
                else:
                    summarize(uid)
                return JSONResponse({"res": True, "data": [], "uid": uid})
            elif cmd == "edit":
                # /edit <find> => <replace>: a pure server-side command,
                # like /update and /delete. Runs the edit_history tool and
                # stops - the model never sees it (its acknowledgement used
                # to re-quote the edited text, which looked like a
                # duplicated message) and it is not saved to the
                # conversation.
                tc, bad_usage = _slash_tool_call(cmd, parts)
                if bad_usage or tc is None:
                    return JSONResponse({"res": True, "data": [], "uid": uid})
                await handle_tool_call(uid, tc)
                return JSONResponse({"res": True, "data": [], "uid": uid})
            else:
                # /search, /fetch, /img, /<game>: run one of the model's
                # tools directly (the popup mirrors _slash_tool_call). The
                # result is saved and the model answers from it. A valid
                # tool command falls through to the normal flow below.
                tool_cmd, bad_usage = _slash_tool_call(cmd, parts)
                if bad_usage:
                    return JSONResponse({"res": True, "data": [], "uid": uid})

        if data.get("edit"):
            key = f"sess:{uid}"
            raw = rds.lrange(key, 0, -1)
            original_text = data.get("original_text", "")
            for i, item in enumerate(raw):
                msg = json.loads(html.unescape(item.decode()))
                if not isinstance(msg, dict):
                    continue
                if msg["role"] == "user" and (not original_text or msg["content"] == original_text):
                    msg["content"] = data["text"]
                    rds.lset(key, i, json.dumps(msg))
                    # re-run from the edited message: keep it and everything
                    # older (indices i..end), drop the newer responses
                    # (0..i-1). (ltrim(key, 0, i) did the opposite - it kept
                    # the newer messages and deleted the older ones.)
                    rds.ltrim(key, i, -1)
                    break
            history = add_to_session(uid)
            fetched_pages = ""
        elif not data.get("regen"):
            row = {"role": "user", "content": data["text"]}
            if data.get("images"):
                row["images"] = data["images"]
            history = add_to_session(uid, row)
            touch(uid)
            # a /fetch command fetches its own URL - don't double-fetch it
            fetched_pages = "" if tool_cmd else await fetch_urls_in_text(text)
        else:
            # regen: truncate the session at the user message being
            # regenerated (the one the dice was pressed on), dropping
            # everything more recent (the old response and any later turns),
            # so the model re-answers that turn instead of seeing a
            # conversation that already ended. The list is most-recent-first,
            # so we find the anchor (the most recent user message matching the
            # sent text) and ltrim to keep only it and the older messages.
            key = f"sess:{uid}"
            raw = rds.lrange(key, 0, -1)
            anchor_idx = None
            for i, item in enumerate(raw):
                msg = json.loads(html.unescape(item.decode()))
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "user"
                    and msg.get("content") == data.get("text")
                ):
                    anchor_idx = i
                    break
            if anchor_idx is not None and anchor_idx > 0:
                # keep only the anchor and older messages (index anchor_idx..end)
                rds.ltrim(key, anchor_idx, -1)
            history = add_to_session(uid)
            fetched_pages = ""
        if isFirst:
            # The chat must appear in the sidebar immediately, not after a
            # whole LLM round trip. Set a provisional title (a short version
            # of the first message) right now, then refine it with a deadpan
            # LLM title in the background. Firing here (not at the end of
            # generate()) means it doesn't depend on the stream being fully
            # consumed, and summarize() keeps the provisional title if the
            # slow LLM call fails.
            prov = re.sub(r"\s+", " ", text or "(an image was shared)").strip()[:40]
            rds.hset(_topicList, uid, prov)
            rds.publish(_topicList, json.dumps({uid: prov}))
            run_in_background(summarize, uid)

        openrouter_model = _model

        if tool_cmd:
            # The user's /command ran a model tool directly. Run it before
            # building the model context so the result composes with the
            # fetched pages and the game block exactly as a model-made call
            # would, and the model reads it on its first pass.
            tool_status = await handle_tool_call(uid, tool_cmd)
            history = add_to_session(uid)

        # If the user linked any websites, hand the fetched pages to the model
        # as part of their message so it can answer from the actual content.
        model_history = filter_tools(history)
        if fetched_pages and model_history and model_history[-1].get("role") == "user":
            model_history[-1]["content"] = (
                (model_history[-1].get("content") or "")
                + "\n\n[Websites you linked, fetched for you:]\n" + fetched_pages
            )
        if tool_cmd and model_history and model_history[-1].get("role") == "user":
            # a model that sees a bare /command tends to re-run the tool
            # instead of answering from its result - tell it it already ran.
            tname = tool_cmd["function"]["name"]
            if tname == "play_game":
                raw = rds.get(f"game:{uid}")
                who = json.loads(raw).get("turn") if raw else "user"
                if who == "model":
                    nudge = " It is your turn now - describe the board, then make your move with the tool."
                else:
                    nudge = " It is the player's turn now - describe the board and what to do. Do not call the tool again until the player moves."
            elif tname == "generate_image":
                nudge = " The image is ready - comment on it for the player."
            else:
                nudge = " Answer the player from that result now - do not call the tool again."
            model_history[-1]["content"] = (
                (model_history[-1].get("content") or "")
                + "\n\n[You ran a /command; the tool already ran and its result is in the tool message above."
                + nudge + "]"
            )

        # game: with a game in play, what the user types is their move when
        # it's their turn (plain text, no special mode). The live board is
        # injected as a [GAME ...] block - the single place the model reads
        # game state (history rows are neutral markers, so the model can't
        # imitate board text as plain speech).
        raw = rds.get(f"game:{uid}")
        if raw:
            st = json.loads(raw)
            is_plain = (
                not data.get("edit") and not data.get("regen")
                and text and not text.startswith("/")
            )
            published = False
            if is_plain and text.lower().strip() in ("stop", "quit", "stop the game", "stop game"):
                games.stop_game(st)
                st["log"].append({"who": "user", "text": text, "note": "abandoned"})
                published = True
            elif not st.get("over"):
                a = games.actor(st)
                if a == "user" and games.is_move(st, text, "user"):
                    note, _invalid = games.apply_move(st, text, "user")
                    st["log"].append({"who": "user", "text": text, "note": note})
                    published = True
            if st.get("over"):
                rds.delete(f"game:{uid}")
            else:
                rds.set(f"game:{uid}", json.dumps(st))
            if published:
                add_to_session(uid, {
                    "role": "assistant",
                    "content": {
                        "tool_call_id": "usermove-" + str(int(time.time() * 1000)),
                        "role": "tool",
                        "name": "play_game",
                        "content": games.board_text(st),
                        "state": st,
                        "started": False,
                    },
                })
            if model_history:
                # the game block is the single place the model reads the
                # board + whose turn it is + what move to make. Append it
                # as its own message: the last message is often a usermove
                # line (not a user message), so injecting into the user
                # message would skip it and the model would stall.
                model_history.append({"role": "user", "content": games.game_block(st)})

        async def generate():
            # Bounded tool loop: the model may call a tool (web_search,
            # web_fetch, play_game, ...); we run it, feed the result back,
            # and let the model answer from it. Capped at 4 passes so a
            # model that keeps calling tools can't loop forever.
            history = model_history
            ran_tools = set()  # (name, args) already run this turn
            if tool_cmd:
                try:
                    _a = json.loads(tool_cmd["function"]["arguments"] or "{}")
                except Exception:
                    _a = {}
                ran_tools.add((tool_cmd["function"]["name"], json.dumps(_a, sort_keys=True)))
            for _pass in range(4):
                ttlResponse = ''
                tool_calls = []
                emitted = False
                for attempt in range(1, INFER_MAX_ATTEMPTS + 1):
                    ttlResponse = ''
                    tool_calls = []
                    emitted = False
                    try:
                        async for chunk in openrouter_stream(openrouter_model, history, _tools, "auto"):
                            choices = chunk.get("choices", [])
                            content = ""
                            if choices:
                                delta = choices[0].get("delta", {})
                                if "tool_calls" in delta:
                                    for tc in delta["tool_calls"]:
                                        idx = tc["index"]
                                        while len(tool_calls) <= idx:
                                            tool_calls.append({"id": "", "function": {"name": "", "arguments": ""}})
                                        if "id" in tc and tc["id"]:
                                            tool_calls[idx]["id"] = tc["id"]
                                        if "function" in tc:
                                            fn = tc["function"]
                                            if "name" in fn and fn["name"]:
                                                tool_calls[idx]["function"]["name"] = fn["name"]
                                            if "arguments" in fn:
                                                tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                                content = delta.get("content", "") or ""
                                # reasoning_content (thinking tokens) is also
                                # shown to the client, so it counts as "emitted"
                                # too - a retry after thinking started would
                                # duplicate it.
                                reasoning = delta.get("reasoning_content", "") or delta.get("reasoning", "") or ""
                                if content:
                                    ttlResponse += content
                                if content or reasoning:
                                    emitted = True

                            chunk['uid'] = uid
                            chunk['delta'] = content
                            yield f"data:{json.dumps(chunk)}\n"
                        break  # completed this attempt
                    except Exception as e:
                        if emitted:
                            # Already streamed content to the client - a retry
                            # would duplicate it. Finalize with the partial
                            # content and drop any partial tool calls (their
                            # arguments would be incomplete).
                            print(f"stream failed after partial output (attempt {attempt}): {type(e).__name__}: {e}")
                            tool_calls = []
                            break
                        if attempt < INFER_MAX_ATTEMPTS:
                            print(f"stream attempt {attempt} failed ({type(e).__name__}: {e}), retrying in {INFER_RETRY_DELAY}s...")
                            await asyncio.sleep(INFER_RETRY_DELAY)
                            continue
                        # Every attempt timed out / failed before any output.
                        # Tell the user instead of hanging or returning a bare 500.
                        print(f"inference failed after {INFER_MAX_ATTEMPTS} attempts: {type(e).__name__}: {e}")
                        err = "⏳ the model timed out a few times in a row - try again"
                        yield f"data:{json.dumps({'delta': err, 'uid': uid})}\n"
                        ttlResponse = err
                        break

                if not tool_calls:
                    # Completed text, partial output, or the timeout message -
                    # finalize this turn.
                    raw = rds.get(f"game:{uid}")
                    st = json.loads(raw) if raw else None
                    recovered = False
                    # 1. the model may emit tool calls as text (model_call:...,
                    #    name{...}, name(...), or a bare tool name) instead of
                    #    structured tool_calls - execute the ones we can and
                    #    suppress the raw text.
                    if ttlResponse:
                        text_calls, cleaned = _extract_text_tool_calls(ttlResponse)
                    else:
                        text_calls, cleaned = [], ""
                    executable = []
                    for name, args in text_calls:
                        # never run edit_history with missing args (an empty
                        # search_text would match and wipe the newest reply)
                        if name == "edit_history" and not (args.get("search_text") and args.get("replacement_text")):
                            continue
                        if name == "get_history" and not args:
                            args = {"limit": "5"}
                        executable.append((name, args))
                    if executable:
                        for name, args in executable:
                            tc = {
                                "id": "textcall-" + str(int(time.time() * 1000)),
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                            await handle_tool_call(uid, tc)
                        yield f"data:{json.dumps({'type': 'clear_pending', 'uid': uid})}\n"
                        recovered = True
                    elif cleaned and cleaned != ttlResponse:
                        # only non-executable tokens were found - save the
                        # cleaned text (tool-call tokens stripped)
                        add_to_session(uid, {"role": "assistant", "content": cleaned})
                        recovered = True
                    # 2. game: the model's turn - recover the move from its text
                    #    (or auto-pick one) so the game never stalls, even if
                    #    the model emits no text at all.
                    if not recovered and st and not st.get("over") and games.actor(st) == "model":
                        move = (games.parse_model_move(st, ttlResponse) if ttlResponse else None) or games.auto_move(st)
                        if move:
                            note, _invalid = games.apply_move(st, move, "model")
                            st["log"].append({"who": "model", "text": ttlResponse or "", "note": note})
                            if st.get("over"):
                                rds.delete(f"game:{uid}")
                            else:
                                rds.set(f"game:{uid}", json.dumps(st))
                            yield f"data:{json.dumps({'type': 'clear_pending', 'uid': uid})}\n"
                            add_to_session(uid, {
                                "role": "assistant",
                                "content": {
                                    "tool_call_id": "textmove-" + str(int(time.time() * 1000)),
                                    "role": "tool",
                                    "name": "play_game",
                                    "content": games.game_block(st),
                                    "state": st,
                                    "started": False,
                                },
                            })
                            recovered = True
                    # 3. save the text (if nothing was recovered).
                    if not recovered and ttlResponse:
                        add_to_session(uid, {"role": "assistant", "content": ttlResponse})
                        # rate the reply in the background (System One noul
                        # metrics) - the bars appear under the message when
                        # done. Off the loop: it's a blocking model call.
                        run_in_background(rate_and_attach, uid, ttlResponse)
                    break

                # Completed with tool calls - run them, then let the model
                # read the results and answer on the next pass.
                #
                # IMPORTANT: do NOT save this pass's text, and do NOT feed it
                # back. A chatty model repeats the same line every pass; saving
                # it as a separate assistant message (and feeding it back) makes
                # the model imitate its own prior text, so the user sees the
                # same line repeated N times. The text is still streamed to the
                # client live, but only the FINAL pass's text (no tool calls)
                # is persisted. The tool results ARE saved (via handle_tool_call)
                # so the model can read them.
                #
                # The intermediate text WAS already streamed to the client (it
                # can't be unstreamed), and the model often repeats the same
                # line on the final pass - so the user would see it twice. Tell
                # the client to clear the pending text now; the final pass's
                # text is the only thing that stays.
                # Duplicate check: is this pass re-calling tools that were
                # already run this turn (a 9B model loops on the same search
                # instead of answering from its results)?
                sigs = []
                for tc in tool_calls:
                    try:
                        _a = json.loads(tc["function"]["arguments"] or "{}")
                    except Exception:
                        _a = {}
                    sigs.append((tc["function"]["name"], json.dumps(_a, sort_keys=True)))
                if ran_tools and all(s in ran_tools for s in sigs):
                    # The model is looping on tools it already ran: force a
                    # text-only final pass (no tools offered, so it must
                    # answer from what it has).
                    yield f"data:{json.dumps({'type': 'clear_pending', 'uid': uid})}\n"
                    final_text = ''
                    try:
                        async for chunk in openrouter_stream(openrouter_model, history, None, "auto"):
                            choices = chunk.get("choices", [])
                            content = ""
                            if choices:
                                content = choices[0].get("delta", {}).get("content", "") or ""
                            final_text += content
                            chunk['uid'] = uid
                            chunk['delta'] = content
                            yield f"data:{json.dumps(chunk)}\n"
                    except Exception as e:
                        print(f"forced final pass failed: {type(e).__name__}: {e}")
                    if final_text:
                        add_to_session(uid, {"role": "assistant", "content": final_text})
                        run_in_background(rate_and_attach, uid, final_text)
                    break

                yield f"data:{json.dumps({'type': 'clear_pending', 'uid': uid})}\n"
                for tc in tool_calls:
                    await handle_tool_call(uid, tc)
                for tc in tool_calls:
                    try:
                        _a = json.loads(tc["function"]["arguments"] or "{}")
                    except Exception:
                        _a = {}
                    ran_tools.add((tc["function"]["name"], json.dumps(_a, sort_keys=True)))
                history = filter_tools(add_to_session(uid))

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

@app.post("/speak")
async def speak(data: dict):
    import os
    path = "fe/voice.mp3"
    if os.path.exists(path):
        return Response(content=open(path, "rb").read(), media_type="audio/mpeg")
    return Response(content=b"", media_type="audio/mpeg")

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
        # Last-activity timestamp per chat, used to order and bucket the sidebar.
        ts = rds.hgetall(_topicTs)
        ts = {k.decode(): int(v) for k, v in ts.items()}
        for uid in chats:
            chats[uid] = {"title": chats[uid], "ts": ts.get(uid)}
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
        await pubsub.aclose()


@app.websocket("/ws/channel/{id}")
async def websocket_endpoint(websocket: WebSocket, id: str):
    await websocket.accept()
    _track_ws()
    await stream_channel(websocket, f"sess:{id}")


@app.websocket("/ws/topics")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _track_ws()
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

class NoCacheStatic(StaticFiles):
    """Serve markup/JS/CSS with must-revalidate semantics.

    Starlette sends no Cache-Control, so browsers apply *heuristic* freshness
    from Last-Modified: a file untouched for months is assumed fresh for days
    and is never revalidated. That silently pins clients to a stale frontend
    after a deploy. Images keep the default (they're content-addressed by uuid).
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path in ("", ".") or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStatic(directory="fe", html=True), name="static")

# Startup banner. The art lives in logo.txt, which is NOT committed to git,
# so its contents are copied in here (kept in sync manually). Printed once
# when the app starts.
LOGO = r"""▂ 𜺠𜺣▂𜺣          ▂𜺣   ▂▂ ▂▂▂ ▂▂▂▂
█ ▐▌▐𜷀𜶜𜺣𜴘𜴧𜶻 𜷋𜴧𜶜𜺣▐▌𜷋𜴉𜷥𜺨𜺫▘▐𜷀𜷕𜴍𜺨▐▌𜺫
█𜴖𜶪▌𜷕▌▐▌𜶪𜶮𜶫𜺣𜶫▂𜷋𜴉𜷕𜵊𜶚▖𜴦𜶽𜵜▌𜷕𜷀   𜷕𜷀"""

if __name__ == "__main__":
    import uvicorn

    print(LOGO)
    print()

    # Auto-reload: when app.py (or a local import) changes, uvicorn restarts
    # the server automatically - no manual restart needed, and the editor
    # (or an agent) never has to kill the running process. uvicorn's own
    # signal handlers manage the clean exit (stop the reloader + the server
    # subprocess).
    #
    # timeout_graceful_shutdown: the websocket handlers block forever on
    # pubsub.listen(), so they never complete on their own. Without a finite
    # timeout, Ctrl+C hangs on "Waiting for background tasks to complete"
    # until a second Ctrl+C force-quits. A finite grace period lets in-flight
    # requests finish, then uvicorn cancels the stuck handler tasks (their
    # finally blocks unsubscribe + close the pubsub), so a single Ctrl+C
    # exits cleanly.
    # reload (and workers) require the app as an import string, not the app
    # object: the reloader spawns a subprocess that re-imports the app by
    # name, so it can't be handed across the process boundary as an object.
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_graceful_shutdown=5,
    )
