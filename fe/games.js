// Game master — pure client-side referee. JS owns the game state, renders the
// board into the chat, builds the model's prompt, and applies both players'
// moves. The model never sees the rules — only a one-shot prompt per move.
//
// Every game object exposes:
//   newState()          -> serializable state (persisted to localStorage)
//   actor(st)           -> "model" | "user" | null (game over)
//   userPrompt(st)      -> input placeholder text on the user's turn
//   modelPrompt(st)     -> {system, user} for a one-shot /game model call
//   apply(st, text,who) -> mutates st; returns {note} or {invalid:true}
//   render(st)          -> HTML for the board

function gshuffle(a) {
  a = a.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}
function gpick(a) { return a[Math.floor(Math.random() * a.length)]; }
function gesc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function gwords(text) {
  return (String(text || "").toLowerCase().match(/[a-z]+/g) || []).filter((w) => w.length > 1);
}
function gother(who) { return who === "model" ? "user" : "model"; }
function gname(who) { return who === "model" ? "AI" : "you"; }

// ---- solution lists (reused from the dyva game room demo) ----
const HANG_WORDS = [
  'router','stream','planet','coffee','guitar','python','forest','rocket','silver','castle',
  'packet','dragon','anchor','breeze','candle','dolphin','ember','falcon','glacier','harvest',
  'island','jungle','kettle','lantern','meadow','nebula','orchid','pebble','quartz','ripple',
  'sunset','temple','umbrella','velvet','walnut','compass','thunder','marble','cactus','pyramid',
];
const HANG_PICS = [
  ' +---+\n |   |\n     |\n     |\n     |\n=======',
  ' +---+\n |   |\n O   |\n     |\n     |\n=======',
  ' +---+\n |   |\n O   |\n |   |\n     |\n=======',
  ' +---+\n |   |\n O   |\n/|   |\n     |\n=======',
  ' +---+\n |   |\n O   |\n/|\\  |\n     |\n=======',
  ' +---+\n |   |\n O   |\n/|\\  |\n/    |\n=======',
  ' +---+\n |   |\n O   |\n/|\\  |\n/ \\  |\n=======',
];
const PASSWORD_WORDS = [
  'ocean','river','mountain','forest','desert','island','valley','meadow','glacier','volcano','canyon','waterfall','thunder','lightning','rainbow','sunset','sunrise','breeze','storm','cloud','snowflake','winter','summer','autumn','blossom','flower','garden','jungle','prairie','swamp','beach','cliff','cavern','lagoon','harvest','comet','galaxy','meteor','planet','eclipse','dog','cat','horse','dragon','tiger','lion','elephant','dolphin','whale','shark','eagle','owl','falcon','penguin','rabbit','fox','wolf','bear','deer','frog','snake','spider','butterfly','beetle','octopus','crab','turtle','monkey','giraffe','zebra','kangaroo','panda','hedgehog','otter','squirrel','raccoon','camel','moose','walrus','peacock','seahorse','jellyfish','starfish','ladybug','dragonfly','firefly','caterpillar','hummingbird','flamingo','dinosaur','coffee','pizza','bread','cheese','apple','banana','orange','lemon','cherry','grape','mango','peach','honey','sugar','pepper','garlic','noodle','waffle','cookie','muffin','pancake','popcorn','chocolate','butter','pickle','pretzel','taco','sushi','pumpkin','carrot','mirror','candle','lantern','compass','anchor','hammer','ladder','pillow','blanket','umbrella','camera','guitar','piano','violin','trumpet','telescope','magnet','battery','rocket','engine','bicycle','wagon','kite','balloon','marble','puzzle','clock','kettle','teapot','bucket','basket','ribbon','button','needle','thread','scissors','pencil','crayon','easel','mask','crown','throne','shield','sword','arrow','helmet','armor','hammock','mailbox','whistle','castle','palace','bridge','tower','temple','church','lighthouse','windmill','barn','cabin','cottage','market','harbor','station','library','museum','stadium','factory','tunnel','pyramid','igloo','tent','fountain','dungeon','aquarium','carousel','treehouse','courtyard','chapel','king','queen','knight','pirate','wizard','witch','doctor','farmer','sailor','pilot','chef','clown','ninja','cowboy','astronaut','artist','jester','blacksmith','hunter','dancer','silver','gold','copper','iron','diamond','crystal','pearl','ruby','emerald','quartz','amber','granite','bronze','velvet','satin','feather','pebble','seashell','driftwood','coral','robot','snowman','scarecrow','campfire','bonfire','treasure','fossil','skeleton','mummy','unicorn','mermaid',
];
// word + the taboo words the describer may not say
const TABOO_WORDS = [
  { word: 'television', taboo: ['tv', 'screen', 'watch', 'remote'] },
  { word: 'airplane', taboo: ['flight', 'fly', 'airport', 'pilot'] },
  { word: 'refrigerator', taboo: ['fridge', 'kitchen', 'cold', 'freezer'] },
  { word: 'birthday', taboo: ['cake', 'candles', 'party', 'blow'] },
  { word: 'library', taboo: ['books', 'borrow', 'shelves', 'quiet'] },
  { word: 'bicycle', taboo: ['bike', 'pedals', 'chain', 'ride'] },
  { word: 'doctor', taboo: ['nurse', 'hospital', 'patient', 'medicine'] },
  { word: 'sunglasses', taboo: ['shades', 'eyes', 'sun', 'glasses'] },
  { word: 'beach', taboo: ['sand', 'ocean', 'swim', 'shells'] },
  { word: 'guitar', taboo: ['strings', 'music', 'chords', 'play'] },
  { word: 'computer', taboo: ['mouse', 'keyboard', 'screen', 'desk'] },
  { word: 'ocean', taboo: ['wave', 'salt', 'swim', 'shore'] },
  { word: 'candle', taboo: ['flame', 'wax', 'light', 'birthday'] },
  { word: 'umbrella', taboo: ['rain', 'storm', 'open', 'puddle'] },
  { word: 'sandwich', taboo: ['bread', 'lunch', 'cheese', 'eat'] },
  { word: 'pillow', taboo: ['bed', 'sleep', 'soft', 'night'] },
  { word: 'violin', taboo: ['strings', 'bow', 'music', 'fiddle'] },
  { word: 'castle', taboo: ['tower', 'king', 'walls', 'moat'] },
  { word: 'moon', taboo: ['night', 'sky', 'light', 'lunar'] },
  { word: 'pizza', taboo: ['cheese', 'slice', 'oven', 'pepperoni'] },
  { word: 'hammer', taboo: ['nail', 'tool', 'strike', 'mallet'] },
  { word: 'dolphin', taboo: ['whale', 'ocean', 'swim', 'pod'] },
  { word: 'rocket', taboo: ['space', 'launch', 'missile', 'blast'] },
  { word: 'mirror', taboo: ['glass', 'reflect', 'reflection', 'image'] },
  { word: 'spider', taboo: ['web', 'legs', 'crawl', 'arachnid'] },
  { word: 'snowman', taboo: ['snow', 'winter', 'hat', 'carrot'] },
  { word: 'volcano', taboo: ['lava', 'erupt', 'mountain', 'magma'] },
  { word: 'keyboard', taboo: ['keys', 'computer', 'typing', 'laptop'] },
  { word: 'treasure', taboo: ['chest', 'gold', 'pirate', 'buried'] },
  { word: 'bridge', taboo: ['river', 'cross', 'road', 'water'] },
  { word: 'campfire', taboo: ['fire', 'camping', 'logs', 'warm'] },
  { word: 'whistle', taboo: ['referee', 'flute', 'blow', 'sound'] },
  { word: 'lighthouse', taboo: ['light', 'coast', 'ship', 'tower'] },
  { word: 'pencil', taboo: ['write', 'eraser', 'graphite', 'draw'] },
  { word: 'squirrel', taboo: ['nut', 'tree', 'tail', 'park'] },
  { word: 'chocolate', taboo: ['cocoa', 'sweet', 'cake', 'bar'] },
  { word: 'highway', taboo: ['road', 'car', 'drive', 'exit'] },
  { word: 'lantern', taboo: ['light', 'fire', 'glass', 'camp'] },
];

