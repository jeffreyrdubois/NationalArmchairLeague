/**
 * Simulator: flip remaining covers and recompute the week matrix in place.
 *
 * The server ships the default "viewer wins every remaining game" slate in
 * #sim-data. Each toggle updates assumptions and repaints cells, totals,
 * and the podium list — no round trip.
 */
(function () {
  var dataEl = document.getElementById("sim-data");
  if (!dataEl) return;

  var data;
  try {
    data = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }
  if (!data || !data.games) return;

  var assumptions = Object.assign({}, data.assumptions);

  function coveringTeam(game, homeCovered) {
    return homeCovered ? game.home : game.away;
  }

  function homeCoveredFor(game) {
    if (game.is_final) return !!game.home_covered;
    var key = String(game.id);
    return !!assumptions[key];
  }

  function pointsFor(uid, game) {
    var byUser = data.picks[String(uid)] || {};
    var pick = byUser[String(game.id)];
    if (!pick) return 0;
    var winner = coveringTeam(game, homeCoveredFor(game));
    return pick.team === winner ? pick.points : 0;
  }

  function computeStandings() {
    var rows = data.users.map(function (u) {
      var total = 0;
      var perGame = {};
      data.games.forEach(function (g) {
        var earned = pointsFor(u.id, g);
        perGame[g.id] = earned;
        total += earned;
      });
      return { user: u, total: total, per_game: perGame };
    });
    rows.sort(function (a, b) {
      if (b.total !== a.total) return b.total - a.total;
      return a.user.id - b.user.id;
    });
    return rows;
  }

  function podiumRanks(rows) {
    var ranks = {};
    var place = 1;
    var i = 0;
    while (i < rows.length && place <= 3) {
      var score = rows[i].total;
      var tied = [];
      for (var j = i; j < rows.length; j++) {
        if (rows[j].total === score) tied.push(rows[j]);
        else break;
      }
      tied.forEach(function (row) {
        if (place <= 3) ranks[row.user.id] = place;
      });
      i += tied.length;
      place += tied.length;
    }
    return ranks;
  }

  function medalLabel(place) {
    if (place === 1) return "🥇 1st";
    if (place === 2) return "🥈 2nd";
    if (place === 3) return "🥉 3rd";
    return "\u00a0";
  }

  function listRank(place, index) {
    if (place === 1) return "🥇";
    if (place === 2) return "🥈";
    if (place === 3) return "🥉";
    return '<span class="text-gray-400">' + (index + 1) + "</span>";
  }

  function placeClass(place) {
    if (place === 1) return "sim-place-1";
    if (place === 2) return "sim-place-2";
    if (place === 3) return "sim-place-3";
    return "";
  }

  function paint() {
    var rows = computeStandings();
    var podium = podiumRanks(rows);
    var byId = {};
    rows.forEach(function (r) {
      byId[r.user.id] = r;
    });

    data.games.forEach(function (g) {
      if (g.is_final) return;
      var label = document.querySelector(
        '.sim-assumed-label[data-game-id="' + g.id + '"]'
      );
      if (label) {
        label.textContent = "assume " + coveringTeam(g, homeCoveredFor(g));
      }
    });

    data.games.forEach(function (g) {
      data.users.forEach(function (u) {
        var cell = document.querySelector(
          '#sim-matrix td[data-user-id="' +
            u.id +
            '"][data-game-id="' +
            g.id +
            '"]'
        );
        if (!cell) return;
        var pick = (data.picks[String(u.id)] || {})[String(g.id)];
        if (!pick) return;
        var earned = byId[u.id].per_game[g.id] || 0;
        var teamEl = cell.querySelector(".sim-pick-team");
        var ptsEl = cell.querySelector(".sim-pick-pts");
        if (teamEl) {
          teamEl.classList.toggle("text-green-700", earned > 0);
          teamEl.classList.toggle("text-red-600", earned <= 0);
        }
        if (ptsEl) {
          ptsEl.classList.toggle("bg-green-100", earned > 0);
          ptsEl.classList.toggle("text-green-700", earned > 0);
          ptsEl.classList.toggle("bg-red-100", earned <= 0);
          ptsEl.classList.toggle("text-red-600", earned <= 0);
          ptsEl.classList.toggle("line-through", earned <= 0);
        }
      });
    });

    data.users.forEach(function (u) {
      var td = document.querySelector(
        '#sim-totals td[data-user-id="' + u.id + '"]'
      );
      if (!td) return;
      var place = podium[u.id] || null;
      td.classList.remove("sim-place-1", "sim-place-2", "sim-place-3");
      var pc = placeClass(place);
      if (pc) td.classList.add(pc);
      td.setAttribute("data-place", place || "");
      var totalEl = td.querySelector(".sim-total");
      var medalEl = td.querySelector(".sim-medal");
      if (totalEl) totalEl.textContent = String(Math.trunc(byId[u.id].total));
      if (medalEl) medalEl.innerHTML = medalLabel(place);
    });

    var list = document.querySelector("#sim-podium ol");
    if (list) {
      var items = Array.prototype.slice.call(
        list.querySelectorAll("li[data-user-id]")
      );
      // Reorder list items to match the new ranking.
      rows.forEach(function (row, index) {
        var li = items.find(function (el) {
          return Number(el.getAttribute("data-user-id")) === row.user.id;
        });
        if (!li) return;
        list.appendChild(li);
        var place = podium[row.user.id] || null;
        var rankEl = li.querySelector(".sim-list-rank");
        var totalEl = li.querySelector(".sim-list-total");
        if (rankEl) rankEl.innerHTML = listRank(place, index);
        if (totalEl) totalEl.textContent = String(Math.trunc(row.total));
      });
    }

    // Sync toggle button styles with current assumptions.
    document.querySelectorAll("#sim-controls li[data-game-id]").forEach(function (li) {
      var gid = li.getAttribute("data-game-id");
      var hc = !!assumptions[gid];
      li.querySelectorAll("button[data-home-covered]").forEach(function (btn) {
        var on = btn.getAttribute("data-home-covered") === "true";
        var isOn = on === hc;
        btn.classList.toggle("sim-on", isOn);
        btn.classList.toggle("sim-off", !isOn);
      });
    });
  }

  document.querySelectorAll("#sim-controls li[data-game-id]").forEach(function (li) {
    var gid = li.getAttribute("data-game-id");
    li.querySelectorAll("button[data-home-covered]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        assumptions[gid] = btn.getAttribute("data-home-covered") === "true";
        paint();
      });
    });
  });

  var reset = document.getElementById("sim-reset");
  if (reset) {
    reset.addEventListener("click", function () {
      assumptions = Object.assign({}, data.defaults);
      paint();
    });
  }
})();
