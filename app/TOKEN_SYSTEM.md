# How Khayyam spends tokens now

## Why "hi" was slow and the limit vanished (measured)
- 17 database statements per message (3 of them table-creation DDL on every request) -> now 3.
- `tiktoken` downloaded its vocabulary from the internet on first use per worker -> removed (local estimate).
- A second paid model call for memory extraction -> now rule-based, zero tokens.
- A failed provider call was charged in full against the user's limit -> now refunded.
- The fixed system prompt was charged to the user's limit on every message -> now excluded
  (still counted in OUR dollar guard).
- 1-worker gunicorn blocked every user behind one slow AI call -> 2 workers x 4 threads.
- Every message carried all poetry, memory and cultural rules; now a compact core plus only what is needed.

## Pipeline per message (router.py, no model call)
1. classify: canned | simple | standard | complex (+ kind: poetry/code/math/longform/document/image)
2. canned (pure greetings, thanks, goodbye): answered locally, 0 tokens, instant
3. prompt = core + Afghan glossary + relevant cultural notes/examples + memory notes
4. history = last 6 messages, long replies shortened
5. model order per intent: simple/standard -> mini first; complex -> best model first
   (tunable in `router.ROUTING` / `router.LIMITS`)
6. reserve (token window + dollar ceiling) -> call -> settle with the provider's real numbers
7. error -> refund and try the next affordable model; timeout -> charged conservatively

## Reading the numbers
Admin panel > "مصرف هوش مصنوعی" (or `/api/admin/ai-log`): per request model, intent, tokens charged to the user,
the fixed-prompt share, real USD cost, latency, and status (failed = refunded).

## Known follow-ups
- Streaming replies (words appear as generated): needs the chat page redesign (Day 3-4).
- OpenAI's docs list gpt-5.4-nano as deprecated; check your account before relying on it.
- Tutor Pro has only the full model; a mini tier would cut its cost for simple turns.
- Rate limits are per worker unless RATELIMIT_STORAGE_URI points to Redis.