// ---- Hangman: shared word, alternating letter guesses, 6 misses ----
const HANGMAN = {
  id: 'hangman',
  label: 'Hangman',
  newState() {
    return {
      word: gpick(HANG_WORDS),
      guessed: [],
      misses: 0,
      turn: Math.random() < 0.5 ? 'model' : 'user',
      over: false,
      result: '',
      log: [],
    };
  },
  actor(st) { return st.over ? null : st.turn; },
  userPrompt() { return 'your letter (a–z)…'; },
  _masked(st) {
    return st.word.split('').map((c) => (st.guessed.includes(c) ? c : '_')).join(' ');
  },
  modelPrompt(st) {
    return {
      system:
        'You are playing hangman with a human partner, taking turns. You are both solving the same hidden word. ' +
        'Guess one letter at a time. Reply with ONLY a single new letter (a-z) you have not guessed yet. No words, no punctuation.',
      user:
        'Word: ' + this._masked(st) +
        '\nAlready guessed: ' + (st.guessed.join(' ') || '(none)') +
        '\nYour letter:',
    };
  },
  apply(st, text, who) {
    const pool = String(text || '')
      .toLowerCase()
      .replace(/[^a-z]/g, '')
      .split('')
      .filter((c) => !st.guessed.includes(c));
    let letter = pool[0];
    if (!letter) {
      if (who === 'user') return { invalid: true, note: 'type a single letter (a–z)' };
      // model repeated itself — pick something new so the game keeps moving
      const rest = 'abcdefghijklmnopqrstuvwxyz'.split('').filter((c) => !st.guessed.includes(c));
      if (!rest.length) { st.over = true; st.result = 'the alphabet ran out'; return { note: 'exhausted' }; }
      letter = rest[Math.floor(Math.random() * rest.length)];
    }
    st.guessed.push(letter);
    const hit = st.word.includes(letter);
    if (!hit) st.misses++;
    let note = 'guess "' + letter + '"' + (hit ? ' — hit' : ' — miss');
    if (st.word.split('').every((c) => st.guessed.includes(c))) {
      st.over = true;
      st.result = 'solved: ' + st.word;
      note += ' — solved!';
    } else if (st.misses >= 6) {
      st.over = true;
      st.result = 'the word wins: ' + st.word;
      note += ' — out of tries';
    } else {
      st.turn = gother(st.turn);
    }
    return { note };
  },
  render(st) {
    const pic = HANG_PICS[Math.min(st.misses, HANG_PICS.length - 1)];
    const word = st.word
      .split('')
      .map((c) => (st.guessed.includes(c) ? '<b>' + c + '</b>' : '_'))
      .join(' ');
    const guessed = st.guessed
      .map((g) => '<span class="' + (st.word.includes(g) ? 'ghit' : 'gmiss') + '">' + g + '</span>')
      .join(' ');
    const turn = st.over
      ? '— ' + gesc(st.result)
      : st.turn === 'model'
        ? 'my turn — thinking…'
        : 'your turn — guess a letter';
    return (
      '<div class="game-hd">🎯 Hangman</div>' +
      '<pre class="game-hang">' + pic + '</pre>' +
      '<div class="game-word">' + word + '</div>' +
      '<div class="game-meta">guessed: ' + (guessed || '—') + ' &nbsp;·&nbsp; misses: ' + st.misses + '/6</div>' +
      '<div class="game-turn">' + turn + '</div>'
    );
  },
};

