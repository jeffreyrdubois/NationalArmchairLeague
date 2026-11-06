// Choosing a confidence value removes it from the other games' menus,
// and clearing it puts the value back. Run with: node tests/test_pick_points.js

const assert = require("assert");
const {
  pointLabel,
  pointOptionsFor,
  syncPointMenus,
} = require("../app/static/js/pick-points.js");

const WEEK = [14, 15, 16];

function fakeSelect(value, optionValues) {
  const sel = {
    value: value == null ? "" : String(value),
    options: optionValues.map((v) => ({ value: String(v) })),
    ownerDocument: {
      createElement() {
        return { value: "", textContent: "", selected: false };
      },
    },
    replaceChildren() {
      sel.options = [];
    },
    appendChild(opt) {
      sel.options.push(opt);
    },
  };
  return sel;
}

function values(sel) {
  return sel.options.map((opt) => opt.value);
}

function test(name, fn) {
  try {
    fn();
    console.log(`ok  ${name}`);
  } catch (err) {
    console.error(`FAIL ${name}: ${err.stack || err}`);
    process.exitCode = 1;
  }
}

test("labels stay singular only for one point", () => {
  assert.strictEqual(pointLabel(1), "1 point");
  assert.strictEqual(pointLabel(16), "16 points");
});

test("a chosen value is omitted from the other games", () => {
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "", ""], 0), ["", "14", "15", "16"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "", ""], 1), ["", "14", "15"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "", ""], 2), ["", "14", "15"]);
});

test("clearing a value puts it back on the other menus", () => {
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["", "15", ""], 0), ["", "14", "16"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["", "15", ""], 1), ["", "14", "15", "16"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["", "15", ""], 2), ["", "14", "16"]);
});

test("each menu keeps its own value and not the other game's", () => {
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "14", ""], 0), ["", "15", "16"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "14", ""], 1), ["", "14", "15"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "14", ""], 2), ["", "15"]);
});

test("a duplicate stays visible on the games that have it", () => {
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "16", ""], 0), ["", "14", "15", "16"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "16", ""], 1), ["", "14", "15", "16"]);
  assert.deepStrictEqual(pointOptionsFor(WEEK, ["16", "16", ""], 2), ["", "14", "15"]);
});

test("syncing the menus drops a newly chosen value from the rest", () => {
  const menus = [
    fakeSelect("16", ["", "14", "15", "16"]),
    fakeSelect("", ["", "14", "15", "16"]),
    fakeSelect("", ["", "14", "15", "16"]),
  ];
  syncPointMenus(menus, WEEK);
  assert.deepStrictEqual(values(menus[0]), ["", "14", "15", "16"]);
  assert.strictEqual(menus[0].value, "16");
  assert.deepStrictEqual(values(menus[1]), ["", "14", "15"]);
  assert.deepStrictEqual(values(menus[2]), ["", "14", "15"]);

  menus[1].value = "15";
  syncPointMenus(menus, WEEK);
  assert.deepStrictEqual(values(menus[0]), ["", "14", "16"]);
  assert.strictEqual(menus[0].value, "16");
  assert.deepStrictEqual(values(menus[1]), ["", "14", "15"]);
  assert.strictEqual(menus[1].value, "15");
  assert.deepStrictEqual(values(menus[2]), ["", "14"]);

  menus[1].value = "";
  syncPointMenus(menus, WEEK);
  assert.deepStrictEqual(values(menus[0]), ["", "14", "15", "16"]);
  assert.strictEqual(menus[0].value, "16");
  assert.deepStrictEqual(values(menus[1]), ["", "14", "15"]);
  assert.deepStrictEqual(values(menus[2]), ["", "14", "15"]);
  assert.strictEqual(menus[1].value, "");
  const restored = menus[2].options.find((opt) => opt.value === "15");
  assert.strictEqual(restored.textContent, "15 points");
});

if (process.exitCode) {
  process.exit(process.exitCode);
}
console.log("passed");
