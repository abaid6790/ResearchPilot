// Runs in <head> before first paint so the saved theme never flashes.
(function () {
  try {
    var saved = localStorage.getItem("rp-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {
    /* storage unavailable: fall back to the system preference */
  }
})();
