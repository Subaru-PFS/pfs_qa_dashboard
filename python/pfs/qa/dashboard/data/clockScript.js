// Shared time map for the run report and the night page, paired with clockControls.html. It reads the
// page globals `DATA`, `CLS` and `esc` (declared before this runs) and draws one row per night: by time,
// each visit from its start for as long as it exposed, over the night (18:00 to 07:00) or the whole day;
// in order taken, one square each. The arm and spectrograph selectors re-figure a visit's shown verdict
// from only the detectors kept, so a shift on one detector is not hidden by the rest.
//
// DATA columns: [visit, hst, label, slit, exptime, arms, status, flagged, why, night, detectorsStatus].
(function () {
  const cal = document.getElementById("cal"), tip = document.getElementById("tip");
  if (!cal) return;
  // Per-page starting state, if the page sets CLOCK_DEFAULTS: `layout` ("time" or "order") and `eng`
  // (whether Engineering fibers start shown). Absent (the run report) means By time with engineering off.
  const defaults = typeof CLOCK_DEFAULTS !== "undefined" ? CLOCK_DEFAULTS : {};
  const sel = {
    show: new Set(["science", "cal"]), arm: "", spec: "",
    layout: defaults.layout === "order" ? "order" : "time", night: true,
  };
  // Detector letters: Q no QA method, N no QA result, A taken but not yet judged (night page), U unknown.
  const WORD = { P: "PASS", W: "WARN", F: "FAIL", U: "UNKNOWN", N: "NO RESULT", A: "AWAITING", Q: "NO QA" };
  const RANK = { Q: -3, A: -2, N: -1, U: 0, P: 1, W: 2, F: 3 };
  // A no-QA frame (dark, bias, ...) is its own group; else science, engineering fibers, or calibration.
  const groupOf = (r) => r[6] === "NO QA" ? "noqa" : r[2] === "science" ? "science" : r[3] === "engineering fibers" ? "eng" : "cal";
  // the detectors of a visit that the arm and spectrograph selection keeps, as [name, letter]
  const chosen = (r) => r[10].split(" ").filter(Boolean).map((d) => [d.slice(0, 2), d[2]])
    .filter(([cam]) => (!sel.arm || cam[0] === sel.arm) && (!sel.spec || cam[1] === sel.spec));
  // The worst keeps the list's own letters, so an all-awaiting, all-no-result or all-no-QA visit shows it.
  const worstOf = (list) => list.reduce((w, [, s]) => RANK[s] > RANK[w] ? s : w, list.length ? list[0][1] : "A");
  // Minutes from noon (HST) of the night's date to the visit's start: every night shares that clock.
  const minutesOf = (r) => {
    if (!r[1] || !r[9]) return null;
    const [date, clock] = r[1].split(" "), [hour, minute] = clock.split(":").map(Number);
    return (date === r[9] ? hour - 12 : hour + 12) * 60 + minute;
  };
  const STARTS = DATA.map(minutesOf);
  const lasts = (i) => (+DATA[i][4] || 0) / 60;
  // The clock's span in hours from noon (HST): the night, 18:00 to 07:00, or the whole day.
  const NIGHT = [6, 19], DAY = [0, 24];
  function renderCal() {
    const byTime = sel.layout === "time";
    const nights = new Map();
    const [firstHour, lastHour] = sel.night ? NIGHT : DAY;
    const hours = lastHour - firstHour, from = firstHour * 60, to = lastHour * 60;
    let untimed = 0, outside = 0;
    DATA.forEach((r, i) => {
      if (!sel.show.has(groupOf(r))) return;
      const list = chosen(r);
      if (!list.length) return;
      if (byTime && STARTS[i] === null) { untimed += 1; return; }
      if (byTime && (STARTS[i] + lasts(i) < from || STARTS[i] > to)) { outside += 1; return; }
      if (!nights.has(r[9])) nights.set(r[9], []);
      nights.get(r[9]).push([i, WORD[worstOf(list)]]);
    });
    const along = (minutes) => ((minutes - from) / (hours * 60) * 100).toFixed(3);
    // A visit running over an end of the clock is drawn up to that end.
    const square = ([i, status]) => {
      const start = Math.max(STARTS[i], from), end = Math.min(STARTS[i] + lasts(i), to);
      const place = byTime ? ` style="left:${along(start)}%;width:${((end - start) / (hours * 60) * 100).toFixed(3)}%"` : "";
      return `<a class="sq ${CLS[status]}" href="/visit/${DATA[i][0]}" data-i="${i}"${place} role="img" aria-label="Visit ${DATA[i][0]}, ${status}"></a>`;
    };
    const notes = [];
    if (outside) notes.push(`${outside} ${outside === 1 ? "visit" : "visits"} outside 18:00 to 07:00 ${outside === 1 ? "is" : "are"} not drawn.`);
    if (untimed) notes.push(`${untimed} ${untimed === 1 ? "visit" : "visits"} without a time ${untimed === 1 ? "is" : "are"} not drawn.`);
    let axis = "";
    if (byTime && nights.size) {
      // As many hour labels as fit the width.
      const step = Math.max(1, Math.ceil(hours * 52 / Math.max(cal.clientWidth - 120, 200)));
      const ticks = [];
      for (let hour = firstHour; hour <= lastHour; hour += step) ticks.push(`<span style="left:${along(hour * 60)}%">${String((hour + 12) % 24).padStart(2, "0")}:00</span>`);
      axis = `<div class="night"></div><div class="axis">${ticks.join("")}</div>`;
    }
    cal.style.setProperty("--hours", hours);
    // Each night's date label links to that night's page: the latest to "/" (the canonical live night),
    // the rest to /night/<date>. Only when more than one night is shown (the run report), so the
    // single-night night page keeps a plain label.
    const entries = [...nights.entries()].sort();
    const latestNight = entries.length ? entries[entries.length - 1][0] : null;
    const linkNights = entries.length > 1;
    const nightLabel = (night) =>
      linkNights && /^\d{4}-\d{2}-\d{2}$/.test(night)
        ? `<a href="${night === latestNight ? "/" : "/night/" + night}">${night}</a>`
        : night;
    cal.innerHTML = (axis + entries.map(([night, list]) =>
      `<div class="night">${nightLabel(night)}</div><div class="cells${byTime ? " time" : ""}">` + list.map(square).join("") + "</div>").join("")
      + notes.map((note) => `<div class="night"></div><div class="night">${note}</div>`).join(""))
      || `<div class="night">${sel.show.size ? "No visit has the selected detectors." : "No kind of frame is selected."}</div>`;
    tip.hidden = true;
  }
  function showTip(sq, x, y) {
    const r = DATA[+sq.dataset.i], list = chosen(r), status = WORD[worstOf(list)];
    const bad = list.filter(([, s]) => s === "W" || s === "F").map(([cam, s]) => `${cam} ${WORD[s]}`);
    const which = sel.arm || sel.spec ? `detectors ${list.map(([cam]) => cam).join(" ")}` : `${list.length} detectors`;
    const detail = status === "NO RESULT" || status === "AWAITING"
      ? esc(r[8]) || (status === "AWAITING" ? "awaiting QA" : "")
      : bad.length ? esc(bad.join(", ")) : "on " + which;
    tip.innerHTML = `<b>Visit ${r[0]}</b><br>${esc(r[1])} HST · ${r[4]} s<br>${esc(r[2])}${r[3] ? ", " + r[3] : ""}<br>` +
      `<span class="chip ${CLS[status]}">${status}</span> ${detail}`;
    tip.hidden = false;
    tip.style.left = Math.max(8, Math.min(x + 12, window.innerWidth - tip.offsetWidth - 8)) + "px";
    tip.style.top = (y + 16 + tip.offsetHeight > window.innerHeight ? y - tip.offsetHeight - 10 : y + 16) + "px";
  }
  cal.addEventListener("mousemove", (e) => { const sq = e.target.closest(".sq"); if (sq) showTip(sq, e.clientX, e.clientY); else tip.hidden = true; });
  cal.addEventListener("mouseleave", () => { tip.hidden = true; });
  cal.addEventListener("click", (e) => { const sq = e.target.closest(".sq"); if (sq) { const b = sq.getBoundingClientRect(); showTip(sq, b.left, b.bottom - 12); } });
  document.querySelectorAll(".seg").forEach((group) => group.addEventListener("click", (e) => {
    const button = e.target.closest("button");
    if (!button) return;
    group.querySelectorAll("button").forEach((o) => o.setAttribute("aria-pressed", String(o === button)));
    sel[group.dataset.group] = button.dataset.value;
    renderCal();
  }));
  const fNight = document.getElementById("f-night");
  // Apply the per-page defaults to the controls before reading them: show Engineering fibers if asked, and
  // reflect the chosen layout in the toggle and the night-only switch (which only applies to By time).
  if (defaults.eng) { const eng = document.getElementById("f-show-eng"); if (eng) eng.checked = true; }
  // The "Darks & biases" toggle appears only when the page has such frames (the run report, not the night page).
  const noqaBox = document.getElementById("f-show-noqa");
  if (noqaBox && !DATA.some((r) => r[6] === "NO QA")) { const l = noqaBox.closest("label"); if (l) l.hidden = true; }
  document.querySelectorAll('.seg[data-group="layout"] button').forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.value === sel.layout)));
  fNight.disabled = sel.layout !== "time";
  document.querySelectorAll("input[data-show]").forEach((box) => box.addEventListener("change", () => {
    if (box.checked) sel.show.add(box.dataset.show); else sel.show.delete(box.dataset.show);
    renderCal();
  }));
  fNight.addEventListener("change", () => { sel.night = fNight.checked; renderCal(); });
  document.querySelector('.seg[data-group="layout"]').addEventListener("click", () => { fNight.disabled = sel.layout !== "time"; });
  // Start from what the controls now show (the per-page defaults above, kept across a reload).
  sel.show = new Set([...document.querySelectorAll("input[data-show]")].filter((box) => box.checked).map((box) => box.dataset.show));
  sel.night = fNight.checked;
  renderCal();
  window.addEventListener("resize", renderCal);
})();
