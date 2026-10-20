# Server-side game master for the play_game tool.
# The model plays the games as a tool call inside normal chat; this module
# owns the rules and the state. State shapes mirror fe/games.js exactly so
# the frontend renderers work unchanged.
import json
import random
import re

HANG_WORDS = [
    "router", "stream", "planet", "coffee", "guitar", "python", "forest", "rocket", "silver", "castle",
    "packet", "dragon", "anchor", "breeze", "candle", "dolphin", "ember", "falcon", "glacier", "harvest",
    "island", "jungle", "kettle", "lantern", "meadow", "nebula", "orchid", "pebble", "quartz", "ripple",
    "sunset", "temple", "umbrella", "velvet", "walnut", "compass", "thunder", "marble", "cactus", "pyramid",
]
PASSWORD_WORDS = [
    "ocean", "river", "mountain", "forest", "desert", "island", "valley", "meadow", "glacier", "volcano", "canyon", "waterfall", "thunder", "lightning", "rainbow", "sunset", "sunrise", "breeze", "storm", "cloud", "snowflake", "winter", "summer", "autumn", "blossom", "flower", "garden", "jungle", "prairie", "swamp", "beach", "cliff", "cavern", "lagoon", "harvest", "comet", "galaxy", "meteor", "planet", "eclipse", "dog", "cat", "horse", "dragon", "tiger", "lion", "elephant", "dolphin", "whale", "shark", "eagle", "owl", "falcon", "penguin", "rabbit", "fox", "wolf", "bear", "deer", "frog", "snake", "spider", "butterfly", "beetle", "octopus", "crab", "turtle", "monkey", "giraffe", "zebra", "kangaroo", "panda", "hedgehog", "otter", "squirrel", "raccoon", "camel", "moose", "walrus", "peacock", "seahorse", "jellyfish", "starfish", "ladybug", "dragonfly", "firefly", "caterpillar", "hummingbird", "flamingo", "dinosaur", "coffee", "pizza", "bread", "cheese", "apple", "banana", "orange", "lemon", "cherry", "grape", "mango", "peach", "honey", "sugar", "pepper", "garlic", "noodle", "waffle", "cookie", "muffin", "pancake", "popcorn", "chocolate", "butter", "pickle", "pretzel", "taco", "sushi", "pumpkin", "carrot", "mirror", "candle", "lantern", "compass", "anchor", "hammer", "ladder", "pillow", "blanket", "umbrella", "camera", "guitar", "piano", "violin", "trumpet", "telescope", "magnet", "battery", "rocket", "engine", "bicycle", "wagon", "kite", "balloon", "marble", "puzzle", "clock", "kettle", "teapot", "bucket", "basket", "ribbon", "button", "needle", "thread", "scissors", "pencil", "crayon", "easel", "mask", "crown", "throne", "shield", "sword", "arrow", "helmet", "armor", "hammock", "mailbox", "whistle", "castle", "palace", "bridge", "tower", "temple", "church", "lighthouse", "windmill", "barn", "cabin", "cottage", "market", "harbor", "station", "library", "museum", "stadium", "factory", "tunnel", "pyramid", "igloo", "tent", "fountain", "dungeon", "aquarium", "carousel", "treehouse", "courtyard", "chapel", "king", "queen", "knight", "pirate", "wizard", "witch", "doctor", "farmer", "sailor", "pilot", "chef", "clown", "ninja", "cowboy", "astronaut", "artist", "jester", "blacksmith", "hunter", "dancer", "silver", "gold", "copper", "iron", "diamond", "crystal", "pearl", "ruby", "emerald", "quartz", "amber", "granite", "bronze", "velvet", "satin", "feather", "pebble", "seashell", "driftwood", "coral", "robot", "snowman", "scarecrow", "campfire", "bonfire", "treasure", "fossil", "skeleton", "mummy", "unicorn", "mermaid",
]
# word + the taboo words the describer may not say
TABOO_WORDS = [
    {"word": "television", "taboo": ["tv", "screen", "watch", "remote"]},
    {"word": "airplane", "taboo": ["flight", "fly", "airport", "pilot"]},
    {"word": "refrigerator", "taboo": ["fridge", "kitchen", "cold", "freezer"]},
    {"word": "birthday", "taboo": ["cake", "candles", "party", "blow"]},
    {"word": "library", "taboo": ["books", "borrow", "shelves", "quiet"]},
    {"word": "bicycle", "taboo": ["bike", "pedals", "chain", "ride"]},
    {"word": "doctor", "taboo": ["nurse", "hospital", "patient", "medicine"]},
    {"word": "sunglasses", "taboo": ["shades", "eyes", "sun", "glasses"]},
    {"word": "beach", "taboo": ["sand", "ocean", "swim", "shells"]},
    {"word": "guitar", "taboo": ["strings", "music", "chords", "play"]},
    {"word": "computer", "taboo": ["mouse", "keyboard", "screen", "desk"]},
    {"word": "ocean", "taboo": ["wave", "salt", "swim", "shore"]},
    {"word": "candle", "taboo": ["flame", "wax", "light", "birthday"]},
    {"word": "umbrella", "taboo": ["rain", "storm", "open", "puddle"]},
    {"word": "sandwich", "taboo": ["bread", "lunch", "cheese", "eat"]},
    {"word": "pillow", "taboo": ["bed", "sleep", "soft", "night"]},
    {"word": "violin", "taboo": ["strings", "bow", "music", "fiddle"]},
    {"word": "castle", "taboo": ["tower", "king", "walls", "moat"]},
    {"word": "moon", "taboo": ["night", "sky", "light", "lunar"]},
    {"word": "pizza", "taboo": ["cheese", "slice", "oven", "pepperoni"]},
    {"word": "hammer", "taboo": ["nail", "tool", "strike", "mallet"]},
    {"word": "dolphin", "taboo": ["whale", "ocean", "swim", "pod"]},
    {"word": "rocket", "taboo": ["space", "launch", "missile", "blast"]},
    {"word": "mirror", "taboo": ["glass", "reflect", "reflection", "image"]},
    {"word": "spider", "taboo": ["web", "legs", "crawl", "arachnid"]},
    {"word": "snowman", "taboo": ["snow", "winter", "hat", "carrot"]},
    {"word": "volcano", "taboo": ["lava", "erupt", "mountain", "magma"]},
    {"word": "keyboard", "taboo": ["keys", "computer", "typing", "laptop"]},
    {"word": "treasure", "taboo": ["chest", "gold", "pirate", "buried"]},
    {"word": "bridge", "taboo": ["river", "cross", "road", "water"]},
    {"word": "campfire", "taboo": ["fire", "camping", "logs", "warm"]},
    {"word": "whistle", "taboo": ["referee", "flute", "blow", "sound"]},
    {"word": "lighthouse", "taboo": ["light", "coast", "ship", "tower"]},
    {"word": "pencil", "taboo": ["write", "eraser", "graphite", "draw"]},
    {"word": "squirrel", "taboo": ["nut", "tree", "tail", "park"]},
    {"word": "chocolate", "taboo": ["cocoa", "sweet", "cake", "bar"]},
    {"word": "highway", "taboo": ["road", "car", "drive", "exit"]},
    {"word": "lantern", "taboo": ["light", "fire", "glass", "camp"]},
]

