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
_model = "blit"

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

DYVA_API_BASE = "http://127.0.0.1:11434/v1"

async def openrouter_stream(model: str, messages: list, tools=None, tool_choice=None):
    """Call Dyva with streaming and yield chunks."""
    headers = { "Content-Type": "application/json", }

    payload = {
        "think": False,
        "model": model,
        "messages": messages,
        "stream": True
    }

    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    async with httpx.AsyncClient(timeout=60.0) as client:
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
}, {
    "type": "function",
    "function": {
        "name": "edit_history",
        "description": "Edit the assistant's most recent response in the conversation history. Replace search_text with replacement_text in that message.",
        "parameters": {
            "type": "object",
            "properties": {
                "search_text": {
                    "type": "string",
                    "description": "The exact text to search for in the last assistant response",
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
        response = httpx.post(f"{DYVA_API_BASE}/chat/completions", json=payload, headers=headers, timeout=30.0)
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


def touch(uid):
    """Record when a chat was last active so the sidebar can order and bucket by date."""
    rds.hset(_topicTs, uid, int(time.time()))


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
        # board + the move note: the note is the model's feedback on its own
        # move (including "that was not a valid move" retries)
        content = games.board_text(st)
        if note:
            content += "\n(move: " + note + ")"
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

    elif name == "edit_history":
        try:
            search_text = args.get("search_text", "")
            replacement_text = args.get("replacement_text", "")
            if not search_text:
                print("edit_history: no search_text given, nothing to edit")
                return

            key = f"sess:{uid}"
            raw = rds.lrange(key, 0, -1)

            # add_to_session lpushes, so index 0 is the newest message. The
            # tool edits the most recent reply and only that one: searching
            # further back used to silently rewrite an older message while
            # the browser animated the newest bubble, so the UI showed an
            # edit that had happened somewhere else. Image results are dicts,
            # so skip past them to the last thing said in words.
            target = None
            for i, item in enumerate(raw):
                msg = json.loads(html.unescape(item.decode()))
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                    target = (i, msg)
                    break

            if target is None:
                print("edit_history: no assistant reply in this session to edit")
                return

            i, msg = target
            old_content = msg["content"]
            if search_text not in old_content:
                # The model has to reproduce its own words exactly; when it
                # misquotes by a character this is the only trace.
                print(f"edit_history: {search_text!r} is not in the last reply, no edit made")
                return

            new_content = old_content.replace(search_text, replacement_text)
            msg["content"] = new_content
            rds.lset(key, i, json.dumps(msg))
            event = json.dumps({"type": "edit_history", "old_text": old_content, "new_text": new_content})
            rds.publish(key, event)
            print(f"edit_history: rewrote {search_text!r} -> {replacement_text!r}")
        except Exception as e:
            print(f"Error processing edit_history tool: {e}")

    elif name == "web_search":
        query = (args.get("query") or "").strip()
        if not query:
            return
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
            },
        })

    elif name == "web_fetch":
        url = (args.get("url") or "").strip()
        if not url:
            return
        result = await asyncio.to_thread(web_fetch, url)
        add_to_session(uid, {
            "role": "assistant",
            "content": {
                "tool_call_id": tc["id"],
                "role": "tool",
                "name": "web_fetch",
                "content": result,
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

    if data.get("text") or data.get("images"):
        text = data.get("text") or ""
        if text and text[0] == "/":
            parts = text[1:].split(" ")
            cmd = parts[0]
            if cmd == "delete":
                summarize(uid, "")
                rds.hdel(_topicList, uid)
                rds.hdel(_topicTs, uid)
            elif cmd == "update":
                newname = " ".join(parts[1:]).strip()
                if newname:
                    summarize(uid, newname)
                else:
                    summarize(uid)

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
                    rds.ltrim(key, 0, i)
                    break
            history = add_to_session(uid)
            fetched_pages = ""
        elif not data.get("regen"):
            row = {"role": "user", "content": data["text"]}
            if data.get("images"):
                row["images"] = data["images"]
            history = add_to_session(uid, row)
            touch(uid)
            fetched_pages = await fetch_urls_in_text(text)
        else:
            # regen: the history currently ends with the assistant's response.
            # Drop that response so the model re-answers the user's turn
            # instead of seeing a conversation that already ended.
            key = f"sess:{uid}"
            raw = rds.lrange(key, 0, -1)
            for item in raw:
                msg = json.loads(html.unescape(item.decode()))
                if (
                    isinstance(msg, dict)
                    and msg.get("role") == "assistant"
                    and isinstance(msg.get("content"), str)
                ):
                    rds.lrem(key, 1, item)
                    break
            history = add_to_session(uid)
            fetched_pages = ""
        if isFirst:
            # Titling costs a whole LLM round trip. Do it off the request path
            # so the first token reaches the browser immediately; the finished
            # title is pushed out over the topics websocket by summarize().
            title_text = text or "(an image was shared)"
            run_in_background(lambda t: summarize(uid, generate_summary(t)), title_text)

        openrouter_model = _model

        # If the user linked any websites, hand the fetched pages to the model
        # as part of their message so it can answer from the actual content.
        model_history = filter_tools(history)
        if fetched_pages and model_history and model_history[-1].get("role") == "user":
            model_history[-1]["content"] = (
                (model_history[-1].get("content") or "")
                + "\n\n[Websites you linked, fetched for you:]\n" + fetched_pages
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
            if model_history and model_history[-1].get("role") == "user":
                model_history[-1]["content"] = (
                    (model_history[-1].get("content") or "") + "\n\n" + games.game_block(st)
                )

        async def generate():
            # Bounded tool loop: the model may call a tool (web_search,
            # web_fetch, play_game, ...); we run it, feed the result back,
            # and let the model answer from it. Capped at 4 passes so a
            # model that keeps calling tools can't loop forever.
            history = model_history
            for _pass in range(4):
                ttlResponse = ''
                tool_calls = []
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
                        if content:
                            ttlResponse += content

                    chunk['uid'] = uid
                    chunk['delta'] = content
                    yield f"data:{json.dumps(chunk)}\n"

                if not tool_calls:
                    if ttlResponse:
                        add_to_session(uid, {"role": "assistant", "content": ttlResponse})
                    break

                for tc in tool_calls:
                    await handle_tool_call(uid, tc)
                if ttlResponse:
                    add_to_session(uid, {"role": "assistant", "content": ttlResponse})
                # reload the history (now including the tool result) so the
                # next pass lets the model read it and produce an answer
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

if __name__ == "__main__":
    import uvicorn
    import signal
    import threading

    # Run the server in a daemon (non-main) thread: uvicorn's capture_signals()
    # only installs its own SIGINT/SIGTERM handlers in the main thread, so our
    # main-thread handlers below stay in charge of the clean exit.
    config = uvicorn.Config(app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)

    def _run_server():
        server.run()

    _server_thread = threading.Thread(target=_run_server, daemon=True)
    _server_thread.start()

    _shutdown_event = threading.Event()

    def _handle_shutdown(sig, frame):
        print("\nShutting down (clean exit)...")
        # Cancel in-flight background work (summarize, etc.) and live
        # websocket handlers so the shutdown doesn't hang on a stuck network
        # call or a pubsub.listen() that blocks forever.
        for t in list(_bg_tasks):
            t.cancel()
        for t in list(_ws_tasks):
            t.cancel()
        server.should_exit = True
        _server_thread.join(timeout=8)
        if _server_thread.is_alive():
            print("Drain timed out - forcing exit.")
            os._exit(0)
        _shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # Block the main thread until the shutdown completes; then the main
    # thread ends and the process exits (the server thread is a daemon that
    # has already finished its clean drain).
    _shutdown_event.wait()
