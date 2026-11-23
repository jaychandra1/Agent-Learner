// PQ web UI. Everything a person types and every answer is kept in this browser's
// localStorage and nowhere else. The backend stores nothing: each question is sent together
// with the earlier messages of that chat, read from this device.
(function () {
  "use strict";

  const CHATS_KEY = "pq.chats.v1";
  const SETTINGS_KEY = "pq.settings.v1";
  const CONTEXT_MESSAGES = 30;      // messages of the chat sent along with a question
  const CONTEXT_IMAGES = 4;         // pictures sent along: the newest ones of the chat
  const MAX_IMAGES = 4;             // pictures on one question
  const MAX_FILES = 3;              // text files on one question
  const IMAGE_SIDE = 1280;          // pictures are scaled down to this before they are kept or sent
  const FILE_CHARS = 12000;         // of one text file, the rest is cut

  const $ = function (id) { return document.getElementById(id); };
  const el = {
    app: $("app"), sidebar: $("sidebar"), scrim: $("scrim"), chatList: $("chatList"), search: $("search"),
    scroll: $("scroll"), welcome: $("welcome"), thread: $("thread"),
    attach: $("attach"), attachFile: $("attachFile"), attachments: $("attachments"), mode: $("mode"), modePill: $("modePill"),
    title: $("chatTitle"), composer: $("composer"), input: $("input"), send: $("send"), mic: $("mic"), toast: $("toast"),
    settings: $("settings"), theme: $("theme"), accessCode: $("accessCode"),
    storageInfo: $("storageInfo"), importFile: $("importFile"),
  };

  // ---------- storage (this device only) ----------

  function load(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return fallback;
    }
  }

  let memoryOnly = false;
  function save(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch (e) {
      if (!memoryOnly) {
        memoryOnly = true;
        toast("This browser's storage is full or blocked. New messages are not saved; delete old chats in Settings.", 7000);
      }
      return false;
    }
  }

  let chats = load(CHATS_KEY, []).filter(function (c) { return c && c.id && Array.isArray(c.messages); });
  let settings = Object.assign({ theme: "system", accessCode: "", mode: "chat" }, load(SETTINGS_KEY, {}));
  if (settings.mode !== "interview") settings.mode = "chat";
  let attached = [];                // what is waiting on the question being typed: {kind: "image", name, data} or {kind: "file", name, text}
  let currentId = null;
  let pending = null;               // {chatId, controller} while an answer is on its way
  let shown = { id: null, count: 0 };   // what the thread showed last, to animate only what is new

  // Pictures fill the browser's storage quickly. When it is full, the oldest pictures are
  // dropped (the chat text stays) until the chats fit again.
  function saveChats() {
    try {
      localStorage.setItem(CHATS_KEY, JSON.stringify(chats));
      return;
    } catch (e) { /* full, or blocked */ }
    const withImages = [];
    chats.slice().sort(function (x, y) { return x.updated - y.updated; }).forEach(function (c) {
      c.messages.forEach(function (m) { if (m.images && m.images.length) withImages.push(m); });
    });
    let freed = 0;
    while (withImages.length > 1) {              // the newest picture message is kept
      const m = withImages.shift();
      m.imagesDropped = (m.imagesDropped || 0) + m.images.length;
      freed += m.images.length;
      delete m.images;
      try {
        localStorage.setItem(CHATS_KEY, JSON.stringify(chats));
        toast(freed + " older picture(s) were removed from this device to make room. The chat text is kept.", 6000);
        return;
      } catch (e) { /* still too big */ }
    }
    save(CHATS_KEY, chats);                      // says that storage is full
  }
  function currentChat() { return chats.find(function (c) { return c.id === currentId; }) || null; }
  function newId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

  // ---------- backend ----------

  // Where questions go. Empty in the page's config means this site's own address: the host
  // then passes them on (api/chat.js) and the backend's address never reaches the browser.
  // It is never shown anywhere in the page, and people cannot change it.
  function backendUrl() {
    const configured = (window.PQ_CONFIG && window.PQ_CONFIG.BACKEND_URL) || "";
    return (configured || window.location.origin).replace(/\/+$/, "");
  }

  async function post(path, messages, signal) {
    const headers = { "Content-Type": "application/json" };
    if (settings.accessCode) headers["X-Access-Code"] = settings.accessCode;
    try {
      return await fetch(backendUrl() + path, {
        method: "POST", headers: headers, signal: signal,
        body: JSON.stringify({ messages: messages, mode: settings.mode }),
      });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      throw new Error("Can't reach PQ right now. Check your connection and try again. " +
                      "A sleeping service can take a minute to wake up.");
    }
  }

  async function failure(response) {
    let data = null;
    try { data = await response.json(); } catch (e) { /* not JSON */ }
    const detail = data && typeof data.detail === "string" ? data.detail : "";
    const error = new Error(detail || ("PQ answered with an error (" + response.status + ")."));
    error.status = response.status;
    return error;
  }

  // The whole answer in one piece. Used when the backend can't stream.
  async function askWhole(messages, signal) {
    const response = await post("/api/chat", messages, signal);
    if (!response.ok) throw await failure(response);
    const data = await response.json();
    if (data && typeof data.answer === "string") return data.answer;
    throw new Error("PQ answered with something unexpected.");
  }

  // The answer while it is written. onText gets everything written so far, again and again.
  // The server sends events: data: {"delta": "..."} ... data: {"done": true, "answer": "..."}
  async function ask(messages, signal, onText) {
    const response = await post("/api/stream", messages, signal);
    if (response.status === 404 || response.status === 405) return askWhole(messages, signal);   // an older backend
    if (!response.ok) throw await failure(response);
    if (!response.body || !response.body.getReader) return askWhole(messages, signal);           // a very old browser

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let text = "";
    for (;;) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
      const events = buffer.split(/\r?\n\r?\n/);
      buffer = events.pop();                       // the last one may be half an event
      for (const event of events) {
        const line = event.split(/\r?\n/).find(function (l) { return l.startsWith("data:"); });
        if (!line) continue;
        let data;
        try { data = JSON.parse(line.slice(5)); } catch (e) { continue; }
        if (typeof data.delta === "string") {
          text += data.delta;
          onText(text);
        } else if (data.done) {
          return typeof data.answer === "string" ? data.answer : text;
        } else if (data.error) {
          throw new Error(String(data.error));
        }
      }
      if (chunk.done) break;
    }
    if (text.trim()) return text;                  // the connection ended without the last event
    throw new Error("PQ stopped before it answered. Please try again.");
  }

  // ---------- rendering ----------

  function clock(ts) {
    const d = new Date(ts);
    const today = new Date().toDateString() === d.toDateString();
    return today ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
                 : d.toLocaleDateString([], { day: "numeric", month: "short" });
  }

  function group(ts) {
    const days = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(ts).setHours(0, 0, 0, 0)) / 86400000);
    return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days < 7 ? "Previous 7 days" : days < 30 ? "Previous 30 days" : "Older";
  }

  function renderList() {
    const query = el.search.value.trim().toLowerCase();
    const shown = chats.slice().sort(function (a, b) { return b.updated - a.updated; }).filter(function (c) {
      return !query || c.title.toLowerCase().includes(query) ||
        c.messages.some(function (m) { return m.content.toLowerCase().includes(query); });
    });
    el.chatList.textContent = "";
    if (!shown.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = query ? "No chat matches." : "No chats yet.";
      el.chatList.appendChild(empty);
      return;
    }
    let lastGroup = "";
    shown.forEach(function (chat) {
      const g = group(chat.updated);
      if (g !== lastGroup) {
        const label = document.createElement("div");
        label.className = "group";
        label.textContent = lastGroup = g;
        el.chatList.appendChild(label);
      }
      const row = document.createElement("div");
      row.className = "chat-row" + (chat.id === currentId ? " active" : "");
      const open = document.createElement("button");
      open.className = "chat-open";
      open.textContent = chat.title;
      open.title = chat.title;
      open.addEventListener("click", function () { openChat(chat.id); });
      const rename = iconButton("Rename chat", '<path d="M4 20h4L19 9l-4-4L4 16v4z"/>', function () { renameChat(chat.id); });
      const remove = iconButton("Delete chat", '<path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/>', function () { deleteChat(chat.id); });
      row.append(open, rename, remove);
      el.chatList.appendChild(row);
    });
  }

  function iconButton(label, path, onClick) {
    const b = document.createElement("button");
    b.className = "row-btn";
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.title = label;
    b.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + path + "</svg>";
    b.addEventListener("click", function (e) { e.stopPropagation(); onClick(); });
    return b;
  }

  function messageNode(message, index) {
    const wrap = document.createElement("article");
    wrap.className = "msg " + message.role + (message.error ? " error" : "");
    if (message.role === "user") {
      const stack = document.createElement("div");
      stack.className = "user-stack";
      if ((message.images && message.images.length) || message.imagesDropped || (message.files && message.files.length)) {
        const extras = document.createElement("div");
        extras.className = "sent-extras";
        (message.images || []).forEach(function (data) {
          const img = document.createElement("img");
          img.src = data;
          img.alt = "Attached image";
          img.loading = "lazy";
          img.decoding = "async";
          extras.appendChild(img);
        });
        if (message.imagesDropped) extras.appendChild(fileChip(message.imagesDropped + " picture(s), no longer stored"));
        (message.files || []).forEach(function (f) { extras.appendChild(fileChip(f.name)); });
        stack.appendChild(extras);
      }
      if (message.content) {
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        bubble.textContent = message.content;
        stack.appendChild(bubble);
      }
      wrap.appendChild(stack);
      return wrap;
    }
    const avatar = document.createElement("span");
    avatar.className = "logo small";
    avatar.textContent = "PQ";
    avatar.setAttribute("aria-hidden", "true");
    const body = document.createElement("div");
    body.className = "body";
    const content = document.createElement("div");
    content.className = "content";
    if (message.error) content.textContent = message.content;
    else content.innerHTML = window.PQMarkdown.render(message.content);
    body.appendChild(content);

    const actions = document.createElement("div");
    actions.className = "msg-actions";
    if (message.error) {
      actions.appendChild(textButton("Try again", function () { retry(index); }));
      if (message.status === 401) actions.appendChild(textButton("Enter access code", openSettings));
    } else {
      actions.appendChild(textButton("Copy", function (e) { copy(message.content, e.currentTarget); }));
      const time = document.createElement("span");
      time.className = "time";
      time.textContent = clock(message.ts);
      actions.appendChild(time);
    }
    body.appendChild(actions);
    wrap.append(avatar, body);
    return wrap;
  }

  function fileChip(name) {
    const chip = document.createElement("span");
    chip.className = "file-chip";
    chip.textContent = name;
    return chip;
  }

  function textButton(label, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "text-btn";
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function renderThread(scrollToEnd) {
    const chat = currentChat();
    const messages = chat ? chat.messages : [];
    el.title.textContent = chat ? chat.title : "New chat";
    document.title = chat ? chat.title + " - PQ" : "PQ";
    el.welcome.hidden = messages.length > 0;
    el.thread.textContent = "";
    messages.forEach(function (m, i) { el.thread.appendChild(messageNode(m, i)); });
    if (pending && chat && pending.chatId === chat.id) {
      pending.live = null;
      if (pending.text) {
        el.thread.appendChild(liveNode());
      } else {
        const wait = document.createElement("article");
        wait.className = "msg assistant waiting";
        wait.innerHTML = '<span class="logo small" aria-hidden="true">PQ</span><div class="body"><div class="typing" ' +
          'aria-label="PQ is thinking"><span></span><span></span><span></span></div></div>';
        el.thread.appendChild(wait);
      }
    }
    // only a message that was just added slides in; opening a chat shows it at once
    const count = el.thread.children.length;
    const id = chat ? chat.id : null;
    if (id && id === shown.id && count > shown.count && el.thread.lastElementChild) {
      el.thread.lastElementChild.classList.add("enter");
      if (count - shown.count > 1) el.thread.children[count - 2].classList.add("enter");
    }
    shown = { id: id, count: count };
    setBusy(Boolean(pending));
    if (scrollToEnd !== false) el.scroll.scrollTop = el.scroll.scrollHeight;
  }

  // The message PQ is writing right now. Only its text changes while pieces arrive.
  function liveNode() {
    const wrap = document.createElement("article");
    wrap.className = "msg assistant streaming";
    wrap.innerHTML = '<span class="logo small" aria-hidden="true">PQ</span><div class="body"><div class="content"></div></div>';
    pending.live = wrap.querySelector(".content");
    pending.live.innerHTML = window.PQMarkdown.render(pending.text);
    return wrap;
  }

  // Called for every piece of text. Draws at most once per frame, and keeps the view at the
  // end only while the person has not scrolled up to read.
  function paintLive() {
    if (!pending || pending.frame) return;
    pending.frame = requestAnimationFrame(function () {
      if (!pending) return;
      pending.frame = 0;
      if (pending.chatId !== currentId) return;          // another chat is open: it is drawn when they come back
      const atEnd = el.scroll.scrollHeight - el.scroll.scrollTop - el.scroll.clientHeight < 140;
      if (pending.live && pending.live.isConnected) {
        pending.live.innerHTML = window.PQMarkdown.render(pending.text);
      } else {
        const wait = el.thread.querySelector(".waiting");
        if (wait) wait.replaceWith(liveNode());
        else el.thread.appendChild(liveNode());
        shown.count = el.thread.children.length;
      }
      if (atEnd) el.scroll.scrollTop = el.scroll.scrollHeight;
    });
  }

  function setBusy(busy) {
    el.send.classList.toggle("busy", busy);
    el.send.setAttribute("aria-label", busy ? "Stop" : "Send");
    el.send.disabled = !busy && !el.input.value.trim() && !attached.length;
  }

  // ---------- chats ----------

  function openChat(id) {
    currentId = id;
    renderList();
    renderThread();
    closeSidebar();
    el.input.focus();
  }

  function startNewChat() {
    currentId = null;
    renderList();
    renderThread();
    closeSidebar();
    el.input.focus();
  }

  function renameChat(id) {
    const chat = chats.find(function (c) { return c.id === id; });
    const name = chat && window.prompt("Name of this chat", chat.title);
    if (!name || !name.trim()) return;
    chat.title = name.trim().slice(0, 80);
    saveChats();
    renderList();
    if (id === currentId) renderThread(false);
  }

  function deleteChat(id) {
    const chat = chats.find(function (c) { return c.id === id; });
    if (!chat || !window.confirm('Delete "' + chat.title + '"? It is only stored on this device and cannot be recovered.')) return;
    if (pending && pending.chatId === id) pending.controller.abort();
    chats = chats.filter(function (c) { return c.id !== id; });
    saveChats();
    if (id === currentId) currentId = null;
    renderList();
    renderThread();
  }

  function titleFrom(text) {
    const line = text.trim().split("\n")[0].replace(/\s+/g, " ");
    return line.length > 48 ? line.slice(0, 47).trimEnd() + "…" : line || "New chat";
  }

  // ---------- asking ----------

  // The memory of a chat is what this device holds: the last messages are sent with the question.
  // Text files travel as text inside the question; pictures as pictures, the newest few only.
  function contextOf(chat) {
    const recent = chat.messages.filter(function (m) { return !m.error; }).slice(-CONTEXT_MESSAGES);
    let room = CONTEXT_IMAGES;
    const out = [];
    for (let i = recent.length - 1; i >= 0; i--) {
      const m = recent[i];
      let content = m.content || "";
      (m.files || []).forEach(function (f) {
        content += (content ? "\n\n" : "") + "File `" + f.name + "`:\n```\n" + f.text + "\n```";
      });
      const item = { role: m.role, content: content };
      if (m.images && m.images.length && room > 0) {
        item.images = m.images.slice(-room);
        room -= item.images.length;
      }
      out.unshift(item);
    }
    return out;
  }

  async function submit(text, extras) {
    if (pending) return;
    extras = extras || [];
    const images = extras.filter(function (x) { return x.kind === "image"; }).map(function (x) { return x.data; });
    const files = extras.filter(function (x) { return x.kind === "file"; })
      .map(function (x) { return { name: x.name, text: x.text }; });
    let chat = currentChat();
    if (!chat) {
      const name = text || (files[0] && files[0].name) || "Image";
      chat = { id: newId(), title: titleFrom(name), created: Date.now(), updated: Date.now(), messages: [] };
      chats.push(chat);
      currentId = chat.id;
    }
    chat.messages = chat.messages.filter(function (m) { return !m.error; });   // an old error is not part of the chat
    const message = { role: "user", content: text, ts: Date.now() };
    if (images.length) message.images = images;
    if (files.length) message.files = files;
    chat.messages.push(message);
    chat.updated = Date.now();
    saveChats();
    await answer(chat);
  }

  async function answer(chat) {
    const controller = new AbortController();
    const mine = pending = { chatId: chat.id, controller: controller, text: "", live: null, frame: 0 };
    renderList();
    renderThread();
    try {
      const text = await ask(contextOf(chat), controller.signal, function (soFar) {
        mine.text = soFar;
        paintLive();
      });
      chat.messages.push({ role: "assistant", content: text, ts: Date.now() });
    } catch (e) {
      if (mine.text.trim()) {
        // Stop was pressed, or the connection broke, after part of the answer arrived: keep that part
        chat.messages.push({ role: "assistant", content: mine.text.trim(), ts: Date.now() });
        if (e.name !== "AbortError") toast("The answer was interrupted. Ask \"continue\" for the rest.", 5000);
      } else if (e.name !== "AbortError") {
        chat.messages.push({ role: "assistant", content: e.message, ts: Date.now(), error: true, status: e.status || 0 });
      }
    }
    if (mine.frame) cancelAnimationFrame(mine.frame);
    pending = null;
    chat.updated = Date.now();
    saveChats();
    renderList();
    if (chat.id === currentId) renderThread();
  }

  function retry(index) {
    const chat = currentChat();
    if (!chat || pending) return;
    chat.messages.splice(index, 1);   // drop the error, ask the same question again
    if (!chat.messages.length || chat.messages[chat.messages.length - 1].role !== "user") {
      saveChats();
      renderThread();
      return;
    }
    answer(chat);
  }

  // ---------- small things ----------

  let toastTimer = 0;
  function toast(text, ms) {
    el.toast.textContent = text;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.toast.hidden = true; }, ms || 2500);
  }

  async function copy(text, button) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    if (button) {
      const label = button.textContent;
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = label; }, 1500);
    }
  }

  function applyTheme() {
    if (settings.theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", settings.theme);
  }

  function openSidebar() { el.app.classList.add("menu-open"); el.scrim.hidden = false; }
  function closeSidebar() { el.app.classList.remove("menu-open"); el.scrim.hidden = true; }

  function growInput() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 200) + "px";
    setBusy(Boolean(pending));
  }

  // ---------- pictures and files on a question ----------
  // Pictures are scaled down in the browser and kept, like the chat, on this device. Text
  // files (code, logs, configuration) are read here and sent as text inside the question.

  function renderAttached() {
    el.attachments.textContent = "";
    el.attachments.hidden = !attached.length;
    attached.forEach(function (item, i) {
      const box = document.createElement("div");
      box.className = "attached " + item.kind;
      if (item.kind === "image") {
        const img = document.createElement("img");
        img.src = item.data;
        img.alt = item.name;
        box.appendChild(img);
      } else {
        const name = document.createElement("span");
        name.textContent = item.name;
        box.appendChild(name);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.setAttribute("aria-label", "Remove " + item.name);
      remove.textContent = "×";
      remove.addEventListener("click", function () { attached.splice(i, 1); renderAttached(); });
      box.appendChild(remove);
      el.attachments.appendChild(box);
    });
    setBusy(Boolean(pending));
  }

  function shrinkImage(file) {
    return new Promise(function (resolve, reject) {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = function () {
        const scale = Math.min(1, IMAGE_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#fff";                    // JPEG has no transparency
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        resolve(canvas.toDataURL("image/jpeg", 0.85));
      };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("not an image")); };
      img.src = url;
    });
  }

  function isTextFile(file) {
    return file.type.startsWith("text/") || /(json|xml|yaml|javascript|typescript|x-sh|sql|toml)/.test(file.type) ||
      /\.(txt|md|log|json|ya?ml|toml|ini|env|xml|csv|sql|sh|ps1|bat|py|m?js|tsx?|jsx|java|kt|go|rs|c|h|cpp|cs|rb|php|swift|scala|tf|html|css|vue|dockerfile|gradle|properties|conf)$/i.test(file.name) ||
      /^(dockerfile|makefile|jenkinsfile)$/i.test(file.name);
  }

  async function addFiles(list) {
    for (const file of Array.from(list || [])) {
      const images = attached.filter(function (x) { return x.kind === "image"; }).length;
      const files = attached.length - images;
      if (/^image\/(png|jpeg|webp|gif)$/.test(file.type)) {
        if (images >= MAX_IMAGES) { toast("At most " + MAX_IMAGES + " pictures on one question."); continue; }
        try {
          attached.push({ kind: "image", name: file.name || "image", data: await shrinkImage(file) });
        } catch (e) {
          toast("That picture could not be read.");
        }
      } else if (isTextFile(file)) {
        if (files >= MAX_FILES) { toast("At most " + MAX_FILES + " files on one question."); continue; }
        let text = await file.text();
        if (text.indexOf("\u0000") !== -1) { toast(file.name + " is not a text file."); continue; }
        if (text.length > FILE_CHARS) text = text.slice(0, FILE_CHARS) + "\n... (cut: the file is longer)";
        attached.push({ kind: "file", name: file.name, text: text });
      } else {
        toast("PQ reads pictures and text files (code, logs, configuration). " + (file.name || "That file") + " is neither.", 5000);
      }
      renderAttached();
    }
  }

  el.attach.addEventListener("click", function () { el.attachFile.click(); });
  el.attachFile.addEventListener("change", function () {
    addFiles(el.attachFile.files);
    el.attachFile.value = "";
  });
  el.input.addEventListener("paste", function (e) {        // a screenshot from the clipboard
    const files = Array.from((e.clipboardData && e.clipboardData.files) || [])
      .filter(function (f) { return f.type.startsWith("image/"); });
    if (!files.length) return;
    e.preventDefault();
    addFiles(files);
  });

  // ---------- chat or interview ----------

  function applyMode() {
    const interview = settings.mode === "interview";
    el.modePill.hidden = !interview;
    el.input.placeholder = interview ? "Ask an interview question" : "Ask a technical question";
  }

  // ---------- speaking a question ----------
  // The browser's own speech recognition turns speech into text in the question box. PQ's
  // backend is not involved and gets only the text that is sent afterwards. Nothing is sent
  // on its own: the person reads what was heard, corrects it, and presses Send.
  // (How the browser recognises speech is up to the browser: Chrome and Edge use their
  // vendor's online service for it.) Browsers without it don't show the button.

  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let listening = null;             // {recognition, before, heard} while the microphone is on

  function stopListening() {
    if (listening) listening.recognition.stop();
  }

  function startListening() {
    const recognition = new Recognition();
    recognition.lang = document.documentElement.lang === "en" ? (navigator.language || "en-US") : document.documentElement.lang;
    recognition.continuous = true;      // keep listening through pauses, until the button is pressed again
    recognition.interimResults = true;  // show the words while they are spoken
    const before = el.input.value.trim();
    const mine = listening = { recognition: recognition, before: before, heard: "" };

    recognition.onresult = function (e) {
      if (mine.sent) return;            // the question went out already: late words don't refill the box
      let fixed = "";
      let unsure = "";
      for (let i = 0; i < e.results.length; i++) {
        if (e.results[i].isFinal) fixed += e.results[i][0].transcript;
        else unsure += e.results[i][0].transcript;
      }
      mine.heard = (fixed + unsure).replace(/\s+/g, " ").trim();
      el.input.value = (before ? before + " " : "") + mine.heard;
      growInput();
      el.input.scrollTop = el.input.scrollHeight;
    };
    recognition.onerror = function (e) {
      const why = {
        "not-allowed": "The microphone is blocked for this site. Allow it in the browser's address bar and try again.",
        "service-not-allowed": "Speech recognition is switched off in this browser.",
        "audio-capture": "No microphone was found.",
        "network": "Speech recognition needs an internet connection.",
        "no-speech": "I didn't hear anything. Press the microphone and speak.",
      }[e.error];
      if (why) toast(why, 5000);
    };
    recognition.onend = function () {
      if (listening === mine) listening = null;
      el.mic.classList.remove("listening");
      el.mic.setAttribute("aria-pressed", "false");
      el.mic.setAttribute("aria-label", "Speak your question");
      el.mic.title = "Speak your question";
      applyMode();
      growInput();
      if (mine.heard) el.input.focus();
    };

    try {
      recognition.start();
    } catch (e) {
      listening = null;
      toast("The microphone could not be started.");
      return;
    }
    el.mic.classList.add("listening");
    el.mic.setAttribute("aria-pressed", "true");
    el.mic.setAttribute("aria-label", "Stop listening");
    el.mic.title = "Stop listening";
    el.input.placeholder = "Listening… speak your question";
  }

  if (Recognition && el.mic) {
    el.mic.hidden = false;
    el.mic.addEventListener("click", function () {
      if (listening) stopListening();
      else startListening();
    });
  }

  // ---------- settings ----------

  function openSettings() {
    el.theme.value = settings.theme;
    el.accessCode.value = settings.accessCode;
    el.mode.value = settings.mode;
    let bytes = 0;
    try { bytes = (localStorage.getItem(CHATS_KEY) || "").length; } catch (e) { /* blocked */ }
    el.storageInfo.textContent = chats.length + (chats.length === 1 ? " chat, " : " chats, ") +
      (bytes / 1024).toFixed(0) + " KB, stored in this browser only. Clearing the browser's site data deletes them.";
    closeSidebar();
    el.settings.showModal();
  }

  function exportChats() {
    const blob = new Blob([JSON.stringify({ app: "pq", version: 1, exported: new Date().toISOString(), chats: chats }, null, 2)],
                          { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "pq-chats-" + new Date().toISOString().slice(0, 10) + ".json";
    link.click();
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
  }

  function importChats(file) {
    const reader = new FileReader();
    reader.onload = function () {
      try {
        const data = JSON.parse(String(reader.result));
        const incoming = (Array.isArray(data) ? data : data.chats || []).filter(function (c) {
          return c && typeof c.title === "string" && Array.isArray(c.messages) && c.messages.every(function (m) {
            return m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string";
          });
        });
        if (!incoming.length) throw new Error("no chats");
        const known = new Set(chats.map(function (c) { return c.id; }));
        let added = 0;
        incoming.forEach(function (c) {
          if (c.id && known.has(c.id)) return;
          chats.push({ id: c.id || newId(), title: c.title.slice(0, 80), created: c.created || Date.now(),
                       updated: c.updated || Date.now(), messages: c.messages });
          added++;
        });
        saveChats();
        renderList();
        toast(added ? added + " chat(s) imported." : "Those chats are already here.");
      } catch (e) {
        toast("That file is not a PQ chat export.");
      }
    };
    reader.readAsText(file);
  }

  // ---------- events ----------

  el.composer.addEventListener("submit", function (e) {
    e.preventDefault();
    if (pending) { pending.controller.abort(); return; }   // the button is "Stop" while waiting
    if (listening) listening.sent = true;
    stopListening();
    const text = el.input.value.trim();
    if (!text && !attached.length) return;
    const extras = attached;
    attached = [];
    el.input.value = "";
    renderAttached();
    growInput();
    submit(text, extras);
  });
  el.input.addEventListener("input", growInput);
  el.input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!pending) el.composer.requestSubmit();
    }
  });
  el.thread.addEventListener("click", function (e) {
    const button = e.target.closest(".copy-code");
    if (button) copy(button.closest(".code").querySelector("code").textContent, button);
  });
  $("newChat").addEventListener("click", startNewChat);
  $("newChatTop").addEventListener("click", startNewChat);
  $("openSidebar").addEventListener("click", function () {
    if (window.matchMedia("(max-width: 860px)").matches) openSidebar();
    else el.app.classList.toggle("menu-hidden");
  });
  $("closeSidebar").addEventListener("click", closeSidebar);
  el.scrim.addEventListener("click", closeSidebar);
  el.search.addEventListener("input", renderList);
  $("openSettings").addEventListener("click", openSettings);
  $("cancelSettings").addEventListener("click", function () { el.settings.close(); });
  $("settingsForm").addEventListener("submit", function () {
    settings = { theme: el.theme.value, accessCode: el.accessCode.value.trim(), mode: el.mode.value };
    save(SETTINGS_KEY, settings);
    applyTheme();
    applyMode();
    toast("Settings saved.");
  });
  $("exportChats").addEventListener("click", exportChats);
  $("importChats").addEventListener("click", function () { el.importFile.click(); });
  el.importFile.addEventListener("change", function () {
    if (el.importFile.files[0]) importChats(el.importFile.files[0]);
    el.importFile.value = "";
  });
  $("clearChats").addEventListener("click", function () {
    if (!chats.length || !window.confirm("Delete all " + chats.length + " chats from this device? This cannot be undone.")) return;
    if (pending) pending.controller.abort();
    chats = [];
    currentId = null;
    saveChats();
    renderList();
    renderThread();
    el.settings.close();
    toast("All chats deleted.");
  });
  window.addEventListener("storage", function (e) {   // another tab changed the chats
    if (e.key !== CHATS_KEY || pending) return;
    chats = load(CHATS_KEY, []);
    if (!currentChat()) currentId = null;
    renderList();
    renderThread(false);
  });

  // ---------- start ----------

  applyMode();

  // A backend on a plan that sleeps when idle needs up to a minute to start. Wake it now,
  // while the person is still typing, and keep it awake while the page is open and visible.
  function wake() {
    if (document.visibilityState !== "visible") return;
    fetch(backendUrl() + "/api/health", { cache: "no-store" }).catch(function () { /* the question will say so */ });
  }
  wake();
  setInterval(wake, 9 * 60 * 1000);
  document.addEventListener("visibilitychange", wake);
  applyTheme();
  renderList();
  renderThread();
  growInput();
  el.input.focus();
})();
