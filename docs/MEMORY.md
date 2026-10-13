# Memory

Two tiers. Keeping them separate is the whole trick.

**Journal**: an append-only log of every side effect (message sent, file
written, draft created). Written automatically by the scripts. It is your black
box: read it first when something looks wrong.

**Memory**: a few curated files.

```
memory/MEMORY.md        index, one line per memory, loaded every session
memory/<topic>.md       one fact per file
```

A memory file looks like this:

```markdown
---
name: reply-on-the-channel-he-wrote-on
description: a terminal-only answer reads as silence when he texts from the phone
type: feedback            # user | feedback | project | reference
---

The fact. Then **Why:** and **How to apply:** lines.
```

## What to save

- Preferences the owner states ("short messages", "plain language").
- Corrections ("don't do X again") and confirmed approaches, with the reason.
- Decisions, deadlines and anything that is not obvious from the code.
- Where to find things (a folder, a dashboard, a ticket).

## What not to save

- Anything the repo already records (code structure, git history).
- Anything that only matters to the current conversation.
- Secrets.

The test: "would a fresh session, with no memory of today, break something or
redo work without this?" If not, do not save it.

## Keeping it honest

- **Update, do not duplicate.** Look for an existing file first.
- **Delete a memory the moment it is proven wrong.** A confident wrong memory is
  more dangerous than none.
- **Verify before acting on an old memory.** A file name, a flag or a number may
  have changed since it was written.
- Date every entry (DD.MM.YYYY) so it is clear how old it is.
