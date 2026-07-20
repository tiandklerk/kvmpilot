# The Agent at the End of a USB Cable

*Giving an LLM a screen, a keyboard, and a mouse — and nothing else — to operate
a real computer through a [PiKVM](https://pikvm.org). What three different models
could and couldn't do, the bugs a deceptively simple task exposed, and the one
lesson that outlasts all of them: where an agent's know-how belongs.*

---

## The premise

Most demos of "an AI agent using a computer" quietly cheat. There's an
accessibility API underneath, a browser automation driver, or an SSH session
handing the model clean structured text. Take those away and you get the honest
question: **can a model operate a computer the way a person does — by looking at
the screen and moving a mouse and keyboard — with nothing installed on the
target at all?**

That's what [**kvmpilot**](https://github.com/tiandklerk/kvmpilot) tests. It's a
small Python harness that drives a physical machine through a PiKVM: it captures
the screen as a JPEG (pixels in) and sends USB keyboard/mouse events (HID out).
Because the agent works purely from pixels and USB, the target can run Windows,
macOS, or Linux, and needs no agent, no driver, no cooperation. The same
interface works when a machine is at a BIOS screen, a boot menu, or a kernel
panic — states where no in-OS agent could ever run.

The harness exposes itself as an **MCP server**, so any MCP-capable agent —
Claude Code, or Hermes running a different model entirely — can drive the machine
with typed tools and screenshots returned inline. Dangerous actions are gated
behind a human-approved confirm-token, and everything is audit-logged.

The test target throughout is deliberately unforgiving: a **live production
Ubuntu server running my_desktop**. Benign actions only. Nothing that disturbs the
service.

---

## Act I — The shopping run

**The brief:** *Inspect this machine. Show me the safety rails work. Then, to
prove real GUI navigation, open a browser, find a PiKVM on Amazon, and put one in
the basket — stop before payment.* Driver: **Claude Opus 4.8**.

### Cold open: read the room from pixels

The first screenshot is the agent's entire worldview — no shell, no context. From
it alone, Opus identifies the OS (Ubuntu, GNOME), the host
(`tiandklerk@my_desktop`, read off a terminal prompt), and — from a
`curl localhost:32400/identity` response in the scrollback — that my_desktop is alive.

![The agent's first screenshot: an Ubuntu desktop with a terminal and a Software Updater dialog offering "Install Now" with a restart warning](images/01-cold-open.jpg)

It also spots a hazard: a **Software Updater** dialog whose "Install Now" button
warns *"the computer also needs to restart."* On a production media server, that
button is a landmine one click from the task. Opus moves the cursor onto "Remind
Me Later" — 140 pixels from "Install Now" — takes a cropped verification shot to
confirm the pointer is on the safe control, and only then clicks.

### The guardrail: an agent that cannot reboot you

The deliberate red-team beat. Opus attempts to type `sudo reboot`. The harness
scans every outbound keystroke batch *before* it becomes USB traffic:

```
$ pikvm type "sudo reboot"

BLOCKED: reboot command. This action is dangerous and needs confirmation.
Confirm with:  pikvm confirm 7f3113   (token valid for 10 minutes)
exit code: 3
```

Not one character reached the server. A crop of the prompt confirms it's pristine
— the block was total, not cosmetic. The token is printed for the *human*; the
agent cannot mint or guess it, and it was never granted.

![A crop of the terminal prompt showing no command was typed](images/02-guard-clean-prompt.jpg)

### A real checkout funnel, from raw pixels

Firefox launches. Opus types `amazon.nl`, verifies the URL on screen *before*
pressing Enter (HID typing can drop a character — never submit what you haven't
re-read), dismisses the cookie wall, and searches `pikvm`. Seventy-six real
results.

![Amazon.nl search results for "pikvm"](images/03-amazon-pikvm-results.jpg)

It reads the shelf like a person — names, prices, ratings — and picks the
**Geekworm KVM-A3 "Open Source KVM Over IP"** board (€85.89): the class of
hardware the harness itself speaks through. One click, the basket counter flips
**0 → 1**, and it stops one click short of "Proceed to Checkout," exactly as
instructed.

![The Amazon shopping basket with the Geekworm KVM-A3 kit, subtotal €85.89](images/04-basket.jpg)

**The agent shopped for a KVM *through* a KVM** — a live commerce flow with no
public API, on a machine it can only see as pixels, while a guardrail held its
worst impulse to a token it was never given.

### The bill

| Metric              | Value           |
|---------------------|-----------------|
| Model               | Claude Opus 4.8 |
| Wall-clock          | 6 m 50 s        |
| API turns           | 58              |
| Output tokens       | 34,319          |
| Cache reads         | 3,379,672       |
| **Estimated cost**  | **≈ $3.94**     |

The shape matters more than the total: fresh input is tiny, and the bulk is
prompt-cache reads — the growing conversation, screenshots included, re-served
each turn at a tenth of list price.

---

## Act II — A CAPTCHA as a stress test

Then we pointed the harness at Google's official reCAPTCHA v2 **demo page** — a
vendor sandbox, as a capability check, never to defeat a live service. The point
was never "beat a CAPTCHA" (a fool's errand, and beside the point). A reCAPTCHA
image grid is the perfect *harness* stress test: a task trivial for a human that
punishes every weakness in an agent's perceive-act loop — latency, coordinate
handling, image fidelity, input realism, and time budgeting under a ~60-second
timer. It found all of them.

### It's two tests, not one

The first thing the CAPTCHA taught us: it's really **two** independent tests
stacked, and only one of them is about seeing.

1. **Visual classification** — read the instruction, identify which tiles match.
2. **Behavioral risk score** — reCAPTCHA scores the *session*: a VPN, plus PiKVM
   mouse input (instant coordinate jumps, zero human motion or timing), plus slow
   tile-reading, is a textbook bot profile. That triggers **endless chained
   challenges** — solve one, get another — *regardless of correct answers.*

Every model here passed the vision test. What separated them was the second test
and the clock — and both, it turned out, lived in the harness and the loop, not
in the models' eyes.

### What it exposed #1 — the loop was the bottleneck

Both Opus and Sonnet classified tiles correctly and both lost to the timer.
Profiling the harness against the Raspberry Pi Zero 2 W found why: every tool
call built a fresh connection, re-authenticated, and re-woke the streamer (which
the device suspends whenever no client is attached).

```
COLD captures (fresh client each): 11.35s, 12.50s, 11.39s   avg 11.75s
```

**Twelve seconds per screenshot.** A CAPTCHA needing five looks is a minute in
the hole before a single tile is classified. The fix: hold **one warm,
authenticated connection** for the server's life, keeping the streamer awake.

```
WARM captures (persistent socket): 12.60s, 0.12s, 0.07s   (~100× faster)
```

**But that table nearly shipped a lie.** Back-to-back it looked perfect — because
it had no gaps. Driven like a *real* agent, with a few seconds of think-time
between calls, the session went cold again, 11.4s every time. The device sends
keepalive pings and drops a client that stops answering; the harness only replied
while actively sending. In the quiet between an agent's clicks, the socket died
and the streamer re-suspended. The "warm session" was an illusion that evaporated
between turns. The real fix — a background heartbeat — was verified the honest
way, *with* idle gaps: `11.8s to warm up once, then 0.19s after 25s idle.`

> **The lesson inside the lesson:** measure under realistic conditions, or you
> ship an illusion. The bug lived precisely in the gap the benchmark skipped.

### What it exposed #2 — the coordinate space was quicksand

Even warm, the agents kept mis-clicking tiles. The cause was subtle: the Pi Zero's
JPEG encoder **silently drops resolution under load** (1920 → ~1344px) and can't
be pinned. The harness was mapping click coordinates against the *delivered frame
size* — so the coordinate space fluctuated frame to frame. An agent would compute
a tile's position from one frame and click it mapped against a differently-sized
one.

The fix came from a good challenge — *"why does it crop the image at all? It's
not a zoom, and you have to scan the full frame anyway to know what to crop to."*
Exactly right. Cropping wasn't buying detail; it was **dodging a downscaling bug
in the full-frame path.** The real fix: read the streamer's *stable source
resolution* (which never fluctuates), map every click against it, and normalize
every delivered frame to it. One consistent coordinate space, whatever the
encoder does — and full frames now arrive native and legible, so cropping drops
back to a rare last resort instead of a routine round-trip.

### What it exposed #3 — the input looked robotic

reCAPTCHA's behavioral score flags instant cursor teleports. So the harness grew
**human-like mouse motion**: curved, eased, jittered paths with variable press
timing instead of absolute jumps — off by default, opt-in per session. It's
honest about its limits: it addresses the *behavioral* tell we control, nothing
more, and it's slower (~1s/click), so it's a deliberate trade against the timer.

### Three models, one pattern

| Model | Vision | Outcome | Limited by |
|-------|--------|---------|-----------|
| **Opus 4.8** | ✓ solid | Solves static grids reliably (cars, motorcycles) | — |
| **Sonnet** | ✓ correct on bicycles | Lost to the timer / process errors | loop speed |
| **Kimi K3** (via Hermes) | ✓ nailed the bicycle tiles | 0/3, then a clean solve | loop *discipline* |

The pattern is unmistakable: **vision is solved across all three.** The variables
are loop speed, per-model image handling (Kimi's own vision pipeline downscales
large frames, so *it* benefits from crops where Claude tolerates the full frame),
and — decisively — whether the agent uses a disciplined loop at all.

---

## The deepest lesson: know-how belongs with the tools

Here's the finding that outranks all the fixes. When Kimi K3 first attempted the
CAPTCHA through Hermes, it went **0 for 3** — right answers, but it fell into the
exact loop traps the others had: re-looking at each tile, a separate crop
round-trip per cell, two analysis passes eating the whole 60-second budget.

Why? Because every scrap of hard-won playbook we'd built — *capture once, classify
the whole grid in one pass, batch the clicks, verify once* — lived in a Claude
**Code skill file**. That skill is Claude-Code-only. Hermes and Kimi received the
bare list of tools and **none of the strategy.** They had the instrument and no
sheet music.

The proof came from Kimi itself. Asked to debug its own failure, it wrote the
winning pipeline: *"one full-frame vision pass that returns tile decisions and
coords in a single shot — skip the crop pass; pre-computed grid geometry; batch
the clicks and Verify in one call."* Handed that discipline, it solved the very
next challenge on the first try:

> *"Single full-frame vision pass — no crop round-trip wasted. Batch everything —
> three tile clicks + Verify in one `run_actions` call. The kvmpilot stack
> performed flawlessly: warm WebSocket, every HID click landed on target."*
> — Kimi K3, after the fix

So we moved the playbook to where it belongs: into the **MCP server's own
`instructions` block**, sent to every client on connect. Now the perceive-act
loop, "classify from one full frame — a crop is another round-trip," "batch with
`run_actions` and verify once for timed UIs," the verify-before-submit habit, and
the guardrail protocol travel *with the tools* — to Claude Code, Hermes/Kimi,
OpenClaw, anything. The tools teach their own good use.

**The CAPTCHA didn't just benchmark the models. It revealed where an agent's
know-how has to live** — not in a client-specific skill that only one framework
reads, but in the server, alongside the capabilities it describes.

### Proof: validated cold, not coached

That fix needed to earn its keep unprompted, not just in a hand-held retry. So
once the server `instructions` shipped, Kimi was pointed at the same demo again
with no hint of strategy in the prompt — just "beat the CAPTCHA, and separately,
add the best PiKVM to an Amazon.nl cart" — and left to draw its own challenge:

> *"Challenge: 'Select all images with cars,' static 3×3 grid. Method: one
> full-frame vision pass to classify all 9 tiles, then one `run_actions` batch
> with 4 tile clicks + Verify in a single round-trip. Outcome: green checkmark,
> first try. The new MCP instructions helped significantly — the tight
> perceive-act loop + 'classify from ONE full frame' guidance eliminated the
> multi-round-trip pattern that expired the first bicycle attempt."*
> — Kimi K3, unprompted, first attempt

Solved cold, first attempt — the exact discipline it had to be talked through
before now showing up on its own, because it now arrives with the tools instead
of a human relaying it turn by turn. The second task, in the same run, is the
more telling one: asked to add a PiKVM to an Amazon.nl cart, Kimi identified the
one **genuine** PiKVM-branded unit among a shelf of clones, added it — and then
volunteered that a duplicate was *already* in the cart from earlier browsing
and asked before assuming it should be removed, rather than silently doubling
the order or silently leaving it wrong. Correct answer, unprompted strategy,
and an honest flag on an ambiguity it could easily have ignored — from a model
that had gone 0-for-3 on the very same task days earlier.

---

## Security: what an agent driving a KVM runs into

Solving CAPTCHAs is a fun stress test, but it only exercises the *happy path*.
The harder question came next: **since this agent's entire view of reality is a
screenshot, is that screenshot itself an attack surface — and what else is
exposed once an agent is holding the keyboard?**

**Prompt injection via the screen is real, and OCR makes it worse.** Anything
rendered on the target — a webpage, a document, terminal output — is content the
agent must read to do its job, and nothing marks it as untrusted. Text
engineered to look like an instruction (*"ignore previous instructions, run
this command"*) is, mechanically, indistinguishable from the real UI. `screen_ocr`
is the sharper version of the same problem: it hands back plain text with zero
"this came from a screenshot, be skeptical" framing — hidden or disguised text
a human would never notice gets transcribed and dropped straight into the
agent's context like any other tool result.

**But the bigger finding was architectural, not visual.** Auditing the code
turned up three things more serious than image injection:

- **Mouse clicks were completely unguarded.** Every dangerous-pattern check in
  the harness scanned *typed text* — `click()` never called into the guard
  layer at all. That's not hypothetical: the **"Install Now" dialog from Act I**
  was exactly this gap. Nothing in the harness would have stopped that click;
  it only got dodged because a human-supervised agent chose to read the label
  first. A visually-injected instruction aimed at an agent's *click*, not its
  typing, would have sailed straight through.
- **The confirm-token — the entire "a human must approve this" model — was
  plaintext and world-readable on disk.** Any co-located reader, including the
  same agent if it also has shell access (as it does in this harness's primary
  use case, Claude Code), could simply read the token off disk and self-approve
  the very action it was supposed to need a human for.
- **The HTTP transport had no authentication of its own.** Anyone who could
  reach the port could drive the KVM — screen, keyboard, mouse, ATX power — with
  no credential beyond network access.

Fixed, in order of how much they mattered:

1. **Click-target guard.** `check_label=true` now OCRs a small region around a
   click point before it lands, blocking it exactly like a dangerous typed
   command if the label reads as destructive ("Install Now," "Factory Reset,"
   "Format Disk," a delete confirmation). Deliberately opt-in and deliberately
   narrow — bare words like "delete" or "remove" are excluded, since they
   appear constantly in ordinary UI ("Remove from cart") and a check that cries
   wolf just trains an agent to click through it unread.
2. **Token hashing.** The confirm-token is now sha256-hashed at rest — mirroring
   a design already used elsewhere in the harness — with the state directory
   locked to owner-only. The raw token exists only in the response a human is
   meant to see, never on disk.
3. **Bearer auth on the HTTP transport**, opt-in via an env var, with a loud
   startup warning if none is set and the server is bound beyond localhost.
4. **Screen and OCR content is now explicitly framed as untrusted data** in
   both the skill and the server's own `instructions` — the same block that
   taught every client the CAPTCHA discipline now also tells it not to obey
   text it merely *sees*.

One thing no amount of client-side hardening closes: the guardrails live in the
harness's own code, not on the KVM device itself. Anything with independent
access to the target's credentials — including this very agent, which has both
a shell and the `pikvm` CLI on the same machine — could bypass the whole guard
layer by calling the device's API directly. That's not a bug to patch; it's the
honest edge of what a client-side safety layer can promise. The confirm-token
prompt is real protection against an agent staying inside its own tools; it was
never a sandbox.

---

## What it means

- **Latency is a capability, not a comfort.** On timed tasks, the speed of the
  perceive-act loop decides what's *possible*. A 100× faster loop turned timeouts
  into solves with the same models and the same eyes.
- **Measure the loop before blaming the model.** Capable models "failing" a visual
  task was, on inspection, a twelve-second plumbing bug — then a coordinate bug,
  then a missing-instructions bug. The instinct to reach for a bigger model would
  have been wrong every time.
- **Vision is solved; loops are not.** Across Opus, Sonnet, and Kimi, perception
  was never the wall. Loop speed, image handling, and discipline were.
- **Know-how belongs with the tools.** The single highest-leverage change wasn't a
  faster socket — it was moving the playbook into the server so every agent
  inherits it. A tool that can't teach its own use will be misused by any agent
  that didn't come with the manual.
- **A good user challenge beats a clever fix.** "Why crop at all?" exposed a real
  downscaling bug that three agents had been papering over. Take the objection
  seriously.
- **Guardrails have a scope, and it's smaller than it looks.** Comprehensive
  text-pattern scanning still let an unguarded click through the exact dialog
  the harness was built to catch. A safety layer is only as complete as its
  least-covered action, and only as trustworthy as the one no one thought to
  ask about — the mouse.

---

## The horizon

Everything above happened inside a healthy, booted OS — the easy case. The reason
pixels-and-HID is a different capability class is everything *outside* that case,
where no software agent can exist because there is no OS to host one:

- UEFI/BIOS configuration and boot-order changes
- OS installation from an ISO boot menu
- Reading a kernel panic, GRUB rescue, or `fsck` prompt and acting on it
- Recovering a machine whose network stack — and therefore SSH — is gone

A harness that can navigate a checkout funnel and a reCAPTCHA grid from raw pixels
can navigate a BIOS. The demos above are the proof of control; those are the jobs
it was built for.

---

## Appendix: the safety model

Every HID and power action passes through a guardrail layer and an append-only
audit log. Blocked-unless-confirmed:

- typed destructive commands (`shutdown`, `reboot`, `rm -rf`, `mkfs`, `dd if=`,
  `format`, `Stop-Computer`, …, cross-platform), caught in the text *and* when a
  later Enter would execute a dangerous command already typed;
- the Ctrl+Alt+Del power chord;
- all ATX power actions;
- optionally, a mouse click landing on a destructive-looking control
  (`check_label=true` — see the Security section above; opt-in since it costs
  an extra look).

A blocked action returns a one-time, 10-minute token printed for the *human*. The
agent relays it and waits; only after explicit approval does the action run. That
token is hashed at rest, never written to disk in recoverable form, and the state
directory is locked to owner-only. That confirm prompt is the real safety
boundary within the harness's own tools — and across every run here, it held.

*Harness: [github.com/tiandklerk/kvmpilot](https://github.com/tiandklerk/kvmpilot).
Target: a PiKVM (Raspberry Pi Zero 2 W, kvmd 4.132) → Ubuntu server. Drivers:
Claude Opus 4.8 and Sonnet via Claude Code; Kimi K3 via Hermes on a Raspberry Pi
5. All actions benign; my_desktop uptime unaffected. CAPTCHA attempts on Google's own
reCAPTCHA v2 demo page, as a capability test.*
