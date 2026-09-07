const loginView = document.getElementById("login-view");
const chatView = document.getElementById("chat-view");
const loginForm = document.getElementById("login-form");
const loginSubmit = document.getElementById("login-submit");
const loginError = document.getElementById("login-error");
const loginStatus = document.getElementById("login-status");
const activitySummary = document.getElementById("activity-summary");
const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const logoutBtn = document.getElementById("logout-btn");

function showChatView(activityCount) {
  loginView.hidden = true;
  chatView.hidden = false;
  logoutBtn.hidden = false;
  activitySummary.textContent =
    activityCount === undefined
      ? "Chat about your Komoot activities."
      : `Loaded ${activityCount} activit${activityCount === 1 ? "y" : "ies"}. Ask away.`;
}

function showLoginView() {
  chatView.hidden = true;
  loginView.hidden = false;
  logoutBtn.hidden = true;
}

function addMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

async function restoreSession() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    if (data.logged_in) {
      showChatView(data.activities);
    }
  } catch {
    // Backend not reachable yet; stay on the login view.
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.hidden = true;
  loginStatus.hidden = false;
  loginStatus.textContent = "Signing in and syncing your GPX activities… this can take a moment.";
  loginSubmit.disabled = true;

  const email = document.getElementById("email").value;
  const password = document.getElementById("password").value;

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Login failed");
    }
    loginForm.reset();
    showChatView(data.activities);
  } catch (err) {
    loginError.textContent = err.message;
    loginError.hidden = false;
  } finally {
    loginStatus.hidden = true;
    loginSubmit.disabled = false;
  }
});

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;

  addMessage("user", message);
  chatInput.value = "";
  const pending = addMessage("assistant pending", "Thinking…");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Chat request failed");
    }
    pending.textContent = data.reply;
    pending.className = "msg assistant";
  } catch (err) {
    pending.textContent = `Error: ${err.message}`;
    pending.className = "msg assistant error";
  }
});

logoutBtn.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  chatLog.innerHTML = "";
  showLoginView();
});

restoreSession();