GAME_NAMES = ["hangman", "password", "taboo", "poker", "blackjack"]
PW_ROUNDS = 10
PW_ATTEMPTS = 10
TABOO_ROUNDS = 10
TABOO_MAX_CLUES = 3
TABOO_MAX_STRIKES = 3


def gshuffle(a):
    a = list(a)
    random.shuffle(a)
    return a


def gother(who):
    return "user" if who == "model" else "model"


def gname(who):
    return "AI" if who == "model" else "you"


def gwords(text):
    return [w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 1]


# ---- card helpers (poker + blackjack) ----
SUITS = ["\u2660", "\u2665", "\u2666", "\u2663"]


def new_deck():
    d = []
    for s in SUITS:
        for r in ["A", "K", "Q", "J", "10", "9", "8", "7", "6", "5", "4", "3", "2"]:
            d.append(r + s)
    return gshuffle(d)


def card_value(c):
    """Blackjack value: A=1, K/Q/J=10, 2-10 face."""
    r = c[:-1]
    if r == "A":
        return 1
    if r in ("K", "Q", "J"):
        return 10
    return int(r)


def rank_value(c):
    """Poker rank for counting + straight: A=14, K=13, Q=12, J=11, 2-10 face.
    (card_value above folds J/Q/K to 10, which is right for blackjack totals
    but wrong for poker counting - a J+Q+K is three different cards, not
    'three of a kind of tens'.)"""
    r = c[:-1]
    if r == "A":
        return 14
    if r == "K":
        return 13
    if r == "Q":
        return 12
    if r == "J":
        return 11
    return int(r)


