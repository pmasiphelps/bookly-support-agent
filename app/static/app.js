const sessionId = "session-" + Math.random().toString(36).slice(2);

const messagesEl = document.getElementById("messages");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const devToggle = document.getElementById("devToggle");

// "Dev: show tool calls" -- purely a display toggle. The backend always
// sends each reply's real tool calls (see ChatResponse.tool_calls in
// app/main.py); this just flips whether the .tool-trace blocks already in
// the DOM are visible, so toggling reveals every past turn too, not only
// new ones.
devToggle.addEventListener("click", () => {
  const on = document.body.classList.toggle("show-tool-calls");
  devToggle.classList.toggle("active", on);
});

function addMessage(text, role) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

// Renders the tool calls (name, input, result) behind one reply, for the
// "Dev: show tool calls" toggle above. Hidden by default via CSS
// (.tool-trace); this is what proves an answer came from a real tool
// call and not a guess.
function addToolTrace(toolCalls) {
  if (!toolCalls || !toolCalls.length) return;

  const box = document.createElement("div");
  box.className = "tool-trace";

  const label = document.createElement("div");
  label.className = "tool-trace-label";
  label.textContent = toolCalls.length === 1 ? "1 tool call" : `${toolCalls.length} tool calls`;
  box.appendChild(label);

  toolCalls.forEach((call) => {
    const entry = document.createElement("pre");
    entry.className = "tool-trace-entry";
    entry.textContent =
      `${call.name}(${JSON.stringify(call.input)})\n→ ${JSON.stringify(call.result, null, 2)}`;
    box.appendChild(entry);
  });

  messagesEl.appendChild(box);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// Renders the scope gate's "unclear" quick-reply buttons (see
// app/intent_router.py's CLARIFYING_SUGGESTIONS). Each button's `category`
// routes straight to that category via agent.run_turn_for_category,
// instead of reclassifying `value` like ordinary typed text.
function addSuggestions(suggestions) {
  const row = document.createElement("div");
  row.className = "suggestions";
  suggestions.forEach((s) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "suggestion-btn";
    btn.textContent = s.label;
    btn.addEventListener("click", () => {
      row.remove(); // one-shot -- picking any option retires the whole row
      sendMessage(s.value, s.category);
    });
    row.appendChild(btn);
  });
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function sendMessage(text, category) {
  addMessage(text, "user");
  const pending = addMessage("...", "agent pending");

  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        message: text,
        category: category || null,
      }),
    });
    const data = await res.json();
    pending.textContent = data.reply;
    pending.classList.remove("pending");
    addToolTrace(data.tool_calls);
    if (data.suggestions && data.suggestions.length) {
      addSuggestions(data.suggestions);
    }
  } catch (err) {
    pending.textContent = "Something went wrong reaching the server. Is the backend running?";
    pending.classList.remove("pending");
  }
}

addMessage(
  "Hi, I'm Bookly Support. I can help with order status, returns, or general questions. What's going on?",
  "agent"
);

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendMessage(text, null);
});
