"""
chat_viewer.py - build a single self-contained, offline "WhatsApp Web"-style HTML page
from a folder of WhatsApp chat-export .zip files (each zip = one exported chat).

Usage:
    python chat_viewer.py <folder-of-zips> [-o viewer.html] [--max-embed-mb 20] [--owner NAME]

- Parses each zip's _chat.txt (iOS "[date, time] Sender:" and Android "date, time - Sender:").
- Embeds media as base64 data URIs so the output is ONE portable .html (no external files).
- Media larger than --max-embed-mb is shown as a labelled placeholder instead of bloating the
  file (use --max-embed-mb 0 to embed everything, regardless of size).
- Fully offline: no CDNs, pure Python standard library + vanilla JS. Read-only viewer.
"""
from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import html
import json
import os
import re
import sys
import zipfile

# ---- WhatsApp export line formats ---------------------------------------------------------
# iOS:     [06/09/2024, 10:30:45] John Doe: message
# Android: 06/09/2024, 10:30 - John Doe: message      (or "9/6/24, 10:30 AM - John: ...")
_LTR = "\u200e\u200f"  # RTL/LTR marks WhatsApp sprinkles in
_RE_IOS = re.compile(
    r"^[%s]*\[(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?)\]\s*(.*)$" % _LTR
)
_RE_AND = re.compile(
    r"^[%s]*(\d{1,2}[/.]\d{1,2}[/.]\d{2,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?)\s+-\s+(.*)$" % _LTR
)
# media reference forms inside a message
_RE_ATTACHED = re.compile(r"<attached:\s*([^>]+)>", re.IGNORECASE)
_RE_FILEATT = re.compile(r"([^\s]+\.[A-Za-z0-9]{2,4})\s*\(file attached\)", re.IGNORECASE)

_IMG = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VID = {".mp4", ".3gp", ".mov", ".mkv", ".webm"}
_AUD = {".opus", ".mp3", ".m4a", ".aac", ".ogg", ".wav", ".amr"}
_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".mp4": "video/mp4", ".3gp": "video/3gpp",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".opus": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".ogg": "audio/ogg", ".wav": "audio/wav", ".amr": "audio/amr", ".pdf": "application/pdf",
}


