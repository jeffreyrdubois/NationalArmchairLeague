// Confidence points are unique across the week's games. A value already
// chosen on one game is left out of every other game's menu, so opening
// that menu only offers what is still free. The game keeps its own value.

function pointLabel(pt) {
  const n = Number(pt);
  return n === 1 ? "1 point" : n + " points";
}

function pointOptionsFor(allPoints, selections, index) {
  const current = selections[index] == null ? "" : String(selections[index]);
  const taken = new Set();
  selections.forEach(function (value, i) {
    if (i !== index && value !== "" && value != null) taken.add(String(value));
  });

  const options = [""];
  const seen = new Set([""]);
  for (const pt of allPoints) {
    const value = String(pt);
    if (seen.has(value)) continue;
    if (value === current || !taken.has(value)) {
      options.push(value);
      seen.add(value);
    }
  }
  // A saved value outside this week's list still has to stay visible.
  if (current && !seen.has(current)) options.push(current);
  return options;
}

function syncPointMenus(selects, allPoints) {
  const list = Array.from(selects);
  const selections = list.map(function (sel) {
    return sel.value == null ? "" : String(sel.value);
  });

  list.forEach(function (sel, index) {
    const current = selections[index];
    const wanted = pointOptionsFor(allPoints, selections, index);
    const existing = Array.from(sel.options, function (opt) {
      return String(opt.value);
    });
    if (
      existing.length === wanted.length &&
      existing.every(function (value, i) {
        return value === wanted[i];
      })
    ) {
      return;
    }

    const doc = sel.ownerDocument || document;
    sel.replaceChildren();
    wanted.forEach(function (value) {
      const opt = doc.createElement("option");
      opt.value = value;
      opt.textContent = value === "" ? "-- Select points --" : pointLabel(value);
      opt.selected = value === current;
      sel.appendChild(opt);
    });
    sel.value = current;
  });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { pointLabel, pointOptionsFor, syncPointMenus };
}
