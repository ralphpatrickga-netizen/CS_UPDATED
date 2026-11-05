(function () {
  const widget = document.getElementById("bc-widget");
  if (!widget) return;

  const bubble = document.getElementById("bc-bubble");
  const panel = document.getElementById("bc-panel");
  const closeBtn = document.getElementById("bc-close");
  const log = document.getElementById("bc-log");
  const quickReplies = document.getElementById("bc-quick-replies");
  const form = document.getElementById("bc-form");
  const input = document.getElementById("bc-input");

  const MESSAGE_URL = widget.dataset.messageUrl;
  const HISTORY_URL = widget.dataset.historyUrl;
  const POS_KEY = "bc_bubble_pos";

  const SUGGESTIONS = [
    { label: "Document Requirements", action: "send", value: "What are the requirements for Barangay Clearance?" },
    { label: "Request a Document", action: "navigate", url: "/requests/new" },
    { label: "File a Complaint", action: "navigate", url: "/complaints/new" },
    { label: "Track My Request", action: "send", value: "What is the status of my request?" },
    { label: "Track My Complaint", action: "send", value: "What is the status of my complaint?" },
    { label: "Account Assistance", action: "send", value: "I need help with my account or password" },
  ];

  let historyLoaded = false;

  // ---------------------------------------------------------- positioning
  function applySavedPosition() {
    try {
      const saved = JSON.parse(localStorage.getItem(POS_KEY) || "null");
      if (saved && typeof saved.right === "number" && typeof saved.bottom === "number") {
        bubble.style.right = saved.right + "px";
        bubble.style.bottom = saved.bottom + "px";
        panel.style.right = saved.right + "px";
        panel.style.bottom = (saved.bottom + 70) + "px";
      }
    } catch (e) { /* ignore corrupt storage */ }
  }
  applySavedPosition();

  // ---------------------------------------------------------- dragging
  let dragging = false, moved = false, suppressClick = false;
  let startX, startY, startRight, startBottom;

  function pointerDown(e) {
    if (e.button !== 0) return;
    dragging = true;
    moved = false;
    startX = e.clientX;
    startY = e.clientY;
    const rect = bubble.getBoundingClientRect();
    startRight = window.innerWidth - rect.right;
    startBottom = window.innerHeight - rect.bottom;
    bubble.setPointerCapture(e.pointerId);
  }

  function pointerMove(e) {
    if (!dragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) moved = true;
    if (!moved) return;
    e.preventDefault();
    bubble.classList.add("dragging");
    let newRight = startRight - dx;
    let newBottom = startBottom - dy;
    newRight = Math.max(8, Math.min(window.innerWidth - 66, newRight));
    newBottom = Math.max(8, Math.min(window.innerHeight - 66, newBottom));
    bubble.style.right = newRight + "px";
    bubble.style.bottom = newBottom + "px";
    panel.style.right = newRight + "px";
    panel.style.bottom = (newBottom + 70) + "px";
  }

  function pointerUp(e) {
    dragging = false;
    bubble.classList.remove("dragging");
    if (moved) {
      const rect = bubble.getBoundingClientRect();
      const right = window.innerWidth - rect.right;
      const bottom = window.innerHeight - rect.bottom;
      localStorage.setItem(POS_KEY, JSON.stringify({ right, bottom }));
      suppressClick = e.type === "pointerup";
    }
  }

  bubble.addEventListener("pointerdown", pointerDown);
  bubble.addEventListener("pointermove", pointerMove);
  bubble.addEventListener("pointerup", pointerUp);
  bubble.addEventListener("pointercancel", pointerUp);
  bubble.addEventListener("click", (e) => {
    if (suppressClick) {
      suppressClick = false;
      e.preventDefault();
      return;
    }
    togglePanel();
  });

  // ---------------------------------------------------------- panel
  function togglePanel() {
    const isOpen = panel.classList.contains("open");
    if (isOpen) {
      panel.classList.remove("open");
    } else {
      panel.classList.add("open");
      if (!historyLoaded) loadHistory();
    }
  }
  closeBtn.addEventListener("click", () => panel.classList.remove("open"));

  function addBubbleMsg(text, who) {
    const div = document.createElement("div");
    div.className = "bc-bubble-msg " + (who === "user" ? "bc-msg-user" : who === "typing" ? "bc-msg-typing" : "bc-msg-bot");
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function renderQuickReplies() {
    quickReplies.innerHTML = "";
    SUGGESTIONS.forEach((q) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "bc-chip";
      chip.textContent = q.label;
      chip.addEventListener("click", () => {
        if (q.action === "navigate") {
          window.location.href = q.url;
          return;
        }
        sendMessage(q.value || q.label);
      });
      quickReplies.appendChild(chip);
    });
  }

  function clearQuickReplies() {
    quickReplies.innerHTML = "";
  }

  async function loadHistory() {
    historyLoaded = true;
    try {
      const res = await fetch(HISTORY_URL);
      const data = await res.json();
      const msgs = data.messages || [];
      if (msgs.length === 0) {
        addBubbleMsg("Kumusta! Ask me anything about your requests, complaints, or barangay documents.", "bot");
      } else {
        msgs.forEach((m) => addBubbleMsg(m.message, m.role));
      }
      renderQuickReplies();
    } catch (e) {
      addBubbleMsg("Kumusta! Ask me anything about your requests, complaints, or barangay documents.", "bot");
      renderQuickReplies();
    }
  }

  async function sendMessage(text) {
    if (!text) return;
    clearQuickReplies();
    addBubbleMsg(text, "user");
    input.value = "";
    const typingEl = addBubbleMsg("...", "typing");
    try {
      const res = await fetch(MESSAGE_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await res.json();
      typingEl.remove();
      addBubbleMsg(data.reply || "Sorry, something went wrong.", "bot");
      renderQuickReplies();
    } catch (e) {
      typingEl.remove();
      addBubbleMsg("Connection error. Please try again.", "bot");
      renderQuickReplies();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text) sendMessage(text);
  });
})();
