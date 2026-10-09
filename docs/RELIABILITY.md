# Reliability & Evidence Integrity

How the WhatsApp batch exporter guarantees a **complete, non-duplicated, verifiable**
collection — and what to watch for in the field. For setup and day-to-day usage see the main
[README](../README.md); this doc explains the safeguards and the problems they solve.

---

## 1. Problem statement

A seized phone can hold hundreds of WhatsApp chats. Exporting each one by hand (⋮ → Export chat →
share → pick the PC) is hours of work and, worse, hard to defend:

- **Volume** — too many chats to do manually without mistakes.
- **Human error** — chats get missed, exported twice, or sent to the wrong nearby device.
- **Evidence integrity** — without a consistent hash log you cannot prove a file was unaltered.
- **Fragility** — a crash, an unlock, or a USB drop mid-run loses track of what was done.
- **Offline requirement** — the phone stays in airplane mode (Quick Share over BT/Wi-Fi Direct); no
  cloud, no backups.

The tool automates the whole pipeline and treats **every export as evidence**.

---

## 2. How one chat is exported

1. **Read the list** — ADB reads the on-screen chat names (any script) and scrolls with a
   momentum-free swipe so no chat is skipped.
2. **Open & export** — taps the chat → ⋮ → Export chat → **Include media** (media is always
   attempted first). Privacy-locked chats are detected and flagged, not stuck on.
3. **Send via Quick Share** — on the Android share sheet it taps Quick Share and picks the PC by
   **exact name** — never a different device that happens to be in range.
4. **Receive & verify** — the PC auto-accepts; the tool waits for the real file to land, validates
   the `.zip`, computes its **SHA-256**, and only then marks the chat done.
5. **Log as evidence** — a row is written to the CSV (chat, status, media, file, hash, time) with an
   atomic, crash-safe, never-erased write.

---

## 3. The guarantees (and how they are enforced)

### a. A chat is never exported twice
The duplicate bug came from **slow big-media transfers** (long PDFs, videos): Quick Share writes the
received file only when the transfer *finishes*, so mid-flight nothing is visible on the PC, and the
phone's share screen lists **every** nearby device. An early/stray "Failed" (often from another
device) used to be treated as our failure → the chat was re-sent → duplicate `(1)(2)(3).zip`.

Enforced by, in order:
- **No fast-fail on "Failed"** — a phone "Failed" never triggers a re-send; the received file is the
  verdict.
- **Activity-aware wait** — keeps waiting while the phone shows *Sending / % / transferring*, so a
  slow transfer is never cut off.
- **Pre-retry guard (`_await_late_file`)** — before *any* re-send, it watches the folder **and** the
  phone; if a file is still arriving, still growing, or the phone still shows progress, it keeps
  waiting and accepts the file when it lands. Two transfers can never be in flight at once. This
  holds **even when the phone's transfer screen is unreadable**.

### b. A partial/corrupt file is never recorded as success
The received `.zip` is opened and validated. A truncated transfer (an incomplete zip) is **retried**,
never written to the CSV as `ok`.

### c. The evidence CSV is never lost
- Written atomically (temp file → `os.replace`), so a crash mid-write cannot corrupt it.
- The parent folder is created automatically (no "No such file or directory").
- It **refuses to overwrite a non-empty CSV with an empty one**, so a failed/empty run never erases
  prior progress.
- On resume, the source CSV is backed up (`.bak`) before anything else.

### d. The export goes only to the intended PC
The PC is matched by its **exact** name in the Quick Share picker. If the exact name can't be found,
the tool fails that attempt rather than guessing — it never blind-taps a device position (the picker
order changes as devices come and go).

### e. Same phone → same case file
Resuming on the same device (matched by serial) continues the **same** evidence CSV; a different
device opens a **new** case folder. Progress survives restarts.

### f. No chat silently skipped
Chat names in any script (Telugu, Hindi, Arabic, CJK…) and emoji-only names are read and identified
correctly. (A previous loop-guard collapsed non-Latin names to an empty key and mass-skipped them;
that is fixed.)

### g. Every export is verifiable
Each saved file carries a **SHA-256** recorded at collection time, plus a content manifest
(`verified contents: N files, M media, X MB`) taken from the actual zip — so the `media` column and
file sizes are truthful, not guessed.

---

## 4. Resilience during a long run

- **PC stays awake** — system sleep and screen-off are suppressed for the whole run
  (`pc_keep_awake`), the PC-side equivalent of keeping the phone awake.
- **Phone stays awake** — screen-off timeout is raised and `stay-on` asserted; restored afterwards.
- **WhatsApp ANR handled** — if Android shows "WhatsApp isn't responding", the tool taps **Wait** and
  carries on instead of stalling.
- **Device drops / locks** — the run waits for the device to come back / be unlocked rather than
  losing progress, and logs the real adb state (unauthorized / offline / none) so a long
  "initialisation" is never a mystery.

---

## 5. Status values in the CSV / list

