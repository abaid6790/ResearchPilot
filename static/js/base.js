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

  // Prevent accidental double submission, and show that something is happening
  function guardSubmit(form) {
    form.addEventListener("submit", function () {
      if (!form.checkValidity()) return;
      form.querySelectorAll("[type=submit]").forEach(function (button) {
        window.setTimeout(function () {
          button.disabled = true;
          button.classList.add("is-loading");
          button.setAttribute("aria-busy", "true");
        }, 0);
      });
    });
  }
  document.querySelectorAll("form[data-single-submit], form[data-loading]").forEach(guardSubmit);

  // Sidebar drawer (narrow screens)
  var sidebar = document.getElementById("sidebar");
  var sidebarButton = document.querySelector("[data-sidebar-toggle]");
  var backdrop = document.querySelector("[data-sidebar-backdrop]");
  if (sidebar && sidebarButton) {
    var setDrawer = function (open) {
      sidebar.classList.toggle("is-open", open);
      sidebarButton.setAttribute("aria-expanded", open ? "true" : "false");
      sidebarButton.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
      if (backdrop) backdrop.hidden = !open;
    };
    sidebarButton.addEventListener("click", function () { setDrawer(!sidebar.classList.contains("is-open")); });
    if (backdrop) backdrop.addEventListener("click", function () { setDrawer(false); });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && sidebar.classList.contains("is-open")) { setDrawer(false); sidebarButton.focus(); }
    });
    sidebar.querySelectorAll("a").forEach(function (a) { a.addEventListener("click", function () { setDrawer(false); }); });
  }
})();
