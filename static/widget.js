/*!
 * Support Bot widget. Sin dependencias.
 *
 * <script src="https://soporte.midominio.com/widget.js"
 *         data-token-endpoint="/support/token"
 *         data-title="Soporte"
 *         data-primary-color="#4f46e5"
 *         data-position="right"
 *         data-locale="es"
 *         defer></script>
 *
 * `data-token-endpoint` es una URL de TU app (mismo origen, con la sesión del usuario) que
 * devuelve `{"token": "<JWT>"}`. El widget la llama al abrirse, poco antes de que el token
 * caduque y cuando el servicio responde 401.
 */
(() => {
  "use strict";

  const script = document.currentScript;
  if (!script || window.__supportBotLoaded) return;
  window.__supportBotLoaded = true;

  const config = {
    tokenEndpoint: script.dataset.tokenEndpoint,
    apiUrl: (script.dataset.apiUrl || new URL(script.src).origin).replace(/\/$/, ""),
    title: script.dataset.title,
    primary: script.dataset.primaryColor || "#4f46e5",
    primaryText: script.dataset.primaryTextColor || "#ffffff",
    position: script.dataset.position === "left" ? "left" : "right",
    // Idioma: data-locale, si no el `locale` del JWT y, mientras no hay token, el del navegador.
    locale: script.dataset.locale,
  };
  if (!config.tokenEndpoint) {
    console.error("[support-bot] Falta data-token-endpoint en el <script>.");
    return;
  }

  const STRINGS = {
    es: {
      title: "Soporte",
      open: "Abrir el chat de soporte",
      close: "Cerrar",
      placeholder: "Escribe tu pregunta…",
      send: "Enviar",
      human: "Hablar con una persona",
      humanWaiting: "Te hemos pasado con una persona del equipo. Te responderá aquí mismo.",
      humanClosed: "La conversación con el equipo ha terminado. Puedes seguir escribiendo al asistente.",
      agent: "Equipo de soporte",
      you: "Tú",
      bot: "Asistente",
      useful: "Útil",
      notUseful: "No útil",
      error: "No se ha podido conectar con el soporte. Inténtalo de nuevo.",
      rateLimited: "Has enviado muchos mensajes seguidos. Espera unos segundos.",
      empty: "¡Hola! ¿En qué podemos ayudarte?",
    },
    en: {
      title: "Support",
      open: "Open support chat",
      close: "Close",
      placeholder: "Type your question…",
      send: "Send",
      human: "Talk to a person",
      humanWaiting: "You've been transferred to our team. They will reply right here.",
      humanClosed: "The conversation with our team has ended. You can keep chatting with the assistant.",
      agent: "Support team",
      you: "You",
      bot: "Assistant",
      useful: "Helpful",
      notUseful: "Not helpful",
      error: "Couldn't reach support. Please try again.",
      rateLimited: "You've sent many messages in a row. Please wait a few seconds.",
      empty: "Hi! How can we help you?",
    },
  };
  const pickStrings = (locale) => STRINGS[(locale || "").slice(0, 2).toLowerCase()];
  let t = pickStrings(config.locale) || pickStrings(navigator.language) || STRINGS.es;

  // -- Token ------------------------------------------------------------------------------

  let token = null;
  let tokenExp = 0;
  let tokenRequest = null;

  function decodeClaims(jwt) {
    try {
      const payload = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0));
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      return {};
    }
  }

  async function getToken(force = false) {
    if (!force && token && Date.now() < tokenExp - 60_000) return token;
    tokenRequest ||= fetch(config.tokenEndpoint, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    })
      .then((response) => {
        if (!response.ok) throw new Error(`token ${response.status}`);
        return response.json();
      })
      .then((body) => {
        token = body.token;
        const claims = decodeClaims(token);
        tokenExp = (claims.exp || 0) * 1000;
        if (!config.locale && pickStrings(claims.locale) && pickStrings(claims.locale) !== t) {
          t = pickStrings(claims.locale);
          applyStrings();
        }
        return token;
      })
      .finally(() => {
        tokenRequest = null;
      });
    return tokenRequest;
  }

  class ApiError extends Error {
    constructor(status, detail) {
      super(detail || `HTTP ${status}`);
      this.status = status;
    }
  }

  async function api(method, path, body, { stream = false, signal } = {}) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const response = await fetch(config.apiUrl + path, {
        method,
        signal,
        headers: {
          Authorization: `Bearer ${await getToken(attempt > 0)}`,
          ...(body ? { "Content-Type": "application/json" } : {}),
          Accept: stream ? "text/event-stream" : "application/json",
        },
        body: body ? JSON.stringify(body) : undefined,
      });
      if (response.status === 401 && attempt === 0) continue; // token caducado: renovar
      if (!response.ok) {
        const detail = await response.json().then((b) => b.detail, () => null);
        throw new ApiError(response.status, typeof detail === "string" ? detail : null);
      }
      return response;
    }
  }

  async function* readSse(response) {
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += value;
      let index;
      while ((index = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        const event = { event: "message", data: "" };
        for (const line of block.split("\n")) {
          if (line.startsWith(":")) continue;
          const [field, ...rest] = line.split(": ");
          event[field] = rest.join(": ");
        }
        if (event.data) yield { name: event.event, data: JSON.parse(event.data) };
      }
    }
  }

  // -- Interfaz ---------------------------------------------------------------------------

  const host = document.createElement("div");
  host.id = "support-bot-widget";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
      .launcher, .panel { position: fixed; ${config.position}: 20px; z-index: 2147483000; }
      .launcher { bottom: 20px; width: 56px; height: 56px; border-radius: 50%; border: 0;
        background: var(--sb-primary); color: var(--sb-primary-text); cursor: pointer;
        box-shadow: 0 6px 20px rgba(0,0,0,.2); display: grid; place-items: center; }
      .launcher svg { width: 26px; height: 26px; }
      .panel { bottom: 88px; width: min(380px, calc(100vw - 40px));
        height: min(600px, calc(100vh - 120px)); background: #fff; color: #111827;
        border-radius: 14px; box-shadow: 0 12px 40px rgba(0,0,0,.25); display: flex;
        flex-direction: column; overflow: hidden; font-size: 14px; line-height: 1.45; }
      .panel[hidden] { display: none; }
      header { background: var(--sb-primary); color: var(--sb-primary-text); padding: 12px 14px;
        display: flex; align-items: center; gap: 8px; }
      header h2 { font-size: 15px; margin: 0; flex: 1; font-weight: 600; }
      header button { background: rgba(255,255,255,.18); color: inherit; border: 0;
        border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 12px; }
      header button[hidden] { display: none; }
      .messages { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column;
        gap: 10px; background: #f9fafb; }
      .msg { max-width: 85%; padding: 9px 12px; border-radius: 12px; white-space: pre-wrap;
        overflow-wrap: anywhere; }
      .msg.user { align-self: flex-end; background: var(--sb-primary); color: var(--sb-primary-text);
        border-bottom-right-radius: 4px; }
      .msg.assistant, .msg.agent { align-self: flex-start; background: #fff;
        border: 1px solid #e5e7eb; border-bottom-left-radius: 4px; }
      .msg .author { display: block; font-size: 11px; color: #6b7280; margin-bottom: 2px; }
      .msg code { background: #f3f4f6; padding: 1px 4px; border-radius: 4px; font-size: 13px; }
      .msg a { color: var(--sb-primary); }
      .notice { align-self: center; text-align: center; font-size: 12px; color: #4b5563;
        background: #eef2ff; border-radius: 8px; padding: 8px 10px; max-width: 95%; }
      .notice button { margin-top: 6px; background: var(--sb-primary); color: var(--sb-primary-text);
        border: 0; border-radius: 6px; padding: 5px 10px; cursor: pointer; }
      .rating { display: flex; gap: 4px; margin-top: 6px; }
      .rating button { border: 1px solid #e5e7eb; background: #fff; border-radius: 6px;
        cursor: pointer; padding: 2px 6px; font-size: 13px; opacity: .7; }
      .rating button[aria-pressed="true"] { opacity: 1; border-color: var(--sb-primary); }
      .typing { display: inline-flex; gap: 4px; }
      .typing span { width: 6px; height: 6px; border-radius: 50%; background: #9ca3af;
        animation: sb-blink 1.2s infinite ease-in-out; }
      .typing span:nth-child(2) { animation-delay: .2s; }
      .typing span:nth-child(3) { animation-delay: .4s; }
      @keyframes sb-blink { 0%, 80%, 100% { opacity: .3; } 40% { opacity: 1; } }
      form { display: flex; gap: 8px; padding: 10px; border-top: 1px solid #e5e7eb; background: #fff; }
      textarea { flex: 1; resize: none; border: 1px solid #d1d5db; border-radius: 10px;
        padding: 9px 10px; font-size: 14px; max-height: 120px; color: #111827; }
      textarea:focus { outline: 2px solid var(--sb-primary); outline-offset: -1px; }
      form button { border: 0; border-radius: 10px; padding: 0 14px; cursor: pointer;
        background: var(--sb-primary); color: var(--sb-primary-text); font-weight: 600; }
      form button:disabled { opacity: .5; cursor: default; }
    </style>
    <button class="launcher" type="button" aria-expanded="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>
    </button>
    <section class="panel" role="dialog" hidden>
      <header>
        <h2></h2>
        <button type="button" class="human"></button>
        <button type="button" class="close">✕</button>
      </header>
      <div class="messages" aria-live="polite"></div>
      <form>
        <textarea rows="1"></textarea>
        <button type="submit"></button>
      </form>
    </section>`;
  host.style.setProperty("--sb-primary", config.primary);
  host.style.setProperty("--sb-primary-text", config.primaryText);

  const $ = (selector) => root.querySelector(selector);
  const launcher = $(".launcher");
  const panel = $(".panel");
  const list = $(".messages");
  const form = $("form");
  const input = $("textarea");
  const sendButton = $("form button");
  const humanButton = $(".human");
  function applyStrings() {
    $("header h2").textContent = config.title || t.title;
    panel.setAttribute("aria-label", config.title || t.title);
    launcher.setAttribute("aria-label", t.open);
    $(".close").setAttribute("aria-label", t.close);
    humanButton.textContent = t.human;
    input.placeholder = t.placeholder;
    input.setAttribute("aria-label", t.placeholder);
    sendButton.textContent = t.send;
  }
  applyStrings();

  // -- Estado -----------------------------------------------------------------------------

  let conversation = null; // {id, kind, status}
  let lastMessageId = 0;
  let busy = false;
  let eventsController = null;
  const rendered = new Set();

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  /** Markdown mínimo y seguro: se escapa todo y solo se interpretan **negrita**, `código` y enlaces http(s). */
  function renderMarkdown(text) {
    return escapeHtml(text)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function scrollDown() {
    list.scrollTop = list.scrollHeight;
  }

  function bubble(role, authorName) {
    const element = document.createElement("div");
    element.className = `msg ${role}`;
    if (role !== "user") {
      const author = document.createElement("span");
      author.className = "author";
      author.textContent = role === "agent" ? authorName || t.agent : t.bot;
      element.append(author);
    }
    const body = document.createElement("div");
    element.append(body);
    list.append(element);
    scrollDown();
    return { element, body };
  }

  function renderMessage(message) {
    if (rendered.has(message.id)) return;
    rendered.add(message.id);
    lastMessageId = Math.max(lastMessageId, message.id);
    const { element, body } = bubble(message.role, message.author_name);
    body.innerHTML = renderMarkdown(message.content);
    if (message.role === "assistant") addRating(element, message.id, message.rating);
  }

  function notice(text, action) {
    const element = document.createElement("div");
    element.className = "notice";
    element.textContent = text;
    if (action) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = action.label;
      button.addEventListener("click", () => {
        button.disabled = true;
        action.run();
      });
      element.append(document.createElement("br"), button);
    }
    list.append(element);
    scrollDown();
  }

  function addRating(element, messageId, current) {
    const box = document.createElement("div");
    box.className = "rating";
    for (const [value, emoji, label] of [["up", "👍", t.useful], ["down", "👎", t.notUseful]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = emoji;
      button.setAttribute("aria-label", label);
      button.setAttribute("aria-pressed", String(current === (value === "up" ? 1 : -1)));
      button.addEventListener("click", async () => {
        try {
          await api("POST", `/v1/messages/${messageId}/feedback`, { rating: value });
          for (const b of box.children) b.setAttribute("aria-pressed", String(b === button));
        } catch (error) {
          showError(error);
        }
      });
      box.append(button);
    }
    element.append(box);
  }

  function typingIndicator() {
    const { element, body } = bubble("assistant");
    body.innerHTML = '<span class="typing" aria-label="…"><span></span><span></span><span></span></span>';
    return { element, body };
  }

  function showError(error) {
    notice(error instanceof ApiError && error.status === 429 ? t.rateLimited : t.error);
  }

  function setBusy(value) {
    busy = value;
    sendButton.disabled = value;
  }

  function setConversation(next) {
    conversation = next;
    humanButton.hidden = !(next.kind === "bot" && next.status === "open");
    if (next.kind === "human" && next.status === "open") listenForAgent();
  }

  // -- Acciones ---------------------------------------------------------------------------

  async function loadConversation() {
    const data = await (await api("POST", "/v1/conversations")).json();
    // Al volver con el bot tras un chat humano se conserva lo que ya se ve en pantalla.
    if (!data.messages.length && !list.children.length) notice(t.empty);
    data.messages.forEach(renderMessage);
    if (data.kind === "human") notice(t.humanWaiting);
    setConversation(data);
  }

  async function send(text) {
    if (!conversation || conversation.status !== "open") await loadConversation();
    if (conversation.kind === "human") {
      const message = await (
        await api("POST", `/v1/conversations/${conversation.id}/messages`, { content: text })
      ).json();
      renderMessage(message);
      return;
    }

    const userBubble = bubble("user");
    userBubble.body.textContent = text;
    const answer = typingIndicator();
    let content = "";
    try {
      const response = await api(
        "POST", `/v1/conversations/${conversation.id}/messages`, { content: text }, { stream: true });
      for await (const { name, data } of readSse(response)) {
        if (name === "token") {
          content += data.text;
          answer.body.innerHTML = renderMarkdown(content);
          scrollDown();
        } else if (name === "done") {
          rendered.add(data.message_id);
          lastMessageId = Math.max(lastMessageId, data.message_id);
          if (content.trim()) addRating(answer.element, data.message_id, null);
          else answer.element.remove();
          if (data.escalated) {
            notice(t.humanWaiting);
            setConversation({ id: data.human_conversation_id, kind: "human", status: "open" });
          }
        } else if (name === "error") {
          answer.element.remove();
          notice(data.message, data.offer_human ? { label: t.human, run: escalate } : null);
        }
      }
    } catch (error) {
      answer.element.remove();
      throw error;
    }
  }

  async function escalate() {
    if (!conversation || conversation.kind !== "bot" || conversation.status !== "open") return;
    try {
      const { human_conversation_id: id } = await (
        await api("POST", `/v1/conversations/${conversation.id}/escalate`)
      ).json();
      notice(t.humanWaiting);
      setConversation({ id, kind: "human", status: "open" });
    } catch (error) {
      showError(error);
    }
  }

  /** Escucha los mensajes del agente. Se reconecta cuando el token caduca o la red falla. */
  async function listenForAgent() {
    eventsController?.abort();
    const controller = (eventsController = new AbortController());
    const conversationId = conversation.id;
    let delay = 1000;
    while (!controller.signal.aborted && conversation?.id === conversationId) {
      try {
        const response = await api(
          "GET", `/v1/conversations/${conversationId}/events?after=${lastMessageId}`, null,
          { stream: true, signal: controller.signal });
        delay = 1000;
        for await (const { name, data } of readSse(response)) {
          if (name === "message") renderMessage(data);
          if (name === "closed") {
            conversation = { ...conversation, status: "closed" };
            notice(t.humanClosed);
            return;
          }
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && [403, 404, 409].includes(error.status)) return;
        await new Promise((resolve) => setTimeout(resolve, delay));
        delay = Math.min(delay * 2, 30_000);
      }
    }
  }

  // -- Eventos de la interfaz -------------------------------------------------------------

  async function toggle(open = panel.hidden) {
    panel.hidden = !open;
    launcher.setAttribute("aria-expanded", String(open));
    if (!open) return;
    input.focus();
    if (!conversation) {
      try {
        await loadConversation();
      } catch (error) {
        showError(error);
      }
    }
  }

  launcher.addEventListener("click", () => toggle());
  $(".close").addEventListener("click", () => toggle(false));
  humanButton.addEventListener("click", escalate);
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") toggle(false);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    setBusy(true);
    try {
      await send(text);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
      input.focus();
    }
  });

  (document.body ? Promise.resolve() : new Promise((r) => addEventListener("DOMContentLoaded", r)))
    .then(() => document.body.append(host));
})();