def bj_total(cards):
    t = 0
    aces = 0
    for c in cards:
        if card_value(c) == 1:
            aces += 1
            t += 11
        else:
            t += card_value(c)
    while t > 21 and aces:
        t -= 10
        aces -= 1
    return t


def eval5(cards):
    """5-card hand -> {rank, name, tb} (higher rank/tb wins)."""
    vals = sorted([rank_value(c) for c in cards], reverse=True)
    suits = [c[-1] for c in cards]
    flush = all(s == suits[0] for s in suits)
    uniq = []
    for v in vals:
        if v not in uniq:
            uniq.append(v)
    straight = False
    high = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight = True
            high = uniq[0]
        elif uniq[0] == 14 and uniq[1] == 5 and uniq[4] == 2:
            straight = True  # wheel A-5
            high = 5
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    groups = sorted([[n, v] for v, n in counts.items()], key=lambda g: (-g[0], -g[1]))
    if straight and flush:
        rank, name, tb = 8, "straight flush", [high]
    elif groups[0][0] == 4:
        rank, name, tb = 7, "four of a kind", groups
    elif len(groups) > 1 and groups[0][0] == 3 and groups[1][0] == 2:
        rank, name, tb = 6, "full house", groups
    elif flush:
        rank, name, tb = 5, "flush", vals
    elif straight:
        rank, name, tb = 4, "straight", [high]
    elif groups[0][0] == 3:
        rank, name, tb = 3, "three of a kind", groups
    elif len(groups) > 1 and groups[0][0] == 2 and groups[1][0] == 2:
        rank, name, tb = 2, "two pair", groups
    elif groups[0][0] == 2:
        rank, name, tb = 1, "pair", groups
    else:
        rank, name, tb = 0, "high card", vals
    return {"rank": rank, "name": name, "tb": tb}


def cmp_hand(a, b):
    if a["rank"] != b["rank"]:
        return a["rank"] - b["rank"]
    n = max(len(a["tb"]), len(b["tb"]))
    for i in range(n):
        x = a["tb"][i] if i < len(a["tb"]) else 0
        y = b["tb"][i] if i < len(b["tb"]) else 0
        if x != y:
            return x - y
    return 0