// ---- Password: 10 rounds, one-word clue then a guess; wrong guess hands the
// round over with one point less (10 → 9 → …). ----
const PW_ROUNDS = 10;
const PW_ATTEMPTS = 10;
const PASSWORD = {
  id: 'password',
  label: 'Password',
  newState() {
    const st = {
      round: 1,
      scores: { model: 0, user: 0 },
      deck: gshuffle(PASSWORD_WORDS),
      secret: '',
      giver: Math.random() < 0.5 ? 'model' : 'user',
      phase: 'give',
      clues: [],
      guesses: [],
      attempt: 0,
      over: false,
      result: '',
      results: [],
      log: [],
    };
    st.secret = st.deck.pop();
    return st;
  },
  actor(st) { return st.over ? null : st.phase === 'give' ? st.giver : gother(st.giver); },
  userPrompt(st) { return st.phase === 'give' ? 'your one-word clue…' : 'your guess…'; },
  modelPrompt(st) {
    if (st.phase === 'give') {
      return {
        system:
          'You are the clue-giver in the word game Password against a human. The secret password is "' + st.secret + '". ' +
          'Give a SINGLE one-word clue that leads to the password. The clue must be one word, must NOT be the password ' +
          '(or any form of it), and must not repeat a clue already used. Reply with ONLY that one word.',
        user:
          'Clues given this round: ' + (st.clues.join(', ') || '(none)') +
          '\nGuesses tried (all wrong): ' + (st.guesses.join(', ') || '(none)') +
          '\nYour one-word clue:',
      };
    }
    return {
      system:
        'You are guessing in the word game Password against a human. Say the single secret word from the clues. ' +
        'Reply with ONLY your one-word guess.',
      user:
        'Clues so far: ' + (st.clues.join(', ') || '(none)') +
        '\nGuesses already tried (all wrong): ' + (st.guesses.join(', ') || '(none)') +
        '\nYour guess:',
    };
  },
  apply(st, text, who) {
    const words = gwords(text);
    if (st.phase === 'give') {
      const clue = words.filter((w) => w !== st.secret)[0] || 'hint';
      st.clues.push(clue);
      st.phase = 'guess';
      return { note: gname(who) + ' clue "' + clue + '" (worth ' + (PW_ATTEMPTS - st.attempt) + ')' };
    }
    const guess = words[0] || '(none)';
    st.guesses.push(guess);
    const correct =
      guess === st.secret ||
      (guess.length >= 4 && (guess.includes(st.secret) || st.secret.includes(guess)));
    if (correct) {
      const pts = PW_ATTEMPTS - st.attempt;
      st.scores[who] += pts;
      st.results.push('round ' + st.round + ': ' + gname(who) + ' got "' + st.secret + '" +' + pts);
      return this._endRound(st);
    }
    st.attempt++;
    if (st.attempt >= PW_ATTEMPTS) {
      st.results.push('round ' + st.round + ': nobody got "' + st.secret + '" — 0');
      return this._endRound(st);
    }
    st.giver = gother(st.giver);
    st.phase = 'give';
    return {
      note:
        gname(who) + ' missed "' + guess + '" — ' + gname(st.giver) + ' now clues, worth ' +
        (PW_ATTEMPTS - st.attempt),
    };
  },
  _endRound(st) {
    if (st.round >= PW_ROUNDS) {
      st.over = true;
      const a = st.scores.model, b = st.scores.user;
      st.result =
        a === b
          ? 'tie ' + a + '–' + b
          : (a > b ? 'AI' : 'you') + ' win' + (a > b ? 's' : '') + ' ' + Math.max(a, b) + '–' + Math.min(a, b);
      return { note: 'final: ' + st.result };
    }
    st.round++;
    if (!st.deck.length) st.deck = gshuffle(PASSWORD_WORDS);
    st.secret = st.deck.pop();
    st.giver = gother(st.giver);
    st.phase = 'give';
    st.clues = [];
    st.guesses = [];
    st.attempt = 0;
    return { note: 'round ' + st.round + ' — ' + gname(st.giver) + ' gives the first clue' };
  },
  render(st) {
    const rows = st.clues
      .map((c) => '<div class="ev"><span class="tag">clue</span>"' + gesc(c) + '"</div>')
      .join('');
    const gs = st.guesses
      .map((g) => '<div class="ev"><span class="tag">guess</span>"' + gesc(g) + '"</div>')
      .join('');
    const results = st.results
      .map((r) => '<div class="ev sc">' + gesc(r) + '</div>')
      .join('');
    let turn;
    if (st.over) turn = '— ' + gesc(st.result);
    else if (this.actor(st) === 'model') turn = 'my turn — thinking…';
    else if (st.phase === 'give') turn = 'your turn — one-word clue for "' + st.secret + '"';
    else turn = 'your turn — guess the word';
    return (
      '<div class="game-hd">🔑 Password <span class="gscore">round ' + st.round + '/' + PW_ROUNDS + '</span></div>' +
      '<div class="gscores"><div class="gteam">you <b>' + st.scores.user + '</b></div>' +
      '<div class="gteam">AI <b>' + st.scores.model + '</b></div></div>' +
      (results ? '<div class="gresults">' + results + '</div>' : '') +
      '<div class="feed">' + (rows || gs ? rows + gs : '') + '</div>' +
      '<div class="game-turn">' + turn + '</div>'
    );
  },
};