| Status | Meaning | Retried? |
|---|---|---|
| `ok` | Complete, valid file received and hashed | — |
| `pending` | Discovered, not yet exported | — |
| `running` | Currently exporting | — |
| `skipped` | Deliberately not exported (before a chosen start point, not selected, or a confirmed duplicate) | no |
| `blocked-privacy` | WhatsApp "Advanced Chat Privacy" blocks export for this chat | no |
| `fail-transfer` / `fail-sent` | File never arrived / transfer reported failed | yes |
| `fail-export` | Couldn't drive the export menu | yes |
| `fail-noshare` / `fail-pcpick` | Share sheet or PC not found in time | yes |
| `fail-timeout` | Ran past the per-chat time budget | no |

Transient `fail-*` states are retried a few times; a chat that keeps failing is left in the CSV so it
can be retried on the next run (resume).

---

## 6. Reading the log — what a healthy run looks like

```
PC sleep/screen-off suppressed for the duration of the run
sent to <PC>; waiting for the file to arrive...
phone shows Quick Share 'Completed'
verified contents: 42 files, 41 media, 9.5 MB media (with media)
received WhatsApp Chat with <X>.zip (9,730,112 bytes, with media)
```

Warning signs and what they mean:

| Log line | Meaning / action |
|---|---|
| `WARNING: save folder '…' does not exist` | The Save folder is wrong — point it at the real Quick Share destination. |
| `phone shows 'Completed' but no file appeared in '…'` | Quick Share is saving somewhere else — fix the Save folder; otherwise every transfer counts as failed. |
| `received file is not a valid/complete zip … retrying` | A transfer was cut short; it will be retried (no corrupt file recorded). |
| `arrived late; NOT re-sending (no duplicate)` | The anti-duplicate guard caught a slow transfer landing late. Working as intended. |
| `handled a 'WhatsApp isn't responding' dialog (tapped Wait)` | WhatsApp hit an ANR and was kept alive. |
| `waiting for device - UNAUTHORIZED …` | Unlock the phone and tap Allow on the USB-debugging prompt. |

> **Note:** a chat with a big attachment legitimately takes a few minutes to transfer — the tool
> waits for it once and verifies it, instead of re-sending it. Slow ≠ broken.

---

## 7. Field checklist before a run

1. New code deployed on **both** the controller PC and the **receiving** PC.
2. GUI **Save folder** set to the exact folder Quick Share drops files into.
3. Phone unlocked, plugged into a reliable USB port (not a hub), USB-debugging allowed.
4. Restart WhatsApp (or the phone) before a big run so it starts with free memory — the biggest
   reduction in ANRs.
5. Airplane mode on, Bluetooth + Wi-Fi on for Quick Share (or tick **USB transfer** — see §8).

---

## 8. USB transfer mode (`adb pull`) — removing the root cause

Most field bugs (false "failed", retries, duplicates, slowness) shared one root cause: **Quick Share
gives no ground truth.** The PC file is written atomically at the very end (nothing visible
mid-transfer), the phone's screen lists every nearby device, and over Bluetooth it is slow — so
"did it arrive?" had to be *inferred from timing*.

Ticking **"USB transfer (adb pull) instead of Quick Share"** removes that channel: the export is
saved to the phone's own storage via the share sheet's **Save to Files / My Files / Files** target,
and the tool `adb pull`s the **exact** file over USB into the Save folder.

- Deterministic: the tool knows precisely which file it saved — no guessing, no nearby devices.
- Fast: USB is far quicker than Bluetooth.
- No duplicates: before any retry it checks whether an earlier attempt already saved the export on
  the phone, and pulls that instead of exporting again. A pulled file never overwrites an existing
  one (`name (1).zip`, ...).
- First run only: if the save target isn't readable on your phone, the log says
  **ACTION NEEDED: tap your 'Save to Files' / 'My Files' share target** — tap it once on the phone;
  your tap does the action *and* teaches the position for every following chat (same for the
  Save/Done confirmation, if your file manager shows one).

Note: in this mode a copy of each export also stays in the phone's Download/Documents folder.

## 9. Screen reading — what makes it robust

Every decision the tool makes starts from a `uiautomator` screen dump. It is hardened against the
failure modes seen in the field:

| Failure mode | Protection |
|---|---|
| Dump cut short when the busy phone kills `uiautomator` | only a **complete** dump (opening *and* closing tag) is accepted; otherwise retried |
| One odd character in one contact name blanked the whole screen | illegal XML characters are stripped; if the XML is still malformed, nodes are extracted by a fallback parser |
| Chat name only in `content-desc` | names are read from text **or** content-desc |
| Half-visible row at a screen edge | only fully visible rows are tapped; the list is nudged first |
| Same chat reading as two names | invisible direction marks removed anywhere, all whitespace unified, Unicode-normalized |
| Redundant back-to-back dumps (slow) | a 0.35 s cache, invalidated by any tap/swipe/key, so waits still see every change |

## 10. Regression tests — keeping fixed bugs fixed

`tests/test_regressions.py` holds one test per bug that has hit this tool in the field. They run
with the standard library only (no phone, no installs):

```
python -m unittest discover -s tests -v
```

GitHub Actions (`.github/workflows/tests.yml`) runs them on every push and pull request. Each test
was verified by **re-injecting the original bug** and confirming the test fails — so a future change
that brings a bug back is caught before it reaches a phone.