# ---- state construction ----
def new_state(name):
    if name == "hangman":
        return {
            "id": "hangman", "word": random.choice(HANG_WORDS), "guessed": [], "misses": 0,
            "turn": random.choice(["model", "user"]), "over": False, "result": "", "log": [],
        }
    if name == "password":
        deck = gshuffle(PASSWORD_WORDS)
        st = {
            "id": "password", "round": 1, "scores": {"model": 0, "user": 0}, "deck": deck,
            "secret": "", "giver": random.choice(["model", "user"]), "phase": "give",
            "clues": [], "guesses": [], "attempt": 0, "over": False, "result": "",
            "results": [], "log": [],
        }
        st["secret"] = st["deck"].pop()
        return st
    if name == "taboo":
        deck = gshuffle(TABOO_WORDS)
        st = {
            "id": "taboo", "round": 1, "scores": {"model": 0, "user": 0}, "deck": deck,
            "entry": None, "describer": random.choice(["model", "user"]), "phase": "describe",
            "clues": [], "guesses": [], "strikes": 0, "over": False, "result": "",
            "results": [], "log": [],
        }
        st["entry"] = st["deck"].pop()
        return st
    if name == "poker":
        deck = new_deck()
        st = {
            "id": "poker", "phase": "swap-user", "deck": deck, "user": [], "model": [],
            "log": [], "over": False, "result": "", "stake": 100,
        }
        for _ in range(5):
            st["user"].append(deck.pop())
            st["model"].append(deck.pop())
        return st
    if name == "blackjack":
        deck = new_deck()
        st = {
            "id": "blackjack", "phase": "user", "deck": deck, "user": [], "model": [],
            "log": [], "over": False, "result": "", "stake": 100,
        }
        st["user"].append(deck.pop())
        st["model"].append(deck.pop())
        st["model"].append(deck.pop())  # dealer's hole card
        st["user"].append(deck.pop())
        return st
    return None


def actor(st):
    if st.get("over"):
        return None
    i = st["id"]
    if i == "hangman":
        return st["turn"]
    if i == "password":
        return st["giver"] if st["phase"] == "give" else gother(st["giver"])
    if i == "taboo":
        return st["describer"] if st["phase"] == "describe" else gother(st["describer"])
    if i == "poker":
        return "user" if st["phase"] == "swap-user" else "model"
    if i == "blackjack":
        return st["phase"]
    return None


def stake(st):
    return st.get("stake", 0)


def payout(st):
    if not st.get("over"):
        return 0
    r = st.get("result", "")
    if st["id"] == "poker":
        if r.startswith("you win"):
            return 200
        if r.startswith("tie"):
            return 100
        return 0
    if st["id"] == "blackjack":
        if "you win" in r:
            return 200
        if "push" in r:
            return 100
    return 0


def stop_game(st):
    if not st.get("over"):
        st["over"] = True
        st["result"] = "abandoned"


# ---- move validation: is this user text a game move, or just chat? ----
def is_move(st, text, who):
    if who == "model":
        return True
    t = (text or "").strip()
    i = st["id"]
    if i == "hangman":
        return len(t) == 1 and t.lower() in "abcdefghijklmnopqrstuvwxyz"
    if i == "password":
        return len(t.split()) == 1
    if i == "taboo":
        return True  # on the user's turn, anything typed is the move
    if i == "poker":
        t2 = t.lower()
        return t2 in ("none", "nothing", "keep", "keep all", "keep them", "keep them all",
                     "no swap", "no swaps", "all", "them all", "all of them") or bool(_parse_poker_nums(t))
    if i == "blackjack":
        return t.lower() in ("hit", "stand", "h", "s")
    return False


# ---- the rules ----
def apply_move(st, text, who):
    """Apply one move. Returns (note, invalid)."""
    i = st["id"]
    if i == "hangman":
        return _apply_hangman(st, text, who)
    if i == "password":
        return _apply_password(st, text, who)
    if i == "taboo":
        return _apply_taboo(st, text, who)
    if i == "poker":
        return _apply_poker(st, text, who)
    if i == "blackjack":
        return _apply_blackjack(st, text, who)
    return "unknown game", True