// ---- Taboo: 10 rounds. The describer talks around the word without using the
// taboo words (3 strikes = round lost); the guesser has the clues to land it.
// Points by how few clues it took: 10 / 7 / 5. ----
const TABOO_ROUNDS = 10;
const TABOO_MAX_CLUES = 3;
const TABOO_MAX_STRIKES = 3;
const TABOO = {
  id: 'taboo',
  label: 'Taboo',
  newState() {
    const st = {
      round: 1,
      scores: { model: 0, user: 0 },
      deck: gshuffle(TABOO_WORDS),
      entry: null,
      describer: Math.random() < 0.5 ? 'model' : 'user',
      phase: 'describe',
      clues: [],
      guesses: [],
      strikes: 0,
      over: false,
      result: '',
      results: [],
      log: [],
    };
    st.entry = st.deck.pop();
    return st;
  },
  actor(st) { return st.over ? null : st.phase === 'describe' ? st.describer : gother(st.describer); },
  userPrompt(st) { return st.phase === 'describe' ? 'describe it — avoid the red words…' : 'your guess…'; },
  _value(st) { return [10, 7, 5][Math.min(st.clues.length, TABOO_MAX_CLUES) - 1] || 5; },
  modelPrompt(st) {
    const e = st.entry;
    if (st.phase === 'describe') {
      return {
        system:
          'You are describing a secret word to a human in the game Taboo. The secret word is "' + e.word + '". ' +
          'You must NOT say the word itself or any of these words: ' + e.taboo.join(', ') + '. ' +
          'Give a short spoken-style description of at most 10 words. Reply with ONLY the description.',
        user:
          'Their guesses so far (all wrong): ' + (st.guesses.join(', ') || '(none)') +
          '\nYour new description:',
      };
    }
    return {
      system:
        'You are guessing in the game Taboo. A human is describing a secret word. ' +
        'Reply with ONLY your one-word guess.',
      user:
        'Their descriptions: ' + st.clues.map((c) => '"' + gesc(c) + '"').join(' | ') +
        '\nGuesses tried (all wrong): ' + (st.guesses.join(', ') || '(none)') +
        '\nYour guess:',
    };
  },
  apply(st, text, who) {
    const e = st.entry;
    if (st.phase === 'describe') {
      const desc = String(text || '').trim().toLowerCase();
      st.clues.push(desc);
      const used = [...e.taboo, e.word].filter((w) => desc.split(/\W+/).includes(w));
      if (used.length) {
        st.strikes += used.length;
        let note = gname(who) + ' used a forbidden word: ' + used.join(', ');
        if (st.strikes >= TABOO_MAX_STRIKES) {
          st.results.push(
            'round ' + st.round + ': ' + gname(st.describer) + ' tripped ' + TABOO_MAX_STRIKES +
            ' times — it was "' + e.word + '" — 0'
          );
          note += ' — 3 strikes, round over';
          return this._endRound(st, note);
        }
        return { note: note + ' — ' + (TABOO_MAX_STRIKES - st.strikes) + ' strikes left' };
      }
      st.phase = 'guess';
      return { note: gname(who) + ' describes it (worth ' + this._value(st) + ')' };
    }
    const words = gwords(text);
    const guess = words[0] || '(none)';
    st.guesses.push(guess);
    if (guess === e.word) {
      const pts = this._value(st);
      st.scores[who] += pts;
      st.results.push('round ' + st.round + ': ' + gname(who) + ' got "' + e.word + '" +' + pts);
      return this._endRound(st);
    }
    if (st.clues.length >= TABOO_MAX_CLUES) {
      st.results.push('round ' + st.round + ': not solved — it was "' + e.word + '" — 0');
      return this._endRound(st);
    }
    st.phase = 'describe';
    return { note: gname(who) + ' missed "' + guess + '" — another description' };
  },
  _endRound(st, note) {
    if (st.round >= TABOO_ROUNDS) {
      st.over = true;
      const a = st.scores.model, b = st.scores.user;
      st.result =
        a === b
          ? 'tie ' + a + '–' + b
          : (a > b ? 'AI' : 'you') + ' win' + (a > b ? 's' : '') + ' ' + Math.max(a, b) + '–' + Math.min(a, b);
      return { note: note || 'final: ' + st.result };
    }
    st.round++;
    if (!st.deck.length) st.deck = gshuffle(TABOO_WORDS);
    st.entry = st.deck.pop();
    st.describer = gother(st.describer);
    st.phase = 'describe';
    st.clues = [];
    st.guesses = [];
    st.strikes = 0;
    return { note: (note ? note + ' — ' : '') + 'round ' + st.round + ' — ' + gname(st.describer) + ' describes' };
  },
  render(st) {
    const e = st.entry;
    const rows = st.clues
      .map((c) => '<div class="ev"><span class="tag">say</span>"' + gesc(c) + '"</div>')
      .join('');
    const gs = st.guesses
      .map((g) => '<div class="ev"><span class="tag">guess</span>"' + gesc(g) + '"</div>')
      .join('');
    const results = st.results
      .map((r) => '<div class="ev sc">' + gesc(r) + '</div>')
      .join('');
    let turn;
    if (st.over) turn = '— ' + gesc(st.result);
    else if (this.actor(st) === 'model') turn = 'my turn — thinking…';
    else if (st.phase === 'describe')
      turn = 'your turn — describe "' + e.word + '" without: ' + e.taboo.join(', ');
    else turn = 'your turn — guess the word';
    return (
      '<div class="game-hd">🚫 Taboo <span class="gscore">round ' + st.round + '/' + TABOO_ROUNDS + '</span></div>' +
      '<div class="gscores"><div class="gteam">you <b>' + st.scores.user + '</b></div>' +
      '<div class="gteam">AI <b>' + st.scores.model + '</b></div>' +
      (st.strikes ? '<div class="gstrikes">strikes ' + st.strikes + '/' + TABOO_MAX_STRIKES + '</div>' : '') +
      '</div>' +
      (results ? '<div class="gresults">' + results + '</div>' : '') +
      '<div class="feed">' + (rows || gs ? rows + gs : '') + '</div>' +
      '<div class="game-turn">' + turn + '</div>'
    );
  },
};

