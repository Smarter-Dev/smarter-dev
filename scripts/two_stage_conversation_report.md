# Two-stage vs single-stage — cost over a 20-turn conversation

- **Conversation:** `build-a-habit-cli` (20 turns)
- **Two-stage:** worker **Gemini 3.5 Flash Lite** (`gemini-3.5-flash-lite`) + writer **GPT-5.6 Terra** (`gpt-5.6-terra`)
- **Single-stage:** **GPT-5.6 Terra** (`gpt-5.6-terra`) as an ordinary chat bot
- **Generated:** 2026-07-24T21:04:09+00:00

## Bottom line

| Approach | In | Out | Cache read | Cache write | Cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Worker · Gemini 3.5 Flash Lite | 171598 | 6478 | 0 | 0 | $0.06767 |
| Writer · GPT-5.6 Terra | 12665 | 4850 | 0 | 0 | $0.10441 |
| **Two-stage total** |  |  |  |  | **$0.17209** |
| Single-stage · GPT-5.6 Terra | 359481 | 11366 | 338295 | 0 | **$0.30803** |

**Two-stage costs $0.17209 vs single-stage $0.30803** — a difference of $0.13594 (1.79× cheaper) over 20 turns.

Extrapolated to 1,000 such conversations: two-stage **$172.08690** vs single-stage **$308.02875** (saves $135.94185).

## Two-stage — per turn

**Worker (Gemini 3.5 Flash Lite)**

| Turn | In | Out | Cache read | Cache write |
| --- | ---: | ---: | ---: | ---: |
| 1 | 3615 | 597 | 0 | 0 |
| 2 | 4387 | 405 | 0 | 0 |
| 3 | 4975 | 413 | 0 | 0 |
| 4 | 5573 | 307 | 0 | 0 |
| 5 | 6063 | 278 | 0 | 0 |
| 6 | 6523 | 279 | 0 | 0 |
| 7 | 6982 | 290 | 0 | 0 |
| 8 | 7452 | 271 | 0 | 0 |
| 9 | 7900 | 268 | 0 | 0 |
| 10 | 8348 | 286 | 0 | 0 |
| 11 | 8814 | 291 | 0 | 0 |
| 12 | 9283 | 294 | 0 | 0 |
| 13 | 9763 | 320 | 0 | 0 |
| 14 | 10264 | 288 | 0 | 0 |
| 15 | 10737 | 287 | 0 | 0 |
| 16 | 11205 | 318 | 0 | 0 |
| 17 | 11705 | 302 | 0 | 0 |
| 18 | 12185 | 295 | 0 | 0 |
| 19 | 12661 | 326 | 0 | 0 |
| 20 | 13163 | 363 | 0 | 0 |

**Writer (GPT-5.6 Terra)**

| Turn | In | Out | Cache read | Cache write |
| --- | ---: | ---: | ---: | ---: |
| 1 | 623 | 334 | 0 | 0 |
| 2 | 617 | 266 | 0 | 0 |
| 3 | 631 | 263 | 0 | 0 |
| 4 | 646 | 298 | 0 | 0 |
| 5 | 634 | 259 | 0 | 0 |
| 6 | 624 | 236 | 0 | 0 |
| 7 | 624 | 129 | 0 | 0 |
| 8 | 631 | 184 | 0 | 0 |
| 9 | 623 | 142 | 0 | 0 |
| 10 | 628 | 170 | 0 | 0 |
| 11 | 641 | 326 | 0 | 0 |
| 12 | 634 | 296 | 0 | 0 |
| 13 | 646 | 188 | 0 | 0 |
| 14 | 632 | 148 | 0 | 0 |
| 15 | 644 | 337 | 0 | 0 |
| 16 | 634 | 274 | 0 | 0 |
| 17 | 635 | 397 | 0 | 0 |
| 18 | 631 | 133 | 0 | 0 |
| 19 | 633 | 263 | 0 | 0 |
| 20 | 654 | 207 | 0 | 0 |

## Single-stage — per turn

**GPT-5.6 Terra**

| Turn | In | Out | Cache read | Cache write |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2392 | 382 | 0 | 0 |
| 2 | 6005 | 475 | 5065 | 0 |
| 3 | 7351 | 529 | 6435 | 0 |
| 4 | 8840 | 574 | 7823 | 0 |
| 5 | 10250 | 460 | 9375 | 0 |
| 6 | 11586 | 484 | 10638 | 0 |
| 7 | 12870 | 495 | 12007 | 0 |
| 8 | 14227 | 415 | 13288 | 0 |
| 9 | 15401 | 435 | 14580 | 0 |
| 10 | 16666 | 432 | 15773 | 0 |
| 11 | 17917 | 603 | 17035 | 0 |
| 12 | 19575 | 768 | 18460 | 0 |
| 13 | 21330 | 421 | 20275 | 0 |
| 14 | 22508 | 636 | 21676 | 0 |
| 15 | 24292 | 874 | 23074 | 0 |
| 16 | 26329 | 638 | 25091 | 0 |
| 17 | 27895 | 821 | 26950 | 0 |
| 18 | 29920 | 382 | 28643 | 0 |
| 19 | 31140 | 809 | 30236 | 0 |
| 20 | 32987 | 733 | 31871 | 0 |

## Conversation

1. @smarterbot I want to build a small CLI in Python to track my daily habits. Where should I start?
2. what should I use to parse the command-line arguments?
3. ok argparse it is. how do I set up subcommands for 'add' and 'list'?
4. for the 'add' subcommand I want it to take a habit name and an optional note. show me.
5. where should I store the data? I don't want to run a whole database for this.
6. JSON's fine. how do I load it when the file doesn't exist yet?
7. make that load function return an empty list if the file is missing instead of crashing.
8. now write the matching save function that writes the list back to that same JSON file.
9. how do I timestamp each entry at the moment it's added?
10. use UTC, not local time — does that change the add code you showed me?
11. the 'list' subcommand should print each habit with its timestamp. format it nicely.
12. can you make those timestamps human-readable, like '2 hours ago'?
13. I'm getting a KeyError on 'note' for older entries that never had one. how do I fix that?
14. right, .get with a default. update the list formatter you wrote to use that.
15. how would I add a 'streak' count — how many days in a row I logged a given habit?
16. that streak logic looks dense. can you walk me through just the date-math part?
17. let's make the whole thing installable as a real command. what do I need?
18. add a console entry point in pyproject.toml called 'habits'.
19. last code question — how do I write a quick test for that load function from earlier?
20. thanks! can you summarize everything we built as a short README?
