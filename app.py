import uuid, os, redis, json, html
import asyncio
from redis.asyncio import Redis as ioredis
from litellm import completion
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
ws_redis = ioredis.from_url("redis://localhost")
redis_client  = redis.Redis(host='localhost', port=6379, db=0)

_topicList = "convos"
_model = 'openrouter/google/gemini-2.0-flash-exp:free'

def prepare_message(message_list):
    # So you need alternating
    # user / assistant message
    mt = ['user', 'assistant']
    ix = 0
    fList = []

    for row in message_list:
      if row['role'] == mt[ix]:
        fList.append(row)
        ix = not ix
    return fList


# row should be typed liked 
# {role: assistant/user, content: text}
def add_to_session(sess, row = None):
    key = f'sess:{sess}'
    if row:
        redis_client.lpush(key, json.dumps(row))
        redis_client.publish(key, json.dumps(row))

    return list(map(lambda x: json.loads(html.unescape(x.decode())), redis_client.lrange(key, 0, -1)))[::-1]

# So the *easiest* way to store meta information (#20) is to
# store it as our initial setting and then rely on the 
# existing prepare_message to filter it accordingly. Then
def generate_summary(text, max_length=30):
    """Generate summary using litellm"""
    prompt = f"Summarize the following text in less than {max_length} characters: {text}"
    messages = [{"role": "user", "content": prompt}]
    try:
        response = completion(model=_model, messages=messages)
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error generating summary: {e}")
        return "Summary unavailable"

# we don't need to maintain separate keys or do any extra
# cleanup AND we can simply return it with everything else.
def initialize_session(context, model):
    sess = uuid.uuid4().hex
    redis_client.lpush(f'sess:{sess}', json.dumps({'role': 'system', 'content': context }))
    return sess

@app.post('/chat')
async def chat(data: dict):
    # If we don't have a UID, then we initialize and have the first injected assistant.
    isFirst = not data.get('uid')
    uid = data.get('uid') or initialize_session(data['context'], _model)
    nextLine = None

    if data.get('text'):
        # We should also have the first user-generated text at this point
        history = add_to_session(uid, {'role': 'user', 'content': data['text']})
        if isFirst:
            summary = generate_summary(data['text'])
            redis_client.hset(_topicList, uid, summary)
            
        openrouter_model = _model
        openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")

        # So this is apparently stateless.
        # We just unroll the entire conversation up to this point.
        from pprint import pprint
        pprint(history)
        message = completion(
            api_key=openrouter_api_key,
            model=openrouter_model,
            messages=history, #prepare_message(history),
        )
        response = message.choices[0].message.content

        # We need to save that response as an assistant
        nextLine = {'role':'assistant', 'content': response}
    
    # We support a blank add_to_session which just uses the mechanics
    # to retrieve. This is what's done on page-load for a session reload
    return JSONResponse({'res': True, 'data': add_to_session(uid, nextLine), 'uid': uid})

@app.get("/history/{id}")
async def get_history(id: str):
    """Returns the chat history for a given session ID."""
    key = f'sess:{id}'
    history = list(map(lambda x: json.loads(html.unescape(x.decode())), 
                       redis_client.lrange(key, 0, -1)))[::-1]
    return JSONResponse({'res': True, 'data': history, 'uid': id})

@app.get("/topicList")
async def topicList():
    try:
        # Assuming 'chats' is a hash
        chats = redis_client.hgetall(_topicList)
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
            if message['type'] == 'message':
                data = message['data'].decode('utf-8')
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

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await stream_channel(websocket, _topicList)
   
app.mount("/", StaticFiles(directory="fe", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