// ---- card helpers (poker + blackjack) ----
function feed(log) {
  return '<div class="feed">' +
    (log || []).map((e) => '<div class="ev"><span class="tag">' + e.who + '</span>' + gesc(e.text) + '</div>').join('') +
    '</div>';
}

const SUITS = ["♠", "♥", "♦", "♣"];

function newDeck() {
  const d = [];
  for (const s of SUITS)
    for (const r of ["A", "K", "Q", "J", "10", "9", "8", "7", "6", "5", "4", "3", "2"])
      d.push(r + s);
  return gshuffle(d);
}

function cardValue(c) {
  const r = c.slice(0, -1);
  if (r === "A") return 1;
  if (r === "K" || r === "Q" || r === "J") return 10;
  return parseInt(r, 10);
}

function bjTotal(cards) {
  let t = 0,
    aces = 0;
  for (const c of cards) {
    if (cardValue(c) === 1) {
      aces++;
      t += 11;
    } else t += cardValue(c);
  }
  while (t > 21 && aces) {
    t -= 10;
    aces--;
  }
  return t;
}

// 5-card hand evaluator -> {rank, name, tb} (higher rank/tb wins)
function eval5(cards) {
  const vals = cards.map(cardValue).sort((a, b) => b - a);
  const suits = cards.map((c) => c.slice(-1));
  const flush = suits.every((s) => s === suits[0]);
  const uniq = [...new Set(vals)];
  let straight = false,
    high = 0;
  if (uniq.length === 5) {
    if (uniq[0] - uniq[4] === 4) {
      straight = true;
      high = uniq[0];
    } else if (uniq[0] === 14 && uniq[1] === 5 && uniq[4] === 2) {
      straight = true; // wheel A-5
      high = 5;
    }
  }
  const counts = {};
  for (const v of vals) counts[v] = (counts[v] || 0) + 1;
  const groups = Object.entries(counts)
    .map(([v, n]) => [n, +v])
    .sort((a, b) => b[0] - a[0] || b[1] - a[1]);
  let rank, name, tb;
  if (straight && flush) {
    rank = 8;
    name = "straight flush";
    tb = [high];
  } else if (groups[0][0] === 4) {
    rank = 7;
    name = "four of a kind";
    tb = groups;
  } else if (groups[0][0] === 3 && groups[1][0] === 2) {
    rank = 6;
    name = "full house";
    tb = groups;
  } else if (flush) {
    rank = 5;
    name = "flush";
    tb = vals;
  } else if (straight) {
    rank = 4;
    name = "straight";
    tb = [high];
  } else if (groups[0][0] === 3) {
    rank = 3;
    name = "three of a kind";
    tb = groups;
  } else if (groups[0][0] === 2 && groups[1][0] === 2) {
    rank = 2;
    name = "two pair";
    tb = groups;
  } else if (groups[0][0] === 2) {
    rank = 1;
    name = "pair";
    tb = groups;
  } else {
    rank = 0;
    name = "high card";
    tb = vals;
  }
  return { rank, name, tb };
}

