#!/usr/bin/env python3
import uuid, os, redis, json, html
from litellm import completion
from flask import Flask, request, jsonify, send_file

_app = Flask(__name__)
_rc  = redis.Redis(host='localhost', port=6379, db=0)

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

def prompt(history, model):
    openrouter_model ="openrouter/google/gemini-2.0-flash-exp:free"
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")

    # So this is apparently stateless.
    # We just unroll the entire conversation up to this point.
    message = completion(
        api_key=openrouter_api_key,
        model=openrouter_model,
        messages=prepare_message(history),
    )
    return message.choices[0].message.content

# row should be typed liked 
# {role: assistant/user, content: text}
def add_to_session(sess, row = None):
    key = f'sess:{sess}'
    if row:
        _rc.lpush(key, json.dumps(row))

    return list(map(lambda x: json.loads(html.unescape(x.decode())), _rc.lrange(key, 0, -1)))[::-1]

# So the *easiest* way to store meta information (#20) is to
# store it as our initial setting and then rely on the 
# existing prepare_message to filter it accordingly. Then
# we don't need to maintain separate keys or do any extra
# cleanup AND we can simply return it with everything else.
def initialize_session(context, model):
    sess = uuid.uuid4().hex
    initialList = [
        {'role': 'session', 'content': {'model': model} },
        {'role': 'user', 'content': context},
        {'role': 'assistant', 'content': "Welcone to RecklessAI, how may I help you?"}
    ]
    [ _rc.lpush(f'sess:{sess}', json.dumps(row)) for row in initialList ]
    return sess

@_app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()

    # If we don't have a UID, then we initialize and have the first injected assistant.
    uid = data['uid'] or initialize_session(data['context'], data['model'])
    nextLine = None

    if data['text']:
        # We should also have the first user-generated text at this point
        history = add_to_session(uid, {'role': 'user', 'content': data['text']})
        response = prompt(history, data['model'])

        # We need to save that response as an assistant
        nextLine = {'role':'assistant', 'content': response}

    # We support a blank add_to_session which just uses the mechanics
    # to retrieve. This is what's done on page-load for a session reload
    return jsonify({'res': True, 'data': add_to_session(uid, nextLine), 'uid': uid})

@_app.route('/')
def index():
    return send_file('index.html')

if __name__ == '__main__':
    _app.run(debug=False,host="0.0.0.0")
