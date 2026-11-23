const menuToggle = document.querySelector("#menuToggle");
const sidebar = document.querySelector("#sidebar");

if (menuToggle && sidebar) {
  menuToggle.addEventListener("click", () => sidebar.classList.toggle("open"));
  document.addEventListener("click", (event) => {
    if (window.innerWidth <= 780 && !sidebar.contains(event.target) && !menuToggle.contains(event.target)) {
      sidebar.classList.remove("open");
    }
  });
}

const attendanceActive = document.querySelector('meta[name="attendance-active"]')?.content === "true";
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;

if (attendanceActive && csrfToken) {
  let activityPending = false;
  let lastSent = 0;
  const sendActivity = () => {
    if (Date.now() - lastSent < 15000) return;
    activityPending = false;
    lastSent = Date.now();
    fetch("/attendance/activity", {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: { "X-CSRFToken": csrfToken },
    }).catch(() => {
      if (Date.now() - lastSent < 120000) activityPending = true;
    });
  };
  const markActive = () => {
    activityPending = true;
    sendActivity();
  };
  ["pointerdown", "pointermove", "keydown", "touchstart", "scroll"].forEach((eventName) => {
    document.addEventListener(eventName, markActive, { passive: true });
  });

  window.setInterval(() => {
    if (!activityPending || document.visibilityState !== "visible") return;
    sendActivity();
  }, 30000);
}

document.addEventListener("click", (event) => {
  const removeField = event.target.closest(".template-remove-field");
  if (removeField) {
    const container = removeField.form.querySelector(".template-fields");
    const rows = container.querySelectorAll(".template-field-row");
    const row = removeField.closest(".template-field-row");
    if (rows.length === 1) {
      row.querySelectorAll("input").forEach((input) => {
        input.value = "";
      });
    } else {
      row.remove();
    }
    return;
  }

  const addField = event.target.closest(".template-add-field");
  if (!addField) return;
  const container = addField.form.querySelector(".template-fields");
  const row = container.querySelector(".template-field-row").cloneNode(true);
  row.querySelectorAll("input").forEach((input) => {
    input.value = "";
  });
  container.append(row);
});