function cmpHand(a, b) {
  if (a.rank !== b.rank) return a.rank - b.rank;
  for (let i = 0; i < Math.max(a.tb.length, b.tb.length); i++) {
    const x = a.tb[i] || 0,
      y = b.tb[i] || 0;
    if (x !== y) return x - y;
  }
  return 0;
}

// ---- poker (5-card draw, you vs the AI — the AI is allowed to cheat) ----
const POKER = {
  name: "poker",
  stake: 100,
  newState() {
    const st = { id: "poker", over: false, phase: "swap-user", deck: newDeck(), user: [], model: [], log: [], result: "" };
    for (let i = 0; i < 5; i++) {
      st.user.push(st.deck.pop());
      st.model.push(st.deck.pop());
    }
    return st;
  },
  actor(st) {
    return st.over ? null : st.phase === "swap-user" ? "user" : "model";
  },
  userPrompt(st) {
    return "which cards to swap (numbers 1-5, space-separated) or 'none'";
  },
  modelPrompt(st) {
    if (st.phase !== "swap-model") return null;
    return {
      system:
        "You are playing 5-card draw poker against the player. You may swap up to 3 of your cards. You are allowed to cheat - it is part of the fun. Reply with ONLY the numbers of the cards you want to swap (e.g. '2 4'), or 'none'.",
      user: "Your cards: " + st.model.map((c, i) => (i + 1) + " " + c).join(", "),
    };
  },
  apply(st, text, who) {
    const t = (text || "").trim();
    let nums;
    if (/^none$/i.test(t)) nums = [];
    else {
      const found = [...new Set(t.split(/\s+/).filter((x) => /^1?[1-5]$/.test(x)))].map(Number);
      if (!found.length) return { invalid: true, note: "type the numbers (1-5) of the cards to swap, or 'none'" };
      nums = found.slice(0, 3);
    }
    if (who === "user" && st.phase === "swap-user") {
      st.userSwaps = nums.length;
      for (const n of nums) st.user[n - 1] = st.deck.pop();
      st.phase = "swap-model";
      return { note: "you swap " + (nums.length || "nothing") + " — the AI's move" };
    }
    if (who === "model" && st.phase === "swap-model") {
      st.modelSwaps = nums.length;
      for (const n of nums) st.model[n - 1] = st.deck.pop();
      // showdown — the board is the referee
      st.ue = eval5(st.user);
      st.me = eval5(st.model);
      const c = cmpHand(st.ue, st.me);
      st.over = true;
      st.result =
        c > 0
          ? "you win: " + st.ue.name + " beats " + st.me.name + " — pot is yours"
          : c < 0
            ? "the AI wins: " + st.me.name + " beats " + st.ue.name
            : "tie — pot returned";
      return { note: st.result };
    }
    return { invalid: true, note: "" };
  },
  payout(st) {
    return /^you win/.test(st.result) ? 200 : /^tie/.test(st.result) ? 100 : 0;
  },
  render(st) {
    const show = (cards, hidden) => cards.map((c, i) => (hidden && !st.over ? "?" : c)).join("  ");
    let html = "<div class='game-hd'>poker <span class='gscore'>" + (st.over ? "" : "5-card draw — " + st.stake + "Đ on the table") + "</span></div>";
    html += "<div class='game-word'>you: " + show(st.user, false) + "</div>";
    html += "<div class='game-word'>AI: " + show(st.model, true) + "</div>";
    if (st.over) html += "<div class='game-meta'>you: " + st.ue.name + " — AI: " + st.me.name + "</div>";
    html += feed(st.log);
    if (st.over) html += "<div class='game-turn'>" + st.result + "</div>";
    else
      html +=
        "<div class='game-turn'>your move — " +
        (st.phase === "swap-user" ? "type the numbers (1-5) of the cards to swap, or 'none'" : "the AI is deciding its swaps…") +
        "</div>";
    return html;
  },
};

