// Password strength hint and confirm-match check. Hints only: the server is the authority.
(function () {
  "use strict";

  var LABELS = ["", "Too weak", "Fair", "Good", "Strong"];

  function score(value) {
    if (!value) return 0;
    var points = 0;
    if (value.length >= 10) points += 1;
    if (value.length >= 14) points += 1;
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) points += 1;
    if (/\d/.test(value) && /[^A-Za-z0-9]/.test(value)) points += 1;
    if (value.length < 10) return Math.min(points, 1) || 1;
    return Math.max(1, Math.min(points, 4));
  }

  document.querySelectorAll("[data-password-input]").forEach(function (input) {
    var meter = input.closest(".field").querySelector("[data-strength-meter]");
    if (!meter) return;
    var label = meter.querySelector("[data-strength-label]");
    input.addEventListener("input", function () {
      var s = score(input.value);
      meter.setAttribute("data-strength", String(s));
      label.textContent = LABELS[s];
    });
  });

  document.querySelectorAll("[data-confirm-for]").forEach(function (confirmInput) {
    var original = document.querySelector(confirmInput.getAttribute("data-confirm-for"));
    if (!original) return;
    function check() {
      var mismatch = confirmInput.value && confirmInput.value !== original.value;
      confirmInput.setCustomValidity(mismatch ? "Passwords do not match." : "");
    }
    confirmInput.addEventListener("input", check);
    original.addEventListener("input", check);
  });
})();