def _kind(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in _IMG:
        return "image"
    if ext in _VID:
        return "video"
    if ext in _AUD:
        return "audio"
    return "file"


def _split_sender(rest):
    """From the text after the timestamp, split 'Sender: message'. Returns (sender, text).
    System messages (no 'Name: ') come back as (None, rest)."""
    # a real sender line has 'Name: ' fairly early and the name has no newline
    idx = rest.find(": ")
    if idx == -1:
        # could be 'Name:' at end, or a system line
        if rest.endswith(":"):
            return rest[:-1].strip(), ""
        return None, rest.strip()
    sender = rest[:idx].strip()
    # sender names don't contain these; if they do, it's probably a system line with a colon
    if len(sender) > 60 or "\n" in sender:
        return None, rest.strip()
    return sender, rest[idx + 2:]


def parse_chat_text(text):
    """Parse a _chat.txt into a list of message dicts: {ts, sender, text, media_refs:[names]}."""
    msgs = []
    cur = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        m = _RE_IOS.match(line) or _RE_AND.match(line)
        if m:
            date, tm, rest = m.group(1), m.group(2), m.group(3)
            sender, body = _split_sender(rest)
            cur = {"ts": f"{date}, {tm}", "sender": sender, "text": body, "media_refs": []}
            msgs.append(cur)
        elif cur is not None:
            cur["text"] += "\n" + line              # continuation of the previous message
        # else: preamble before the first dated line -> ignore
    # normalise senders: WhatsApp writes the OWNER's own messages with the name replaced by a
    # lone LTR mark (e.g. "- ‎: Yes"), so a sender that's only direction marks/space == "You".
    for mo in msgs:
        if mo["sender"] is not None:
            s = mo["sender"].strip(_LTR + " \t")
            mo["sender"] = s if s else "You"
    # extract media references out of each message's text
    for mo in msgs:
        refs = []
        for rx in (_RE_ATTACHED, _RE_FILEATT):
            for mm in rx.finditer(mo["text"]):
                refs.append(os.path.basename(mm.group(1).strip()))
        # strip the media tokens from the visible text
        t = _RE_ATTACHED.sub("", mo["text"])
        t = _RE_FILEATT.sub("", t)
        t = t.replace("<Media omitted>", "").strip(" \t" + _LTR)
        mo["text"] = t
        mo["media_refs"] = refs
    return msgs


def load_zip(path, max_bytes, mode="embed", media_root=None, idx=0):
    """Return a chat dict {name, messages, ...} from one export zip, or None if it has no chat.
    mode="embed": media as base64 data URIs (single-file output).
    mode="folder": media extracted to <media_root>/<idx>/ and referenced by relative path
                   (handles ALL media, any size - one index.html + a media/ folder)."""
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        print(f"  skip {os.path.basename(path)}: {e}")
        return None
    names = zf.namelist()
    txt_name = next((n for n in names if n.lower().endswith("_chat.txt")), None) \
        or next((n for n in names if n.lower().endswith(".txt")), None)
    if not txt_name:
        print(f"  skip {os.path.basename(path)}: no _chat.txt")
        return None
    raw = zf.read(txt_name)
    text = raw.decode("utf-8", "replace")
    msgs = parse_chat_text(text)

    out_dir = None
    if mode == "folder":
        out_dir = os.path.join(media_root, str(idx))
        os.makedirs(out_dir, exist_ok=True)

    # index media files in the zip by basename
    files = {os.path.basename(n): n for n in names if not n.lower().endswith(".txt")}
    used, embedded, skipped, embed_bytes = set(), 0, 0, 0
    for mo in msgs:
        media = []
        for ref in mo["media_refs"]:
            inzip = files.get(ref)
            item = {"name": ref, "kind": _kind(ref), "data": None, "src": None, "size": 0}
            if inzip is not None:
                used.add(inzip)
                info = zf.getinfo(inzip)
                item["size"] = info.file_size
                if mode == "folder":
                    # extract to media/<idx>/<basename> and reference by relative path (ALL media)
                    dest = os.path.join(out_dir, ref)
                    if not os.path.exists(dest):
                        with zf.open(inzip) as src, open(dest, "wb") as dst:
                            while True:
                                chunk = src.read(1 << 20)
                                if not chunk:
                                    break
                                dst.write(chunk)
                    item["src"] = f"media/{idx}/{ref}"
                    embedded += 1
                    embed_bytes += info.file_size
                elif max_bytes == 0 or info.file_size <= max_bytes:
                    b = zf.read(inzip)
                    mime = _MIME.get(os.path.splitext(ref)[1].lower(), "application/octet-stream")
                    item["data"] = f"data:{mime};base64," + base64.b64encode(b).decode("ascii")
                    embedded += 1
                    embed_bytes += info.file_size
                else:
                    skipped += 1
            media.append(item)
        mo["media"] = media
        mo.pop("media_refs", None)

    # derive the chat name from the file name: "WhatsApp Chat with X.zip" -> "X"
    base = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"^WhatsApp Chat with\s*", "", base).strip() or base
    senders = [m["sender"] for m in msgs if m["sender"]]
    last = next((m for m in reversed(msgs) if m["text"] or m.get("media")), None)
    preview = ""
    if last:
        preview = last["text"] or (last["media"][0]["kind"] if last.get("media") else "")
    return {
        "name": name,
        "file": os.path.basename(path),
        "sha256": _sha256(path),
        "messages": msgs,
        "count": len(msgs),
        "senders": sorted(set(senders)),
        "preview": preview[:80],
        "embedded": embedded,
        "skipped": skipped,
        "embed_bytes": embed_bytes,
    }


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(folder, out_path, max_embed_mb, owner, mode="embed"):
    zips = sorted(glob.glob(os.path.join(folder, "*.zip")))
    if not zips:
        print("No .zip files found in:", folder)
        return 1
    max_bytes = int(max_embed_mb * 1024 * 1024)
    media_root = None
    if mode == "folder":
        media_root = os.path.join(os.path.dirname(os.path.abspath(out_path)), "media")
        os.makedirs(media_root, exist_ok=True)
    print(f"Building viewer ({mode} mode) from {len(zips)} zip(s) in {folder}")
    chats = []
    for i, z in enumerate(zips):
        print(f"- {os.path.basename(z)}")
        c = load_zip(z, max_bytes, mode=mode, media_root=media_root, idx=i)
        if c:
            chats.append(c)
    if not chats:
        print("No parseable chats.")
        return 1
    chats.sort(key=lambda c: c["name"].lower())
    data = {"owner": owner or "", "chats": chats,
            "generated": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    payload = json.dumps(data, ensure_ascii=False)
    # make the JSON safe to inline inside a <script>: U+2028/U+2029 are valid JSON but illegal in
    # JS string literals, and "</" could prematurely close the tag. Both escapes are valid JSON.
    payload = (payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
                      .replace("</", "<\\/"))
    htmldoc = HTML_TEMPLATE.replace("/*__DATA__*/", "window.__CHATS__ = " + payload + ";")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(htmldoc)
    total_embed = sum(c["embed_bytes"] for c in chats)
    total_skip = sum(c["skipped"] for c in chats)
    size = os.path.getsize(out_path)
    print(f"\nWrote {out_path}  ({size/1e6:.1f} MB)")
    if mode == "folder":
        print(f"chats={len(chats)}  media extracted={sum(c['embedded'] for c in chats)} "
              f"({total_embed/1e6:.1f} MB) -> {media_root}")
        print("Open index.html; keep the media/ folder beside it.")
    else:
        print(f"chats={len(chats)}  media embedded={sum(c['embedded'] for c in chats)} "
              f"({total_embed/1e6:.1f} MB)  media too-large-skipped={total_skip}")
    if size > 800e6:
        print("WARNING: the HTML is very large and may be slow to open. Lower --max-embed-mb.")
    return 0


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat Viewer</title>
<style>
  :root{--bg:#0b141a;--side:#111b21;--panel:#202c33;--hd:#202c33;--in:#202c33;--out:#005c4b;
        --txt:#e9edef;--muted:#8696a0;--line:#222d34;--accent:#00a884;--hl:#f6c344;--cur:#ff8a3d;--snd:#53bdeb;}
  *{box-sizing:border-box}
  [hidden]{display:none!important}
  html,body{margin:0;height:100%}
  body{font-family:"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--txt);overflow:hidden}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#374248;border-radius:4px}
  ::-webkit-scrollbar-thumb:hover{background:#4a575f}
  .app{display:flex;height:100vh}
  /* sidebar */
  .side{width:min(32vw,400px);min-width:280px;display:flex;flex-direction:column;background:var(--side);
        border-right:1px solid var(--line);position:relative}
  .side-hd{display:flex;align-items:center;justify-content:space-between;padding:15px 16px;background:var(--hd);font-weight:600;font-size:16px}
  .side-hd .n{font-size:12px;color:var(--muted);font-weight:400}
  .side-search{padding:8px 10px}
  .side-search input{width:100%;padding:9px 16px;border-radius:20px;border:none;background:var(--panel);color:var(--txt);font-size:14px;outline:none}
  .chats{overflow-y:auto;flex:1}
  .chat{padding:10px 14px;cursor:pointer;display:flex;gap:13px;align-items:center;border-bottom:1px solid rgba(255,255,255,.04)}
  .chat:hover{background:#182229}.chat.active{background:#2a3942}
  .avatar{width:46px;height:46px;border-radius:50%;color:#04231d;flex-shrink:0;display:flex;
          align-items:center;justify-content:center;font-weight:700;font-size:18px}
  .chat .meta{overflow:hidden;flex:1}
  .chat .nm{font-size:15.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .chat .pv{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
  /* main */
  .main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg)}
  .top{display:flex;align-items:center;gap:12px;padding:9px 16px;background:var(--hd);border-bottom:1px solid var(--line)}
  .top .av{width:40px;height:40px;font-size:16px}
  .top .ti{min-width:0;flex:1}
  .top .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .top .sub{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .top .tools{display:flex;align-items:center;gap:6px}
  .top select{background:var(--panel);color:var(--txt);border:none;border-radius:6px;padding:6px 8px;outline:none;font-size:12px;max-width:130px}
  .iconbtn{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:7px 9px;border-radius:50%;line-height:1}
  .iconbtn:hover{background:rgba(255,255,255,.08);color:var(--txt)}
  /* in-chat search bar */
  .cbar{display:flex;align-items:center;gap:8px;padding:8px 14px;background:#111b21;border-bottom:1px solid var(--line)}
  .cbar input{flex:1;padding:8px 14px;border-radius:8px;border:none;background:var(--panel);color:var(--txt);outline:none;font-size:14px}
  .cbar .cnt{font-size:12px;color:var(--muted);min-width:46px;text-align:center}
  /* messages */
  .msgs{flex:1;overflow-y:auto;padding:16px 6%;display:flex;flex-direction:column;gap:2px}
  .day{align-self:center;background:#182229;color:var(--muted);font-size:12px;padding:5px 12px;border-radius:8px;margin:12px 0 8px;box-shadow:0 1px 1px rgba(0,0,0,.2)}
  .row{display:flex;margin-top:2px}.row.me{justify-content:flex-end}.row.grp{margin-top:9px}
  .bub{max-width:74%;padding:6px 9px 8px;border-radius:8px;background:var(--in);font-size:14.2px;line-height:1.36;
       white-space:pre-wrap;word-wrap:break-word;box-shadow:0 1px .5px rgba(0,0,0,.15);transition:background .5s}
  .row.me .bub{background:var(--out)}
  .bub .snd{font-size:12.7px;font-weight:600;color:var(--snd);margin-bottom:2px}
  .bub .tm{font-size:11px;color:var(--muted);float:right;margin:6px 0 -3px 12px;user-select:none}
  .bub img,.bub video{max-width:320px;max-height:400px;border-radius:6px;display:block;margin:2px 0;cursor:pointer}
  .bub audio{width:250px;margin:3px 0}
  .bub .file{display:inline-flex;gap:8px;align-items:center;padding:9px 12px;background:rgba(0,0,0,.2);border-radius:8px;color:var(--txt);text-decoration:none;margin:2px 0}
  .bub .ph{font-size:12px;color:var(--muted);font-style:italic;padding:6px 8px;background:rgba(0,0,0,.15);border-radius:6px;margin:2px 0}
  .sys{align-self:center;background:#182229;color:var(--muted);font-size:12.5px;padding:5px 12px;border-radius:8px;margin:6px 0;max-width:80%;text-align:center}
  mark{background:var(--hl);color:#000;border-radius:2px;padding:0 1px}
  mark.cur{background:var(--cur);color:#000}
  .empty{margin:auto;color:var(--muted);text-align:center;font-size:15px}
  /* global search results */
  .gres{position:absolute;top:107px;left:0;right:0;bottom:0;background:var(--side);overflow:auto;z-index:5}
  .gres .g{padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer}
  .gres .g:hover{background:#182229}
  .gres .g .c{font-size:12.5px;color:var(--accent);margin-bottom:2px}
  .gres .g .s{font-size:13px;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .gres .more{padding:8px 14px;font-size:12px;color:var(--muted)}
  /* lightbox */
  .lb{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;align-items:center;justify-content:center;z-index:50;cursor:zoom-out}
  .lb img,.lb video{max-width:92vw;max-height:92vh;border-radius:4px}
  @media(max-width:640px){.side{width:46vw;min-width:0}.msgs{padding:14px 4%}.bub{max-width:86%}}
</style></head>
<body>
<div class="app">
  <aside class="side">
    <div class="side-hd"><span>Chats</span><span class="n" id="cinfo"></span></div>
    <div class="side-search"><input id="q" placeholder="Search all chats &amp; messages"></div>
    <div id="gres" class="gres" hidden></div>
    <div id="chats" class="chats"></div>
  </aside>
  <main class="main">
    <header class="top" id="top" hidden>
      <div class="avatar av" id="tav"></div>
      <div class="ti"><div class="nm" id="tnm"></div><div class="sub" id="tsub"></div></div>
      <div class="tools">
        <select id="owner" title="Which sender is 'you' (right side)"></select>
        <button class="iconbtn" id="csBtn" title="Search in this chat">&#128269;</button>
      </div>
    </header>
    <div class="cbar" id="cbar" hidden>
      <input id="cs" placeholder="Search in this chat...">
      <span class="cnt" id="hit"></span>
      <button class="iconbtn" id="prev" title="Previous match (Shift+Enter)">&#9650;</button>
      <button class="iconbtn" id="next" title="Next match (Enter)">&#9660;</button>
      <button class="iconbtn" id="csClose" title="Close">&#10005;</button>
    </div>
    <div class="msgs" id="msgs"><div class="empty">Select a chat to view its messages</div></div>
  </main>
</div>
<div class="lb" id="lb"></div>
<script>
/*__DATA__*/
const DATA = window.__CHATS__ || {chats:[]};
const chats = DATA.chats;
let active=-1, ownerByChat={}, marks=[], curMark=-1;
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const initial=n=>((n||"?").trim().charAt(0)||"?").toUpperCase();
const reEsc=q=>q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
const AV=['#00a884','#53bdeb','#f6a935','#e5738a','#9b8cff','#5fbf77','#f27c74','#48b5c4','#c98bdb'];
function avColor(n){let h=0;for(const ch of (n||"")) h=(h*31+ch.charCodeAt(0))>>>0;return AV[h%AV.length];}
const dayOf=ts=>(ts||"").split(', ')[0]||"";
const timeOf=ts=>{const p=(ts||"").split(', ');return p.length>1?p.slice(1).join(', '):"";};

$('#cinfo').textContent = chats.length+' chats';

function guessOwner(c){
  if(ownerByChat[c.name]!==undefined) return ownerByChat[c.name];
  if(DATA.owner && c.senders.includes(DATA.owner)) return DATA.owner;
  if(c.senders.includes("You")) return "You";                 // WhatsApp writes the owner as "You"
  const others=c.senders.filter(s=>s.toLowerCase()!==c.name.toLowerCase());
  return (c.senders.length===2 && others.length===1)?others[0]:"";
}

function renderChatList(filter){
  const box=$('#chats'); box.innerHTML=''; const f=(filter||"").toLowerCase();
  chats.forEach((c,i)=>{
    if(f && !c.name.toLowerCase().includes(f)) return;
    const d=document.createElement('div'); d.className='chat'+(i===active?' active':'');
    d.innerHTML=`<div class="avatar" style="background:${avColor(c.name)}">${esc(initial(c.name))}</div>
      <div class="meta"><div class="nm">${esc(c.name)}</div><div class="pv">${esc(c.preview||'')}</div></div>`;
    d.onclick=()=>openChat(i); box.appendChild(d);
  });
}

function mediaHtml(m){
  const src=m.src||m.data;                       // folder mode = relative path, embed mode = data URI
  if(src){
    if(m.kind==='image') return `<img loading="lazy" src="${src}" onclick="lightbox('img',this.src)">`;
    if(m.kind==='video') return `<video controls preload="none" src="${src}"></video>`;
    if(m.kind==='audio') return `<audio controls preload="none" src="${src}"></audio>`;
    return `<a class="file" href="${src}" download="${esc(m.name)}">&#128206; ${esc(m.name)}</a>`;
  }
  return `<div class="ph">&#128206; ${esc(m.name)} — not embedded (${(m.size/1024).toFixed(0)} KB)</div>`;
}

function openChat(i, scrollToIdx){
  active=i; renderChatList($('#q').value);
  const c=chats[i];
  $('#top').hidden=false;
  const av=$('#tav'); av.style.background=avColor(c.name); av.textContent=initial(c.name);
  $('#tnm').textContent=c.name;
  $('#tsub').textContent=`${c.count} messages · ${c.senders.join(', ')}`;
  const sel=$('#owner'); sel.innerHTML='<option value="">auto</option>'+
    c.senders.map(s=>`<option${guessOwner(c)===s?' selected':''}>${esc(s)}</option>`).join('');
  sel.onchange=()=>{ownerByChat[c.name]=sel.value; renderMsgs(c);};
  resetCs();
  renderMsgs(c, scrollToIdx);
}

function renderMsgs(c, scrollToIdx){
  const owner=guessOwner(c); const box=$('#msgs'); box.innerHTML='';
  let lastDay=null, lastSender=null;
  c.messages.forEach((m,idx)=>{
    const d=dayOf(m.ts);
    if(d && d!==lastDay){const dv=document.createElement('div');dv.className='day';dv.textContent=d;box.appendChild(dv);lastDay=d;lastSender=null;}
    if(m.sender===null){
      const s=document.createElement('div');s.className='sys';s.textContent=m.text;box.appendChild(s);lastSender=null;return;
    }
    const me=owner && m.sender===owner;
    const grp=m.sender!==lastSender;
    const row=document.createElement('div');row.className='row'+(me?' me':'')+(grp?' grp':'');
    const media=(m.media||[]).map(mediaHtml).join('');
    const txt=m.text?`<span class="t">${esc(m.text)}</span>`:'';
    row.innerHTML=`<div class="bub" data-i="${idx}">${(!me&&grp)?`<div class="snd">${esc(m.sender)}</div>`:''}${media}${txt}<span class="tm">${esc(timeOf(m.ts))}</span></div>`;
    box.appendChild(row); lastSender=m.sender;
  });
  if(scrollToIdx!=null){
    const el=box.querySelector(`.bub[data-i="${scrollToIdx}"]`);
    if(el){el.scrollIntoView({block:'center'});el.style.background='#0a3d34';setTimeout(()=>el.style.background='',1600);}
  } else box.scrollTop=box.scrollHeight;
}

/* image / video lightbox */
function lightbox(kind,src){const lb=$('#lb');lb.innerHTML=`<img src="${src}">`;lb.style.display='flex';}
$('#lb').onclick=()=>{$('#lb').style.display='none';$('#lb').innerHTML='';};

/* in-chat search with next/prev navigation */
function resetCs(){$('#cbar').hidden=true;$('#cs').value='';$('#hit').textContent='';marks=[];curMark=-1;}
function closeCs(){resetCs(); if(active>=0) renderMsgs(chats[active]);}
$('#csBtn').onclick=()=>{ if($('#cbar').hidden){$('#cbar').hidden=false;$('#cs').focus();} else closeCs(); };
$('#csClose').onclick=closeCs;
$('#prev').onclick=()=>nav(-1);
$('#next').onclick=()=>nav(1);
$('#cs').addEventListener('input',runCs);
$('#cs').addEventListener('keydown',e=>{
  if(e.key==='Enter'){e.preventDefault(); e.shiftKey?nav(-1):nav(1);}
  else if(e.key==='Escape') closeCs();
});
function runCs(){
  if(active<0) return;
  renderMsgs(chats[active]); marks=[]; curMark=-1;
  const q=$('#cs').value.trim(); if(!q){$('#hit').textContent='';return;}
  const ql=q.toLowerCase();
  $$('#msgs .bub .t').forEach(sp=>{
    if(sp.textContent.toLowerCase().includes(ql))
      sp.innerHTML=esc(sp.textContent).replace(new RegExp('('+reEsc(q)+')','ig'),'<mark>$1</mark>');
  });
  marks=$$('#msgs mark');
  if(!marks.length){$('#hit').textContent='0/0';return;}
  nav(1);
}
function nav(dir){
  if(!marks.length) return;
  if(curMark>=0 && marks[curMark]) marks[curMark].classList.remove('cur');
  curMark=(curMark+dir+marks.length)%marks.length;
  const mk=marks[curMark]; mk.classList.add('cur'); mk.scrollIntoView({block:'center'});
  $('#hit').textContent=`${curMark+1}/${marks.length}`;
}

/* global search across all chats */
$('#q').addEventListener('input', e=>{
  const q=e.target.value.trim().toLowerCase(); renderChatList(q); const g=$('#gres');
  if(q.length<2){g.hidden=true; return;}
  const hits=[];
  chats.forEach((c,ci)=>c.messages.forEach((m,mi)=>{
    if(m.text && m.text.toLowerCase().includes(q)) hits.push({ci,mi,name:c.name,sender:m.sender||'system',text:m.text});
  }));
  if(!hits.length){g.hidden=true; return;}
  g.hidden=false;
  g.innerHTML=hits.slice(0,300).map(h=>{
    const i=h.text.toLowerCase().indexOf(q);
    const snip=esc(h.text.substring(Math.max(0,i-24),i+50));
    return `<div class="g" data-ci="${h.ci}" data-mi="${h.mi}"><div class="c">${esc(h.name)} · ${esc(h.sender)}</div><div class="s">…${snip}…</div></div>`;
  }).join('') + (hits.length>300?`<div class="more">${hits.length-300} more matches…</div>`:'');
  $$('#gres .g').forEach(el=>el.onclick=()=>{g.hidden=true; $('#q').value=''; renderChatList(''); openChat(+el.dataset.ci,+el.dataset.mi);});
});

renderChatList('');
</script>
</body></html>
"""


def main():
    try:                                        # chat names contain emoji; keep console output safe
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Build a self-contained WhatsApp-Web-style HTML viewer from export zips.")
    ap.add_argument("folder", help="folder containing WhatsApp export .zip files")
    ap.add_argument("-o", "--out", default=None, help="output .html (default: <folder>/chat_viewer.html)")
    ap.add_argument("--media", choices=["embed", "folder"], default="embed",
                    help="embed: single self-contained .html (media as base64, capped by "
                         "--max-embed-mb). folder: one index.html + a media/ folder holding ALL "
                         "media at any size (recommended for a full export set).")
    ap.add_argument("--max-embed-mb", type=float, default=20.0,
                    help="embed mode only: embed media up to this size each (MB); 0 = everything")
    ap.add_argument("--owner", default="", help="your sender name (right-aligned as 'you')")
    args = ap.parse_args()
    default_name = "index.html" if args.media == "folder" else "chat_viewer.html"
    out = args.out or os.path.join(args.folder, default_name)
    sys.exit(build(args.folder, out, args.max_embed_mb, args.owner, mode=args.media))


if __name__ == "__main__":
    main()