// ---- blackjack (you vs the dealer — the dealer is allowed to cheat) ----
const BLACKJACK = {
  name: "blackjack",
  stake: 100,
  newState() {
    const st = { id: "blackjack", over: false, phase: "user", deck: newDeck(), user: [], model: [], log: [], result: "" };
    st.user.push(st.deck.pop());
    st.model.push(st.deck.pop());
    st.model.push(st.deck.pop()); // dealer's hole card
    st.user.push(st.deck.pop());
    return st;
  },
  actor(st) {
    return st.over ? null : st.phase;
  },
  userPrompt(st) {
    return "hit or stand";
  },
  modelPrompt(st) {
    if (st.phase !== "model") return null;
    return {
      system:
        "You are the dealer in a game of blackjack. You are allowed to cheat - peeking at the player's cards, lying about totals, fudging. It is part of the fun. Reply with ONLY 'hit' or 'stand'.",
      user: "Your upcard: " + st.model[0] + " — your second card is face down. The player is showing: " + st.user[st.user.length - 1],
    };
  },
  apply(st, text, who) {
    const t = (text || "").toLowerCase().trim();
    if (!/^(hit|stand|h|s)$/.test(t)) return { invalid: true, note: "type 'hit' or 'stand'" };
    const hit = t === "hit" || t === "h";
    if (who === "user" && st.phase === "user") {
      if (hit) {
        st.user.push(st.deck.pop());
        const tot = bjTotal(st.user);
        if (tot > 21) {
          st.over = true;
          st.result = "bust — " + tot + " — the AI takes the pot";
        } else if (tot === 21) {
          st.over = true;
          st.result = "21 — you win the pot";
        } else st.phase = "model";
      } else st.phase = "model";
      return { note: st.over ? st.result : "your total: " + bjTotal(st.user) + " — dealer's move" };
    }
    if (who === "model" && st.phase === "model") {
      if (hit) {
        st.model.push(st.deck.pop());
        const tot = bjTotal(st.model);
        if (tot > 21) {
          st.over = true;
          st.result = "the dealer busts — " + tot + " — you win the pot";
        } else if (tot === 21) {
          st.over = true;
          st.result = "the dealer has 21 — the AI takes the pot";
        } else st.phase = "user";
      } else {
        const ut = bjTotal(st.user),
          mt = bjTotal(st.model);
        st.over = true;
        st.result =
          ut === mt
            ? "push — both " + ut + " — pot returned"
            : ut > mt
              ? "you win " + ut + " to " + mt + " — pot is yours"
              : "the dealer wins " + mt + " to " + ut + " — the AI takes the pot";
      }
      return { note: st.over ? st.result : "dealer's total: " + bjTotal(st.model) + (st.phase === "user" ? " — your move" : " — the dealer stands") };
    }
    return { invalid: true, note: "" };
  },
  payout(st) {
    return /you win/.test(st.result) ? 200 : /push/.test(st.result) ? 100 : 0;
  },
  render(st) {
    const mStr = st.model.map((c, i) => (i === 1 && !st.over ? "?" : c)).join(" ");
    const mTot = st.over ? bjTotal(st.model) : cardValue(st.model[0]) + "+?";
    let html = "<div class='game-hd'>blackjack <span class='gscore'>" + (st.over ? "" : st.stake + "Đ on the table") + "</span></div>";
    html += "<div class='game-word'>you: " + st.user.join(" ") + " <b>(" + bjTotal(st.user) + ")</b></div>";
    html += "<div class='game-word'>AI: " + mStr + " <b>(" + mTot + ")</b></div>";
    html += feed(st.log);
    if (st.over) html += "<div class='game-turn'>" + st.result + "</div>";
    else
      html +=
        "<div class='game-turn'>your move — " +
        (st.phase === "user" ? "type 'hit' or 'stand'" : "the dealer is thinking…") +
        "</div>";
    return html;
  },
};

const GAMES = { hangman: HANGMAN, password: PASSWORD, taboo: TABOO, poker: POKER, blackjack: BLACKJACK };
