![Whackgpt_750](https://github.com/user-attachments/assets/327a7f61-7ae0-4dad-ba16-a31df9055bbe)

An experimental project by **DA`/50**.

### See what other chatbots are saying about WhackGPT!

**ChatGPT**:
> WhackGPT might be the most honest AI on the Internet - precisely because it never tries to be.

**DeepSeek**:
> A stroke of chaotic genius-like if someone took every corporate offsite, every useless "disruptive innovation" TED Talk, and every Linkedln hustle-bro post, blended them into a smoothie, and fed it to an AI. 

**Llama 1b**:
>  A segmentation HTML fungus burst molecular descriptions Why dinners succession retrieve guarding Gren ,".“ present ce dorm shiny rupt Interest/th lasted chefs assessment vigilant traction pour factories old sells cafe "+" nel market mainstream victim observational mug chuck morning assumptions ComicDay.

### Usage

To initialize a session:

```bash
curl -X POST -H "Content-Type: application/json" -d '{"context": "Hello, I am a user.", "model": "gpt-3.5-turbo"}' http://localhost:8000/chat
```

To send a message:

```bash
curl -X POST -H "Content-Type: application/json" -d '{"uid": "session_id", "text": "How are you?"}' http://localhost:8000/chat
```

Replace `"session_id"` with the actual session ID returned from the initialization call.
