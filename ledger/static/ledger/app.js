// Progressive enhancement only: every page works without this file.
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }

  function toFa(s) {
    return String(s).replace(/[0-9]/g, function (d) { return "۰۱۲۳۴۵۶۷۸۹"[d]; });
  }

  // Confirm dangerous actions: <form data-confirm="...">
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.dataset && f.dataset.confirm && !window.confirm(f.dataset.confirm)) e.preventDefault();
  }, true);

  // Copy buttons: <button data-copy="element-id">
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    var el = document.getElementById(b.dataset.copy);
    if (!el) return;
    var text = el.textContent.trim();
    var done = function () {
      var old = b.textContent;
      b.textContent = "کپی شد ✓";
      setTimeout(function () { b.textContent = old; }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { selectText(el); });
    } else {
      selectText(el);
    }
  });

  function selectText(el) {
    var r = document.createRange();
    r.selectNodeContents(el);
    var s = window.getSelection();
    s.removeAllRanges();
    s.addRange(r);
  }

  // Inbox: categorize with one tap, no page reload.
  // (e.submitter only exists on iOS 15.4+, so remember the tapped chip ourselves.)
  document.addEventListener("click", function (e) {
    var b = e.target.closest(".js-cat button[name]");
    if (b) b.form._tapped = b;
  }, true);

  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f.classList || !f.classList.contains("js-cat") || e.defaultPrevented) return;
    e.preventDefault();
    var btn = e.submitter || f._tapped;
    if (!btn || !btn.name) return;
    var data = new FormData(f);
    data.set(btn.name, btn.value);
    var card = f.closest(".inbox-card");
    Array.prototype.forEach.call(card.querySelectorAll("button"), function (x) { x.disabled = true; });
    fetch(f.action, {
      method: "POST", body: data, credentials: "same-origin",
      headers: { "Accept": "application/json" },
    }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (res) {
      card.classList.add("done");
      setTimeout(function () { card.remove(); }, 260);
      document.querySelectorAll("[data-inbox-count]").forEach(function (el) {
        el.textContent = toFa(res.left);
        if (el.classList.contains("badge") && res.left === 0) el.remove();
      });
      if (!document.querySelector(".inbox-card:not(.done)")) setTimeout(function () { location.reload(); }, 300);
    }).catch(function () {
      // fall back to a normal POST (form.submit() drops the button's value, so carry it)
      var h = document.createElement("input");
      h.type = "hidden";
      h.name = btn.name;
      h.value = btn.value;
      f.appendChild(h);
      f.submit();
    });
  });

  // Chart tooltips: one tooltip listing both series for the month under the pointer or focus.
  document.querySelectorAll(".viz").forEach(function (viz) {
    var tip = document.createElement("div");
    tip.className = "tip hidden";
    tip.setAttribute("role", "status");
    viz.appendChild(tip);
    var active = null;

    function row(value, name, cls) {
      var r = document.createElement("div");
      r.className = "r";
      var k = document.createElement("span");
      k.className = "key line " + cls;
      var b = document.createElement("b");
      b.textContent = value;
      var n = document.createElement("span");
      n.className = "nm";
      n.textContent = name;
      r.append(k, b, n);
      return r;
    }

    function show(g) {
      if (active) active.classList.remove("active");
      active = g;
      g.classList.add("active");
      tip.replaceChildren();
      var h = document.createElement("div");
      h.className = "h";
      h.textContent = g.dataset.title;
      tip.append(h, row(g.dataset.income, viz.dataset.s1, "s1"), row(g.dataset.expense, viz.dataset.s2, "s2"));
      tip.classList.remove("hidden");
      var vb = viz.getBoundingClientRect();
      var gb = g.getBoundingClientRect();
      var left = gb.left - vb.left + gb.width / 2 - tip.offsetWidth / 2;
      left = Math.max(0, Math.min(left, vb.width - tip.offsetWidth));
      tip.style.left = left + "px";   // CSSOM writes are allowed under CSP (not inline style attributes)
      tip.style.top = Math.max(0, gb.top - vb.top - tip.offsetHeight - 6) + "px";
    }

    function hide() {
      if (active) active.classList.remove("active");
      active = null;
      tip.classList.add("hidden");
    }

    viz.querySelectorAll("g.m").forEach(function (g) {
      g.addEventListener("pointerenter", function () { show(g); });
      g.addEventListener("focus", function () { show(g); });
      g.addEventListener("click", function () { active === g ? hide() : show(g); });
      g.addEventListener("blur", hide);
    });
    viz.addEventListener("pointerleave", function (e) { if (e.pointerType === "mouse") hide(); });
  });
})();
