# Presets

Ready-made example macros you can use as a starting point. Unlike your own recordings
(which stay private in `macros/`), presets are shipped with the project.

## How to use a preset
Copy the preset into your local `macros/` folder, then **Load** it in the app:
```bash
# Windows (cmd)
copy presets\whatsapp_export.json macros\
# macOS / Linux
cp presets/whatsapp_export.json macros/
```
Then in the GUI: **Load → whatsapp_export → Run macro** (or run it from the CLI).

---

## `whatsapp_export.json`
Exports a WhatsApp **chat or group** entirely by **reading the screen** (`tap_text` +
`if_text`), so it uses **no fixed coordinates** and works across screen sizes and for both
1:1 chats and groups.

**Flow:**
```
(open a chat/group first)
More options → More → Export chat → Include media
   ├─ if the "Unable to export" popup appears → OK → (reopen) → Export chat → Without media
   └─ else → continue
→ stops at the Android share sheet
```

**Status / WIP:** this preset stops at the **share sheet**. The final "where to save the
export" step is intentionally left out — pick a destination manually, or adapt the macro to
tap a "save to device" target so the file can be pulled with `adb pull`. (For forensic use,
WhatsApp media can also be pulled directly from
`/sdcard/Android/media/com.whatsapp/WhatsApp/Media/`.)

**Note:** button wording can vary by WhatsApp version/language. If a step can't find its text,
open that screen and adjust the `text` value in the JSON to match what's shown.

> Use only on devices you own or are authorized to access.
