/* Live progress for a running research job: Server-Sent Events with a polling fallback.
   Everything shown is inserted with textContent; nothing from the server is treated as HTML. */
(function () {
  "use strict";
  var panel = document.querySelector("[data-job-panel]");
  if (!panel) return;

  var log = panel.querySelector("[data-run-log]");
  var stage = panel.querySelector("[data-run-stage]");
  var note = panel.querySelector("[data-run-note]");
  var steps = Array.prototype.slice.call(panel.querySelectorAll("[data-step]"));
  var order = steps.map(function (li) { return li.getAttribute("data-step"); });
  var last = parseInt(panel.getAttribute("data-after") || "0", 10);
  var finished = false;
  var source = null;
  var pollTimer = null;

  var LABELS = {
    queued: "Queued", planning: "Planning", researching: "Researching", analyzing: "Analysing",
    writing: "Writing", reviewing: "Reviewing", validating: "Checking",
  };

  function setStage(name) {
    if (!name) return;
    if (stage) stage.textContent = LABELS[name] || name;
    var idx = order.indexOf(name);
    steps.forEach(function (li, i) {
      li.classList.toggle("is-current", i === idx);
      li.classList.toggle("is-done", idx > -1 && i < idx);
      if (i === idx) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
  }

  var tasksList = panel.querySelector("[data-run-tasks]");

  function applySummary(sum) {
    if (!sum) return;
    Object.keys(sum).forEach(function (key) {
      var el = panel.querySelector('[data-fact="' + key + '"]');
      if (el && typeof sum[key] !== "object") el.textContent = String(sum[key]);
    });
    if (tasksList && Array.isArray(sum.tasks)) {
      tasksList.textContent = "";
      sum.tasks.forEach(function (t) {
        var li = document.createElement("li");
        li.className = "tasks__item tasks__item--" + t.status;
        var state = document.createElement("span");
        state.className = "tasks__state";
        var label = t.status === "running" ? "in progress" : t.status;
        state.textContent = label.charAt(0).toUpperCase() + label.slice(1);
        li.appendChild(state);
        li.appendChild(document.createTextNode(" " + t.title));
        tasksList.appendChild(li);
      });
    }
  }

  function addEvent(e) {
    if (e.seq <= last) return;
    last = e.seq;
    if (e.stage) setStage(e.stage);
    var li = document.createElement("li");
    li.className = "log__item log__item--" + (e.level || "info");
    var t = document.createElement("time");
    t.textContent = e.time || "";
    li.appendChild(t);
    li.appendChild(document.createTextNode(" " + e.message));
    log.appendChild(li);
    while (log.children.length > 200) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  }

  function done() {
    if (finished) return;
    finished = true;
    if (source) source.close();
    if (pollTimer) clearInterval(pollTimer);
    if (note) note.textContent = "Finished. Loading the result...";
    window.setTimeout(function () { window.location.reload(); }, 800);
  }

  function poll() {
    fetch(panel.getAttribute("data-events-url") + "?after=" + last, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
      .then(function (data) {
        (data.events || []).forEach(addEvent);
        applySummary(data.summary);
        if (data.finished) done();
      })
      .catch(function () { if (note) note.textContent = "Reconnecting..."; });
  }

  function startPolling() {
    if (pollTimer) return;
    poll();
    pollTimer = window.setInterval(poll, 3000);
  }

  if (!("EventSource" in window)) { startPolling(); return; }
  source = new EventSource(panel.getAttribute("data-stream-url") + "?after=" + last);
  source.addEventListener("progress", function (ev) {
    try { addEvent(JSON.parse(ev.data)); } catch (err) { /* ignore a malformed message */ }
  });
  source.addEventListener("summary", function (ev) {
    try { applySummary(JSON.parse(ev.data)); } catch (err) { /* ignore */ }
  });
  source.addEventListener("done", done);
  source.onerror = function () {
    /* The browser reconnects by itself (and resumes from Last-Event-ID). If the connection is
       closed for good, fall back to polling so the page still updates. */
    if (source.readyState === 2) startPolling();
  };
})();
