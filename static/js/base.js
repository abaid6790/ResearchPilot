// Shared behaviour: theme toggle, user menu, dismissible alerts, password reveal, double-submit guard.
(function () {
  "use strict";

  function currentTheme() {
    var set = document.documentElement.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("rp-theme", next); } catch (e) { /* ignore */ }
    });
  });

  // User menu
  var menuButton = document.querySelector("[data-menu-toggle]");
  if (menuButton) {
    var panel = document.getElementById(menuButton.getAttribute("aria-controls"));
    var setOpen = function (open) {
      menuButton.setAttribute("aria-expanded", open ? "true" : "false");
      panel.hidden = !open;
    };
    menuButton.addEventListener("click", function () {
      setOpen(menuButton.getAttribute("aria-expanded") !== "true");
    });
    document.addEventListener("click", function (event) {
      if (!panel.hidden && !panel.contains(event.target) && !menuButton.contains(event.target)) setOpen(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) { setOpen(false); menuButton.focus(); }
    });
  }

  // Dismissible alerts
  document.querySelectorAll("[data-dismiss]").forEach(function (button) {
    button.addEventListener("click", function () {
      var alert = button.closest(".alert");
      if (alert) alert.remove();
    });
  });

  // Show / hide password
  document.querySelectorAll("[data-toggle-password]").forEach(function (button) {
    var input = document.querySelector(button.getAttribute("data-toggle-password"));
    if (!input) return;
    button.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      button.textContent = show ? "Hide" : "Show";
      button.setAttribute("aria-pressed", show ? "true" : "false");
      button.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
  });

  // Prevent accidental double submission
  document.querySelectorAll("form[data-single-submit]").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (!form.checkValidity()) return;
      form.querySelectorAll("[type=submit]").forEach(function (button) {
        window.setTimeout(function () { button.disabled = true; }, 0);
      });
    });
  });
})();