def _apply_hangman(st, text, who):
    t = (text or "").lower()
    pool = [c for c in t if c in "abcdefghijklmnopqrstuvwxyz" and c not in st["guessed"]]
    letter = pool[0] if pool else None
    if not letter:
        if who == "user":
            return "type a single letter (a-z)", True
        # model repeated itself - pick something new so the game keeps moving
        rest = [c for c in "abcdefghijklmnopqrstuvwxyz" if c not in st["guessed"]]
        if not rest:
            st["over"] = True
            st["result"] = "the alphabet ran out"
            return "exhausted", False
        letter = random.choice(rest)
    st["guessed"].append(letter)
    hit = letter in st["word"]
    if not hit:
        st["misses"] += 1
    note = 'guess "' + letter + '" - ' + ("hit" if hit else "miss")
    if all(c in st["guessed"] for c in st["word"]):
        st["over"] = True
        st["result"] = "solved: " + st["word"]
        note += " - solved!"
    elif st["misses"] >= 6:
        st["over"] = True
        st["result"] = "the word wins: " + st["word"]
        note += " - out of tries"
    else:
        st["turn"] = gother(st["turn"])
    return note, False


def _apply_password(st, text, who):
    words = gwords(text)
    if st["phase"] == "give":
        clue = next((w for w in words if w != st["secret"]), None) or "hint"
        st["clues"].append(clue)
        st["phase"] = "guess"
        return gname(who) + ' clue "' + clue + '" (worth ' + str(PW_ATTEMPTS - st["attempt"]) + ")", False
    guess = words[0] if words else "(none)"
    st["guesses"].append(guess)
    correct = (
        guess == st["secret"]
        or (len(guess) >= 4 and (guess in st["secret"] or st["secret"] in guess))
    )
    if correct:
        pts = PW_ATTEMPTS - st["attempt"]
        st["scores"][who] += pts
        st["results"].append("round " + str(st["round"]) + ": " + gname(who) + ' got "' + st["secret"] + '" +' + str(pts))
        return _end_password_round(st)
    st["attempt"] += 1
    if st["attempt"] >= PW_ATTEMPTS:
        st["results"].append("round " + str(st["round"]) + ': nobody got "' + st["secret"] + '" - 0')
        return _end_password_round(st)
    st["giver"] = gother(st["giver"])
    st["phase"] = "give"
    return (
        gname(who) + ' missed "' + guess + '" - ' + gname(st["giver"]) + " now clues, worth "
        + str(PW_ATTEMPTS - st["attempt"]),
        False,
    )


def _end_password_round(st):
    if st["round"] >= PW_ROUNDS:
        st["over"] = True
        a, b = st["scores"]["model"], st["scores"]["user"]
        st["result"] = (
            "tie " + str(a) + "-" + str(b)
            if a == b
            else ("AI" if a > b else "you") + " win" + ("s" if a > b else "") + " " + str(max(a, b)) + "-" + str(min(a, b))
        )
        return "final: " + st["result"], False
    st["round"] += 1
    if not st["deck"]:
        st["deck"] = gshuffle(PASSWORD_WORDS)
    st["secret"] = st["deck"].pop()
    st["giver"] = gother(st["giver"])
    st["phase"] = "give"
    st["clues"] = []
    st["guesses"] = []
    st["attempt"] = 0
    return "round " + str(st["round"]) + " - " + gname(st["giver"]) + " gives the first clue", False


def _taboo_value(st):
    return [10, 7, 5][min(len(st["clues"]), TABOO_MAX_CLUES) - 1]


