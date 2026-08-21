/* Anastomosis offline archive — patient search bootstrap.
 *
 * Reads the inline `<script type="application/json" id="anast-index">` block
 * (data, not code — CSP-safe), then wires the #q input to filter and render
 * the patient list. Vanilla DOM, no framework, no network requests.
 *
 * Search ranking: exact display_name prefix > display_name substring > any
 * substring in the concatenated `search` field. Case-insensitive. Tokens
 * (whitespace-separated) must all match somewhere on the entry.
 */
(function () {
  "use strict";

  var node = document.getElementById("anast-index");
  if (!node) { return; }
  var entries;
  try {
    entries = JSON.parse(node.textContent || "[]");
  } catch (err) {
    return;
  }

  var input = document.getElementById("q");
  var results = document.getElementById("results");
  if (!input || !results) { return; }

  function score(entry, tokens) {
    if (!tokens.length) { return 1; }
    var name = (entry.display_name || "").toLowerCase();
    var hay = (entry.search || "").toLowerCase() + " " + name;
    var best = 0;
    for (var i = 0; i < tokens.length; i += 1) {
      var t = tokens[i];
      if (!t) { continue; }
      if (hay.indexOf(t) === -1) { return 0; }
      if (name.indexOf(t) === 0) { best = Math.max(best, 3); }
      else if (name.indexOf(t) !== -1) { best = Math.max(best, 2); }
      else { best = Math.max(best, 1); }
    }
    return best;
  }

  function render(query) {
    var tokens = query.toLowerCase().split(/\s+/).filter(function (t) { return t.length > 0; });
    var scored = [];
    for (var i = 0; i < entries.length; i += 1) {
      var s = score(entries[i], tokens);
      if (s > 0) { scored.push({ entry: entries[i], score: s }); }
    }
    scored.sort(function (a, b) {
      if (b.score !== a.score) { return b.score - a.score; }
      return (a.entry.display_name || "").localeCompare(b.entry.display_name || "");
    });
    results.textContent = "";
    for (var j = 0; j < scored.length; j += 1) {
      var entry = scored[j].entry;
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = "patients/" + encodeURIComponent(entry.id) + "/index.html";
      a.textContent = entry.display_name || entry.id;
      li.appendChild(a);
      if (entry.dob) {
        var dob = document.createElement("span");
        dob.className = "dob";
        dob.textContent = "DOB " + entry.dob;
        li.appendChild(dob);
      }
      var count = document.createElement("span");
      count.className = "count";
      count.textContent = entry.encounter_count + " encounter" + (entry.encounter_count === 1 ? "" : "s");
      li.appendChild(count);
      results.appendChild(li);
    }
  }

  input.addEventListener("input", function () { render(input.value); });
  render("");
}());
