// New research form: depth preset hints and character counters. The server enforces every limit.
(function () {
  "use strict";
  var root = document.querySelector("[data-limits]");
  if (!root) return;
  var limits = {};
  try { limits = JSON.parse(root.getAttribute("data-limits")); } catch (e) { return; }

  var depth = document.getElementById("depth");
  var summary = document.querySelector("[data-depth-summary]");
  var fields = {
    sources: document.getElementById("max_sources"),
    research: document.getElementById("max_research_rounds"),
    revision: document.getElementById("max_revision_rounds")
  };

  function applyDepth() {
    var preset = limits.presets && limits.presets[depth.value];
    if (!preset) return;
    if (summary) {
      summary.textContent = "Defaults for this depth: up to " + preset.sources + " sources, " +
        preset.research + " research round(s), " + preset.revision + " revision round(s). Higher depth takes longer and uses more API quota.";
    }
    if (fields.sources) fields.sources.placeholder = String(preset.sources);
    if (fields.research) fields.research.placeholder = String(preset.research);
    if (fields.revision) fields.revision.placeholder = String(preset.revision);
  }
  if (depth) { depth.addEventListener("change", applyDepth); applyDepth(); }

  if (limits.hard) {
    if (fields.sources) { fields.sources.min = 3; fields.sources.max = limits.hard.max_sources; }
    if (fields.research) { fields.research.min = 1; fields.research.max = limits.hard.max_research_rounds; }
    if (fields.revision) { fields.revision.min = 0; fields.revision.max = limits.hard.max_revision_rounds; }
  }

  document.querySelectorAll("[data-counter-for]").forEach(function (counter) {
    var input = document.getElementById(counter.getAttribute("data-counter-for"));
    if (!input) return;
    var max = limits.topic_max || 4000;
    function update() {
      var n = input.value.length;
      counter.textContent = n + " / " + max;
      counter.classList.toggle("counter--over", n > max);
    }
    input.addEventListener("input", update);
    update();
  });
})();