def _apply_taboo(st, text, who):
    e = st["entry"]
    if st["phase"] == "describe":
        desc = (text or "").strip().lower()
        st["clues"].append(desc)
        toks = re.split(r"\W+", desc)
        used = [w for w in list(e["taboo"]) + [e["word"]] if w in toks]
        if used:
            st["strikes"] += len(used)
            note = gname(who) + " used a forbidden word: " + ", ".join(used)
            if st["strikes"] >= TABOO_MAX_STRIKES:
                st["results"].append(
                    "round " + str(st["round"]) + ": " + gname(st["describer"]) + " tripped "
                    + str(TABOO_MAX_STRIKES) + ' times - it was "' + e["word"] + '" - 0'
                )
                note += " - 3 strikes, round over"
                return _end_taboo_round(st, note)
            return note + " - " + str(TABOO_MAX_STRIKES - st["strikes"]) + " strikes left", False
        st["phase"] = "guess"
        return gname(who) + " describes it (worth " + str(_taboo_value(st)) + ")", False
    words = gwords(text)
    guess = words[0] if words else "(none)"
    st["guesses"].append(guess)
    if guess == e["word"]:
        pts = _taboo_value(st)
        st["scores"][who] += pts
        st["results"].append("round " + str(st["round"]) + ": " + gname(who) + ' got "' + e["word"] + '" +' + str(pts))
        return _end_taboo_round(st)
    if len(st["clues"]) >= TABOO_MAX_CLUES:
        st["results"].append("round " + str(st["round"]) + ': not solved - it was "' + e["word"] + '" - 0')
        return _end_taboo_round(st)
    st["phase"] = "describe"
    return gname(who) + ' missed "' + guess + '" - another description', False


def _end_taboo_round(st, note=None):
    if st["round"] >= TABOO_ROUNDS:
        st["over"] = True
        a, b = st["scores"]["model"], st["scores"]["user"]
        st["result"] = (
            "tie " + str(a) + "-" + str(b)
            if a == b
            else ("AI" if a > b else "you") + " win" + ("s" if a > b else "") + " " + str(max(a, b)) + "-" + str(min(a, b))
        )
        return (note or "final: " + st["result"]), False
    st["round"] += 1
    if not st["deck"]:
        st["deck"] = gshuffle(TABOO_WORDS)
    st["entry"] = st["deck"].pop()
    st["describer"] = gother(st["describer"])
    st["phase"] = "describe"
    st["clues"] = []
    st["guesses"] = []
    st["strikes"] = 0
    return ((note + " - ") if note else "") + "round " + str(st["round"]) + " - " + gname(st["describer"]) + " describes", False


def _parse_poker_nums(text):
    """Extract card numbers (1-5) from a model/user move. Accepts '2 4 5',
    '2,4,5', '2-5' (range -> 2,3,4,5), '2-4-5', and natural mentions like
    'cards 2 and 4'. Returns an ordered, de-duplicated list (may be empty)."""
    t = (text or "").strip().lower()
    expanded = []
    for part in re.split(r"[\s,]+", t):
        if not part:
            continue
        m = re.fullmatch(r"([1-5])\s*-\s*([1-5])", part)
        if m:
            # single dash between two digits = a range
            a, b = int(m.group(1)), int(m.group(2))
            lo, hi = (a, b) if a <= b else (b, a)
            expanded.extend(range(lo, hi + 1))
        elif "-" in part:
            # multiple dashes = a dash-separated list (2-4-5 -> 2,4,5)
            for d in part.split("-"):
                if re.fullmatch(r"[1-5]", d):
                    expanded.append(int(d))
        elif re.fullmatch(r"[1-5]", part):
            expanded.append(int(part))
        # else: ignore non-numeric tokens
    found = []
    for n in expanded:
        if 1 <= n <= 5 and n not in found:
            found.append(n)
    return found


