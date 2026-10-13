# UI event queue — investigation and two failed fixes

Handoff note. Everything below is read out of `build/firmware.bin`
(stock V2.2.0r) with Ghidra 12.1.3 and a raw-encoding scanner. Both fixes
described here are **reverted**; the shipped image is back to
`c1565f1596a1d83f25e9ec2237f35a91`.

## The symptom

Filter (cutoff) selected. Held `[TYPE] + [WAVE]` — the manual's *Unlink X-Env
Sequence* mode — pressed a couple of `[SEQ]` keys while held, released. From
then on the panel does not respond. **The sequencer keeps playing and audio
is unaffected.** Occasionally a double press of `TYPE+WAVE` gets a reaction.

## What is established

### The main loop was not stuck

The patch enables the IWDG: prescaler /128, reload 4000 → **16 s timeout**,
and it is kicked from the main loop by the hook at `0x08119A02`. A stalled
main loop would reset the unit within 16 s. It did not reset. So the failure
is **state**, not control flow.

Ruled out along the way:

* The patch's only unbounded loop is the retry in `np_new`. It cannot spin:
  the new-handler slot `0x2000E194` is referenced **only** by the getter
  `0x08159B4C`, `set_new_handler` is never called, so the slot is always 0
  and the first failure goes to `new_abort()`.
* A whole-image sweep found 24 loops of the shape *load / compare / branch
  back* with no store, call or arithmetic. All are boot-time peripheral
  status waits except one: `sub_08115050` / `sub_08115174` enable EXTI4 and
  spin `do {} while (flag == 0)` with no timeout. That is analogue
  calibration and is unreachable while playing — the only path is
  `main → sub_08115570`, once at startup, behind a `0x12121212` magic check
  and boot-time key tests.

### The UI event queue

`sub_08157578(obj, &event)` does not act on an event. It allocates a 12-byte
node, links the event into a list at `obj+0x1C`, nulls the caller's pointer
and bumps a depth counter at `obj+0x24`. The queue is **unbounded**.

`sub_08157650(obj)` drains it under a **time budget**:

```
0x0815765A  start = now()                 ; sub_081479A4 = 1000*millis + TIM8_CNT, microseconds
0x08157662  if (list empty) return
0x08157670  b 0x081576B8                  ; jump to the check
0x08157672  body: take one event, dispatch it, free the node
0x081576B2  if (list empty) return
0x081576B8  check: elapsed = now() - start
0x081576BE         budget  = obj[0x54]
0x081576C6         if (elapsed < budget) goto body
0x081576CA  return
```

The budget is tested **before the first dispatch**. A budget of zero
therefore dispatches nothing, ever — the queue grows without bound and that
UI object stops responding while everything else runs, and the watchdog stays
happy.

Two constructors initialise `+0x54`:

| constructor | value written | meaning |
|---|---|---|
| `sub_0814A8FC` @ `0x0814A994` | `0x3F800000` | the bit pattern of the float `1.0f`, stored into a field read as an integer count — about 1065 s, effectively no limit |
| `sub_08145C40` @ `0x08145CE0` | `0` (`strd r5,r5,[r4,#0x54]`, r5 = 0 from `0x08145C4A`) | **no events at all** |

### The mode latch, for completeness

`TYPE+WAVE` is key code `0x20`. The mode byte is `obj+0xED`. Exactly two
instructions in the whole image write it — `0x08151870` in `sub_08147B4C` and
`0x08151936` in `sub_08147B54` — and both do `^= 1`. Nothing else clears it:
no other key, no mode change, no timeout. It gates the key handling wholesale
(e.g. `0x0815002E`: `ldrb r3,[r0,#0xED] / cbnz r3, …`). The manual describes
the feature as a *hold*; the firmware implements a toggle.

The selected-parameter index `obj+0xEF` has ~130 readers and exactly one
writer, `sub_0814A434` @ `0x0814A438`; that setter is clean (no bounds check,
but no path was found that could feed it a bad index).

## Fix attempt 1 — FAILED, caused resets

`0x08157670`: `b 0x081576B8` → `nop`, so the loop would dispatch one event
before consulting the budget. The list is known non-empty there
(`0x08157662`–`0x08157666` already branched away if empty).

**Result on hardware: pressing `PATTERN` then any `[SEQ]` key reboots the
unit.**

**Why it is wrong:** the entry branch is not only the loop condition. The
check block also sets up the body's arguments:

```
0x081576C2  add r1, sp, #4      ; arg 2
0x081576C4  mov r0, r4          ; arg 1
```

and the body calls `bl 0x0815748C` at `0x0815767C` without setting either.
Skipping the check enters the body with `r0`/`r1` holding garbage.

## Fix attempt 2 — FAILED, intermittent

`0x081576BE`: `ldr r2,[r4,#0x54]` (`62 6d`) → `movs r2,#200` (`c8 22`),
forcing a 200 µs budget so the object's zero can never apply. Register
liveness was checked instruction by instruction this time: `r2` is written
only there and read only by `cmp r3,r2` at `0x081576C6`; the flags `movs`
sets are overwritten by that same `cmp` before the `blo`. The entry branch
was left alone and a static test pinned it.

**Result on hardware: `[SEQ]` keys sometimes do not register.**

**Why it might be wrong — not established.** Candidates worth checking:

1. 200 µs is simply too small for the object that previously had no limit,
   so a burst is left partly undrained and events are perceived as lost.
   The value is capped at 255 by the `movs` imm8 encoding; a larger budget
   needs a different site or a 4-byte replacement.
2. `obj[0x54]` may not be a pure budget for both objects — the same offset in
   the second class may mean something else, and forcing 200 changed
   unrelated behaviour.
3. Events may be dropped rather than delayed somewhere else in the path, so
   bounding the drain made an existing drop path more likely.

## Open questions

* Which of the two constructors builds the object behind the panel, and
  whether entering unlink mode installs a second one (placement-new over the
  same storage) whose budget is the zero.
* Whether `obj+0x54` really is a time budget in **both** classes.
* What clears `obj+0xED` in normal use, if anything — as read, only the same
  chord can.
* The queue depth `obj+0x24` and the pool counters would settle most of this,
  but the diagnostic block at `0x2000E400` is only readable over SWD
  (`dump binary memory build/diag.bin 0x2000e400 0x2000e530`, decode with
  `python3 tools/patch.py diag build/diag.bin`).
