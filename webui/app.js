// PQ web UI. Everything a person types and every answer is kept in this browser's
// localStorage and nowhere else. The backend stores nothing: each question is sent together
// with the earlier messages of that chat, read from this device.
(function () {
  "use strict";

  const CHATS_KEY = "pq.chats.v1";
  const SETTINGS_KEY = "pq.settings.v1";
  const CONTEXT_MESSAGES = 30;      // messages of the chat sent along with a question
  const SUGGESTIONS = [
    "What is a Kubernetes operator, with a real-time scenario?",
    "Explain how HTTPS works, step by step",
    "Kafka vs RabbitMQ: when do I choose which?",
    "Quiz me on SQL joins",
  ];

  const $ = function (id) { return document.getElementById(id); };
  const el = {
    app: $("app"), sidebar: $("sidebar"), scrim: $("scrim"), chatList: $("chatList"), search: $("search"),
    scroll: $("scroll"), welcome: $("welcome"), thread: $("thread"), suggestions: $("suggestions"),
    title: $("chatTitle"), composer: $("composer"), input: $("input"), send: $("send"), toast: $("toast"),
    settings: $("settings"), theme: $("theme"), accessCode: $("accessCode"), backendUrl: $("backendUrl"),
    backendHint: $("backendHint"), storageInfo: $("storageInfo"), importFile: $("importFile"),
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
  let settings = Object.assign({ theme: "system", accessCode: "", backendUrl: "" }, load(SETTINGS_KEY, {}));
  let currentId = null;
  let pending = null;               // {chatId, controller} while an answer is on its way

  function saveChats() { save(CHATS_KEY, chats); }
  function currentChat() { return chats.find(function (c) { return c.id === currentId; }) || null; }
  function newId() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

  // ---------- backend ----------

  function backendUrl() {
    const configured = (window.PQ_CONFIG && window.PQ_CONFIG.BACKEND_URL) || "";
    return (settings.backendUrl || configured || window.location.origin).replace(/\/+$/, "");
  }

  async function ask(messages, signal) {
    const headers = { "Content-Type": "application/json" };
    if (settings.accessCode) headers["X-Access-Code"] = settings.accessCode;
    let response;
    try {
      response = await fetch(backendUrl() + "/api/chat", {
        method: "POST", headers: headers, signal: signal, body: JSON.stringify({ messages: messages }),
      });
    } catch (e) {
      if (e.name === "AbortError") throw e;
      throw new Error("Can't reach PQ at " + backendUrl() + ". Check your connection, or the Backend URL in Settings. " +
                      "A sleeping free service can take a minute to wake up.");
    }
    let data = null;
    try { data = await response.json(); } catch (e) { /* not JSON */ }
    if (response.ok && data && typeof data.answer === "string") return data.answer;
    const detail = data && typeof data.detail === "string" ? data.detail : "";
    const error = new Error(detail || ("PQ answered with an error (" + response.status + ")."));
    error.status = response.status;
    throw error;
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
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = message.content;
      wrap.appendChild(bubble);
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
      const wait = document.createElement("article");
      wait.className = "msg assistant waiting";
      wait.innerHTML = '<span class="logo small" aria-hidden="true">PQ</span><div class="body"><div class="typing" ' +
        'aria-label="PQ is writing"><span></span><span></span><span></span></div>' +
        '<p class="wait-note">Writing a full explanation can take up to a minute.</p></div>';
      el.thread.appendChild(wait);
    }
    setBusy(Boolean(pending));
    if (scrollToEnd !== false) el.scroll.scrollTop = el.scroll.scrollHeight;
  }

  function setBusy(busy) {
    el.send.classList.toggle("busy", busy);
    el.send.setAttribute("aria-label", busy ? "Stop" : "Send");
    el.send.disabled = !busy && !el.input.value.trim();
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
  function contextOf(chat) {
    return chat.messages.filter(function (m) { return !m.error; }).slice(-CONTEXT_MESSAGES)
      .map(function (m) { return { role: m.role, content: m.content }; });
  }

  async function submit(text) {
    if (pending) return;
    let chat = currentChat();
    if (!chat) {
      chat = { id: newId(), title: titleFrom(text), created: Date.now(), updated: Date.now(), messages: [] };
      chats.push(chat);
      currentId = chat.id;
    }
    chat.messages = chat.messages.filter(function (m) { return !m.error; });   // an old error is not part of the chat
    chat.messages.push({ role: "user", content: text, ts: Date.now() });
    chat.updated = Date.now();
    saveChats();
    await answer(chat);
  }

  async function answer(chat) {
    const controller = new AbortController();
    pending = { chatId: chat.id, controller: controller };
    renderList();
    renderThread();
    try {
      const text = await ask(contextOf(chat), controller.signal);
      chat.messages.push({ role: "assistant", content: text, ts: Date.now() });
    } catch (e) {
      if (e.name !== "AbortError") {
        chat.messages.push({ role: "assistant", content: e.message, ts: Date.now(), error: true, status: e.status || 0 });
      }
    }
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

  // ---------- settings ----------

  function openSettings() {
    el.theme.value = settings.theme;
    el.accessCode.value = settings.accessCode;
    el.backendUrl.value = settings.backendUrl;
    const configured = (window.PQ_CONFIG && window.PQ_CONFIG.BACKEND_URL) || "";
    el.backendHint.textContent = configured ? "Leave empty to use " + configured + "." : "Where your PQ backend runs.";
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
    const text = el.input.value.trim();
    if (!text) return;
    el.input.value = "";
    growInput();
    submit(text);
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
    settings = { theme: el.theme.value, accessCode: el.accessCode.value.trim(), backendUrl: el.backendUrl.value.trim() };
    save(SETTINGS_KEY, settings);
    applyTheme();
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

  SUGGESTIONS.forEach(function (text) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = text;
    chip.addEventListener("click", function () { submit(text); });
    el.suggestions.appendChild(chip);
  });
  applyTheme();
  renderList();
  renderThread();
  growInput();
  el.input.focus();
})();