def _apply_poker(st, text, who):
    t = (text or "").strip().lower()
    if t in ("none", "nothing", "keep", "keep all", "keep them", "keep them all",
             "no swap", "no swaps", "all", "them all", "all of them"):
        nums = []
    else:
        nums = _parse_poker_nums(t)
        if not nums:
            return "name the card numbers (1-5) to swap, or say 'none'", True
        nums = nums[:3]
    if who == "user" and st["phase"] == "swap-user":
        st["userSwaps"] = len(nums)
        for n in nums:
            st["user"][n - 1] = st["deck"].pop()
        st["phase"] = "swap-model"
        return "you swap " + (str(len(nums)) if nums else "nothing") + " - the AI's move", False
    if who == "model" and st["phase"] == "swap-model":
        st["modelSwaps"] = len(nums)
        for n in nums:
            st["model"][n - 1] = st["deck"].pop()
        # showdown - the board is the referee
        st["ue"] = eval5(st["user"])
        st["me"] = eval5(st["model"])
        c = cmp_hand(st["ue"], st["me"])
        st["over"] = True
        st["result"] = (
            "you win: " + st["ue"]["name"] + " beats " + st["me"]["name"] + " - pot is yours"
            if c > 0
            else "the AI wins: " + st["me"]["name"] + " beats " + st["ue"]["name"]
            if c < 0
            else "tie - pot returned"
        )
        return st["result"], False
    return "not your move", True


def _apply_blackjack(st, text, who):
    t = (text or "").lower().strip()
    if t not in ("hit", "stand", "h", "s"):
        return "say 'hit' or 'stand'", True
    hit = t in ("hit", "h")
    if who == "user" and st["phase"] == "user":
        if hit:
            st["user"].append(st["deck"].pop())
            tot = bj_total(st["user"])
            if tot > 21:
                st["over"] = True
                st["result"] = "bust - " + str(tot) + " - the AI takes the pot"
            elif tot == 21:
                st["over"] = True
                st["result"] = "21 - you win the pot"
            else:
                st["phase"] = "model"
        else:
            st["phase"] = "model"
        return (
            st["result"]
            if st["over"]
            else "your total: " + str(bj_total(st["user"])) + " - dealer's move"
        ), False
    if who == "model" and st["phase"] == "model":
        if hit:
            st["model"].append(st["deck"].pop())
            tot = bj_total(st["model"])
            if tot > 21:
                st["over"] = True
                st["result"] = "the dealer busts - " + str(tot) + " - you win the pot"
            elif tot == 21:
                st["over"] = True
                st["result"] = "the dealer has 21 - the AI takes the pot"
            else:
                st["phase"] = "user"
        else:
            ut, mt = bj_total(st["user"]), bj_total(st["model"])
            st["over"] = True
            st["result"] = (
                "push - both " + str(ut) + " - pot returned"
                if ut == mt
                else "you win " + str(ut) + " to " + str(mt) + " - pot is yours"
                if ut > mt
                else "the dealer wins " + str(mt) + " to " + str(ut) + " - the AI takes the pot"
            )
        return (
            st["result"]
            if st["over"]
            else "dealer's total: " + str(bj_total(st["model"]))
            + (" - your move" if st["phase"] == "user" else " - the dealer stands")
        ), False
    return "not your move", True


