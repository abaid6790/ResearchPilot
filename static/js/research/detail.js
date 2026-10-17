// Project page: delete confirmation dialog (the server re-checks the typed title).
(function () {
  "use strict";
  var opener = document.querySelector("[data-open-dialog]");
  if (!opener) return;
  var dialog = document.getElementById(opener.getAttribute("data-open-dialog"));
  if (!dialog || typeof dialog.showModal !== "function") return;
  var input = dialog.querySelector("[data-confirm-title]");
  var submit = dialog.querySelector("[data-delete-submit]");

  opener.addEventListener("click", function () {
    if (input) input.value = "";
    if (submit) submit.disabled = true;
    dialog.showModal();
    if (input) input.focus();
  });
  dialog.querySelectorAll("[data-close-dialog]").forEach(function (b) {
    b.addEventListener("click", function () { dialog.close(); opener.focus(); });
  });
  if (input && submit) {
    input.addEventListener("input", function () {
      submit.disabled = input.value.trim() !== input.getAttribute("data-confirm-title");
    });
  }
})();
