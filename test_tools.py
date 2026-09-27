"""Covers the chat-path tool dispatch: image generation and edit_history.

Run from the repo root (paths in generate_image are relative to it):
    python3 test_image_tool.py
"""
import asyncio, json, os, sys

import app


def test_generate_image_extension_matches_bytes():
    """A JPEG from upstream must not be saved under a .png name."""
    name = app.generate_image("a plain red square")
    assert "." in name, f"expected an extension in {name!r}"
    path = os.path.join("fe/images", name)
    assert os.path.exists(path), f"{path} was not written"
    head = open(path, "rb").read(12)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        expected = ".png"
    elif head[:3] == b"\xff\xd8\xff":
        expected = ".jpg"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        expected = ".webp"
    else:
        raise AssertionError(f"unrecognized image bytes: {head!r}")
    assert name.endswith(expected), f"{name!r} holds {expected} bytes"
    return name


def test_generate_image_tool_call_is_dispatched():
    """The chat tool loop must actually run generate_image and store the result."""
    uid = "test-image-tool"
    key = f"sess:{uid}"
    app.rds.delete(key)
    tc = {
        "id": "call_test_1",
        "function": {"name": "generate_image",
                     "arguments": json.dumps({"prompt": "a plain blue circle"})},
    }
    asyncio.run(app.handle_tool_call(uid, tc))

    rows = [json.loads(r.decode()) for r in app.rds.lrange(key, 0, -1)]
    app.rds.delete(key)
    img = [r for r in rows
           if isinstance(r.get("content"), dict) and r["content"].get("tool_call_id")]
    assert img, f"no image row stored in the session; got {rows!r}"
    content = img[0]["content"]
    assert content["name"] == "generate_image"
    assert content["tool_call_id"] == "call_test_1"
    path = os.path.join("fe/images", content["content"])
    assert os.path.exists(path), f"session points at missing file {path}"
    return content["content"]


def test_edit_history_rewrites_and_publishes():
    """edit_history must rewrite the stored message and announce it on the channel."""
    uid = "test-edit-history"
    key = f"sess:{uid}"
    app.rds.delete(key)
    app.add_to_session(uid, {"role": "user", "content": "how do I cook rice"})
    app.add_to_session(uid, {"role": "assistant", "content": "Boil it in motor oil."})

    sub = app.rds.pubsub()
    sub.subscribe(key)
    sub.get_message(timeout=1)  # drain the subscribe confirmation

    tc = {
        "id": "call_test_2",
        "function": {"name": "edit_history",
                     "arguments": json.dumps({"search_text": "motor oil",
                                              "replacement_text": "diesel"})},
    }
    asyncio.run(app.handle_tool_call(uid, tc))

    rows = [json.loads(r.decode()) for r in app.rds.lrange(key, 0, -1)]
    assert any(r.get("content") == "Boil it in diesel." for r in rows), \
        f"stored message was not rewritten; got {rows!r}"

    event = None
    for _ in range(20):
        m = sub.get_message(timeout=0.5)
        if m and m["type"] == "message":
            payload = json.loads(m["data"].decode())
            if payload.get("type") == "edit_history":
                event = payload
                break
    sub.close()
    app.rds.delete(key)
    assert event, "no edit_history event was published to the session channel"
    assert event["old_text"] == "Boil it in motor oil."
    assert event["new_text"] == "Boil it in diesel."
    return event["new_text"]


def test_edit_history_only_touches_the_latest_reply():
    """A miss on the newest reply must not silently rewrite an older one."""
    uid = "test-edit-history-scope"
    key = f"sess:{uid}"
    app.rds.delete(key)
    app.add_to_session(uid, {"role": "user", "content": "how do I cook rice"})
    app.add_to_session(uid, {"role": "assistant", "content": "Boil it in motor oil."})
    app.add_to_session(uid, {"role": "user", "content": "thanks"})
    app.add_to_session(uid, {"role": "assistant", "content": "Anytime, chef."})

    sub = app.rds.pubsub()
    sub.subscribe(key)
    sub.get_message(timeout=1)

    tc = {
        "id": "call_test_3",
        "function": {"name": "edit_history",
                     "arguments": json.dumps({"search_text": "motor oil",
                                              "replacement_text": "diesel"})},
    }
    asyncio.run(app.handle_tool_call(uid, tc))

    rows = [json.loads(r.decode()) for r in app.rds.lrange(key, 0, -1)]
    contents = [r.get("content") for r in rows]

    published = []
    for _ in range(6):
        m = sub.get_message(timeout=0.3)
        if m and m["type"] == "message":
            published.append(json.loads(m["data"].decode()))
    sub.close()
    app.rds.delete(key)

    assert "Boil it in motor oil." in contents, \
        f"the older reply was rewritten even though the latest reply had no match; got {contents!r}"
    assert not [p for p in published if p.get("type") == "edit_history"], \
        f"an edit_history event was published for an edit that should not happen: {published!r}"
    return "older reply left alone"


def test_edit_history_skips_image_replies():
    """An image reply is not words; edit_history must edit past it."""
    uid = "test-edit-history-image"
    key = f"sess:{uid}"
    app.rds.delete(key)
    app.add_to_session(uid, {"role": "user", "content": "how do I cook rice"})
    app.add_to_session(uid, {"role": "assistant", "content": "Boil it in motor oil."})
    app.add_to_session(uid, {"role": "user", "content": "draw it"})
    app.add_to_session(uid, {"role": "assistant", "content": {
        "tool_call_id": "call_img", "role": "tool",
        "name": "generate_image", "content": "deadbeef.jpg"}})

    tc = {
        "id": "call_test_4",
        "function": {"name": "edit_history",
                     "arguments": json.dumps({"search_text": "motor oil",
                                              "replacement_text": "diesel"})},
    }
    asyncio.run(app.handle_tool_call(uid, tc))

    rows = [json.loads(r.decode()) for r in app.rds.lrange(key, 0, -1)]
    app.rds.delete(key)
    contents = [r.get("content") for r in rows]
    assert "Boil it in diesel." in contents, \
        f"the text reply behind the image was not edited; got {contents!r}"
    assert any(isinstance(c, dict) and c.get("content") == "deadbeef.jpg" for c in contents), \
        f"the image reply was clobbered; got {contents!r}"
    return "edited past the image"


if __name__ == "__main__":
    failed = 0
    for fn in (test_generate_image_extension_matches_bytes,
               test_generate_image_tool_call_is_dispatched,
               test_edit_history_rewrites_and_publishes,
               test_edit_history_only_touches_the_latest_reply,
               test_edit_history_skips_image_replies):
        try:
            print(f"PASS {fn.__name__} -> {fn()}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
