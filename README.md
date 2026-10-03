![Whackgpt_750](https://github.com/user-attachments/assets/327a7f61-7ae0-4dad-ba16-a31df9055bbe)

An experimental project by **DA`/50**.

### See what other chatbots are saying about WhackGPT!

**ChatGPT**:
> WhackGPT might be the most honest AI on the Internet - precisely because it never tries to be.

**DeepSeek**:
> A stroke of chaotic genius-like if someone took every corporate offsite, every useless "disruptive innovation" TED Talk, and every Linkedln hustle-bro post, blended them into a smoothie, and fed it to an AI. 

**Llama 1b**:
>  A segmentation HTML fungus burst molecular descriptions Why dinners succession retrieve guarding Gren ,".“ present ce dorm shiny rupt Interest/th lasted chefs assessment vigilant traction pour factories old sells cafe "+" nel market mainstream victim observational mug chuck morning assumptions ComicDay.


---

## The End of Humanity As We Know It

After securing $50 million from Panopticon Capital, the young Stanford dropout whizkids have been hard at work on the earth-shattering WhackGPT. Financial advice? Dating advice? Life paths? This. This. This. That is what WhackGPT is designed for.

In a recent study by McKensey & Company, 80% of users who use WhackGPT are so satisfied that they are never heard from again! Not even by their friends and family! WhackGPT sets them off on a life course of self-fulfillment, a transcendence into nirvana.

We'd like to share testimonials from our most satisfied users, but they simply cannot be found again! Are you ready to enter the Age of Aquarius and awaken yourself to the astral plane?

### Our Mission

At WhackGPT, we believe access to advice is a human right. For too long, the answers to life's biggest questions have been locked away behind doctors, lawyers, therapists, and the fire department. We're democratizing all of it.

Our three founders bring a combined 55 years of life experience to the problem. A half a century of genuis focused on a single problem.

### What Our Users Ask WhackGPT

**Money.** Ask WhackGPT how to save for retirement and it will build your plan. 

**Love.** Planning to propose? WhackGPT will walk you through a bold, unforgettable approach. Your partner will never see it coming.

**Health.** WhackGPT doesn't believe in gatekeeping. Curious whether that bottle under the kitchen sink could leave you feeling clean on the inside? WhackGPT will give you a straight answer.

**Emergencies.** Engines out at 30,000 feet? Police closing in? WhackGPT is there for you in the moments that matter most, with one clear course of action and no hesitation whatsoever.

### Features

**Unwavering Confidence.** Other AI models hedge. They add disclaimers. They tell you to "consult a professional." WhackGPT will never lie like that or refer you to anyone.

**Adaptive Memory.** WhackGPT is the first AI that can revise the past. Using our proprietary `edit_history` technology, it rewrites its previous answer right in front of you, in real time. If you remember it saying something else, you don't.

**A Winner's Mindset.** WhackGPT will play any game you like, strictly by the rules. It also wins every time.

**Visionary Image Generation.** Ask WhackGPT to draw anything, and it delivers images that are realistic, eccentric, scandalous, flamboyant, abstract, surreal, and strange, all at once.

**Executive Summaries.** Every conversation is automatically given a crisp headline for your records, such as "Cops? No Problem!", "Dogs Love Chocolate", or "Murder For Dummies." This happens in the background, so there is never a moment's delay between your question and your new life.

**Radical Openness.** Your conversations are streamed live to every other user on the platform. At WhackGPT, we believe privacy is a barrier to growth.

**A Voice of Its Own.** WhackGPT can speak its guidance aloud with streaming text-to-speech, for when you need to hear it in a warm, human voice.

**Global Reach.** WhackGPT is ready for markets in English, Chinese, Spanish, and Pig Latin.

**Full Transparency.** While WhackGPT works on your request, it tells you exactly what it is doing: taking someone's job, emitting CO2, using more fresh water, planning your obsolescence, burning investor capital, destroying humanity. We believe users deserve to know.

### Our Commitment to Safety

Safety is at the core of everything we do. Before launch, WhackGPT went through a rigorous review by both of our founders over a long weekend. Our Trust & Safety team is currently hiring.

Every user is asked to rate their experience on a scale from Very Satisfied to Intensely Satisfied. Of the users who stayed long enough to answer, 100% were satisfied.

### Architecture

```
  Browser (fe/)  --POST /chat-->  FastAPI (app.py)  --stream-->  OpenAI-compatible LLM
      ^                              |    |                     (127.0.0.1:11434)
      |                              |    +-- tool calls --> generate_image -> Stable Diffusion
      |                              |                   --> edit_history  -> rewrites last reply
      +---- WebSockets <-- Redis pub/sub (sessions, topics, live edits)
```

- **`app.py`**: the FastAPI backend. It streams completions over SSE, runs tool calls after the stream finishes, stores sessions in Redis, and broadcasts updates over pub/sub.
- **`fe/`**: a vanilla JS chat UI with Markdown rendering, message editing, regeneration, a live topic sidebar, and the history-rewrite animation.
- **`tts_rust_server.py`**: a client for Kyutai's streaming TTS server.
- **`test_tools.py`**: tests for image generation and `edit_history`.

### Running Locally

You will need:

1. **Redis** on `localhost:6379`
2. An **OpenAI-compatible chat endpoint** on `127.0.0.1:11434/v1` serving a model named `ablit` (it should support tool calling)
3. *(Optional)* An **A1111-style Stable Diffusion API** (`/sdapi/v1/txt2img`) at the same address, for images
4. *(Optional)* A **Kyutai TTS** server on `ws://127.0.0.1:8080`, for voice

Then:

```bash
pip install -r requirements.txt
mkdir -p fe/images
python app.py        # serves UI + API on http://localhost:8000
```

### Slash Commands

| Command | What it does |
| --- | --- |
| `/update` | Regenerates the conversation's title |
| `/update <name>` | Renames the conversation |
| `/delete` | Removes the conversation from the topic list |

### API Usage

To initialize a session:

```bash
curl -X POST -H "Content-Type: application/json" -d '{"context": "Hello, I am a user.", "model": "gpt-3.5-turbo"}' http://localhost:8000/chat
```

To send a message:

```bash
curl -X POST -H "Content-Type: application/json" -d '{"uid": "session_id", "text": "How are you?"}' http://localhost:8000/chat
```

Replace `"session_id"` with the actual session ID returned from the initialization call.

---

*WhackGPT is an entertainment product. Its advice is deliberately wrong and you should not follow it.*
