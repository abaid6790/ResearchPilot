/* Report editor: autosave with conflict handling, critique, copy. Server text is only ever
   inserted with textContent. If anything here fails the plain forms still work. */
(function () {
  "use strict";
  var root = document.querySelector("[data-editor]");
  if (!root) return;

  var body = root.querySelector("[data-body]");
  var form = root.querySelector("[data-editor-form]");
  var versionField = root.querySelector("[data-version-field]");
  var status = root.querySelector("[data-save-status]");
  var csrf = root.getAttribute("data-csrf");
  var lastSaved = body ? body.value : "";
  var timer = null;
  var saving = false;
  var stopped = false;
  var DELAY = 4000;

  function say(text, isError) {
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("editor__status--error", !!isError);
  }

  function showChecks(checks) {
    var list = root.querySelector("[data-checks-list]");
    if (!list || !checks) return;
    list.textContent = "";
    function add(text, bad) {
      var li = document.createElement("li");
      if (bad) li.className = "bad";
      li.textContent = text;
      list.appendChild(li);
    }
    if (checks.ok) add("No problems found in this section. This does not prove the statements are correct.", false);
    if (checks.invalid_citations.length) add(checks.invalid_citations.length + " citation marker(s) point at sources that are not part of this report.", true);
    if (checks.unverified_quotes.length) add(checks.unverified_quotes.length + " quotation(s) were not found in the cited sources.", true);
    if (checks.uncited_paragraphs) add(checks.uncited_paragraphs + " long paragraph(s) have no citation.", true);
  }

  function post(url, data) {
    return fetch(url, {
      method: "POST", body: data, credentials: "same-origin",
      headers: { "X-Requested-With": "fetch", "X-CSRFToken": csrf, "Accept": "application/json" }
    });
  }

  function autosave() {
    if (stopped || saving || !body || body.readOnly || body.value === lastSaved) return;
    saving = true;
    var text = body.value;
    var data = new FormData();
    data.append("body", text);
    data.append("version", versionField.value);
    data.append("autosave", "1");
    say("Saving...", false);
    post(root.getAttribute("data-save-url"), data).then(function (resp) {
      return resp.json().then(function (json) { return { status: resp.status, json: json }; });
    }).then(function (r) {
      if (r.status === 200 && r.json.ok) {
        lastSaved = text;
        versionField.value = String(r.json.version);
        say("Draft saved. Saved text is version " + r.json.version + ".", false);
        showChecks(r.json.checks);
      } else if (r.status === 409) {
        stopped = true;
        say("This section was changed somewhere else, so autosave stopped. Copy your text, then reload the page.", true);
      } else {
        say((r.json && r.json.message) || "Could not save. Your text is still here; use Save to try again.", true);
      }
    }).catch(function () {
      say("Could not save (network problem). Your text is still here; use Save to try again.", true);
    }).then(function () { saving = false; });
  }

  if (body && !body.readOnly) {
    body.addEventListener("input", function () {
      say("Unsaved changes.", false);
      window.clearTimeout(timer);
      timer = window.setTimeout(autosave, DELAY);
    });
  }
  window.addEventListener("beforeunload", function (ev) {
    if (body && body.value !== lastSaved) { ev.preventDefault(); ev.returnValue = ""; }
  });
  if (form) form.addEventListener("submit", function () { lastSaved = body.value; });

  // ---- critique
  var cBtn = root.querySelector("[data-critique]");
  var cOut = root.querySelector("[data-critique-result]");
  if (cBtn && cOut) {
    cBtn.addEventListener("click", function () {
      cBtn.disabled = true;
      cOut.textContent = "Asking for a critique...";
      post(root.getAttribute("data-critique-url"), new FormData()).then(function (resp) {
        return resp.json().then(function (json) { return { ok: resp.ok, json: json }; });
      }).then(function (r) {
        cOut.textContent = "";
        if (!r.ok) { cOut.textContent = r.json.message || "The critique could not be made."; return; }
        var p = document.createElement("p");
        p.textContent = r.json.assessment || (r.json.notes.length ? "Points to consider:" : "The critic found nothing to flag. That does not prove the section is correct.");
        cOut.appendChild(p);
        var ul = document.createElement("ul");
        r.json.notes.forEach(function (n) {
          var li = document.createElement("li");
          li.textContent = "(" + n.severity + ") " + n.problem + (n.suggestion ? " Suggestion: " + n.suggestion : "");
          ul.appendChild(li);
        });
        cOut.appendChild(ul);
      }).catch(function () {
        cOut.textContent = "The critique could not be made (network problem).";
      }).then(function () { cBtn.disabled = false; });
    });
  }

  // ---- copy
  var copyBtn = root.querySelector("[data-copy-report]");
  var copyStatus = root.querySelector("[data-copy-status]");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      fetch(root.getAttribute("data-text-url"), { credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error("bad"); return r.text(); })
        .then(function (text) { return navigator.clipboard.writeText(text); })
        .then(function () { copyStatus.textContent = "Report text copied."; })
        .catch(function () { copyStatus.textContent = "Could not copy. Open the report view and select the text."; });
    });
  }
})();