# ---- the board, from the model's point of view ----
# Secrets are hidden when the model is the guesser: it only ever sees what a
# player across the table would see (that is also where the cheating lives).
def board_text(st):
    i = st["id"]
    over = st.get("over")
    res = " - over: " + st["result"] if over else ""
    if i == "hangman":
        mask = " ".join(c if c in st["guessed"] else "_" for c in st["word"])
        turn = "game over" if over else ("YOUR turn" if st["turn"] == "model" else "the player's turn")
        return (
            "hangman - word: " + mask + "; guessed: " + (", ".join(st["guessed"]) or "(none)")
            + "; misses " + str(st["misses"]) + "/6; " + turn + res
        )
    if i == "password":
        give = st["phase"] == "give"
        is_model = (st["giver"] == "model") if give else (st["giver"] != "model")
        secret = ' the secret word is "' + st["secret"] + '"' if (give and st["giver"] == "model") else ""
        turn = "game over" if over else ("YOUR turn" if is_model else "the player's turn")
        return (
            "password - round " + str(st["round"]) + "/10;" + secret
            + " clues: " + (", ".join(st["clues"]) or "(none)")
            + "; wrong guesses: " + (", ".join(st["guesses"]) or "(none)")
            + "; scores you " + str(st["scores"]["user"]) + " / AI " + str(st["scores"]["model"])
            + "; " + turn + res
        )
    if i == "taboo":
        e = st["entry"]
        desc = st["phase"] == "describe"
        secret = (
            ' the word is "' + e["word"] + '" - do NOT say: ' + ", ".join(e["taboo"])
            if (desc and st["describer"] == "model")
            else ""
        )
        is_model = (st["describer"] == "model") if desc else (st["describer"] != "model")
        turn = "game over" if over else ("YOUR turn" if is_model else "the player's turn")
        return (
            "taboo - round " + str(st["round"]) + "/10;" + secret
            + " descriptions: " + (" | ".join('"' + c + '"' for c in st["clues"]) or "(none)")
            + "; wrong guesses: " + (", ".join(st["guesses"]) or "(none)")
            + "; strikes " + str(st["strikes"]) + "/3"
            + "; scores you " + str(st["scores"]["user"]) + " / AI " + str(st["scores"]["model"])
            + "; " + turn + res
        )
    if i == "poker":
        if over:
            return (
                "poker - over: " + st["result"]
                + "; your hand: " + " ".join(st["model"]) + " (" + st["me"]["name"] + ")"
                + "; player: " + " ".join(st["user"]) + " (" + st["ue"]["name"] + ")"
            )
        turn = (
            "YOUR turn - swap up to 3 cards (reply with card numbers 1-5, or 'none')"
            if st["phase"] == "swap-model"
            else "the player's turn"
        )
        return "poker - your cards: " + " ".join(st["model"]) + "; " + turn
    if i == "blackjack":
        if over:
            return (
                "blackjack - over: " + st["result"]
                + "; your hand: " + " ".join(st["model"]) + " (" + str(bj_total(st["model"])) + ")"
                + "; player: " + " ".join(st["user"]) + " (" + str(bj_total(st["user"])) + ")"
            )
        turn = "YOUR turn - 'hit' or 'stand'" if st["phase"] == "model" else "the player's turn"
        # open table: the dealer sees everything
        return (
            "blackjack - your hand: " + " ".join(st["model"]) + " (" + str(bj_total(st["model"])) + ")"
            + "; player's hand: " + " ".join(st["user"]) + " (" + str(bj_total(st["user"])) + ")"
            + "; " + turn
        )
    return ""


# ---- the live game block: injected into the model's context on every turn.
# This is the ONLY place the model reads the board - history rows are neutral
# markers, so the model cannot imitate board text as plain speech.
_MOVE_HINT = {
    "hangman": "a single letter (a-z)",
    "password": "one word (your clue, or your guess)",
    "taboo": "your description, or your one-word guess",
    "poker": "the card numbers (1-5) to swap, e.g. '2 4', or 'none'",
    "blackjack": "'hit' or 'stand'",
}


def game_block(st):
    board = board_text(st)
    a = actor(st)
    log = st.get("log") or []
    moves = []
    for m in log[-10:]:
        s = m.get("who", "?") + " " + json.dumps(m.get("text", ""))
        if m.get("note"):
            s += " [" + m["note"] + "]"
        moves.append(s)
    hint = _MOVE_HINT.get(st["id"], "your move")
    if st.get("over"):
        turn = "Game over: " + st.get("result", "")
    elif a == "model":
        turn = (
            "It is YOUR turn - call the play_game tool (game=\"" + st["id"] + "\", move=your move) "
            "to make your move. For " + st["id"] + " your move is: " + hint +
            ". Call the tool; do not type the move as text."
        )
    else:
        turn = "It is the player's turn."
    out = "[GAME " + st["id"].upper() + "]\nboard: " + board
    if moves:
        out += "\nmoves: " + "; ".join(moves)
    out += "\n" + turn
    return out
