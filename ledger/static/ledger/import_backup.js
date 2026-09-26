// Import page: read an iPhone backup's sms.db in the browser (sql.js, WebAssembly) and send only
// the bank SMS the user picks. Personal conversations never leave the computer.
(function () {
  "use strict";

  var box = document.getElementById("backup");
  if (!box || !window.fetch) return;
  var fileInput = document.getElementById("backup-file");
  var statusEl = document.getElementById("backup-status");
  var sendersBox = document.getElementById("backup-senders");
  var list = sendersBox.querySelector("ul");
  var sendBtn = document.getElementById("backup-send");
  var resultEl = document.getElementById("backup-result");
  var csrf = document.querySelector("input[name=csrfmiddlewaretoken]");

  var APPLE_EPOCH_MS = 978307200000;  // 2001-01-01, Apple's time zero
  var BATCH = 100;
  // what the Message automation also looks for: the balance line of a bank SMS
  var BANK = /موجود[یي]|مانده/;
  var OTP = new RegExp(box.dataset.otp, "i");
  // Iranian mobile numbers are people, not banks
  var MOBILE = /^(\+?98|0098|0)?9\d{9}$/;

  var messages = [];  // {sender, text, at}
  var senders = {};   // sender -> {checked, sample}

  function fa(n) {
    return String(n).replace(/[0-9]/g, function (d) { return "۰۱۲۳۴۵۶۷۸۹"[d]; });
  }

  function say(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "small " + (cls || "");
  }

  // iOS 16+ keeps many message texts only in attributedBody (an archived NSAttributedString):
  // the text follows the "NSString" class name, after 5 marker bytes and a length.
  var NSSTRING = [78, 83, 83, 116, 114, 105, 110, 103];
  function fromAttributed(b) {
    if (!b || !b.length) return "";
    outer: for (var i = 0; i + NSSTRING.length < b.length; i++) {
      for (var j = 0; j < NSSTRING.length; j++) if (b[i + j] !== NSSTRING[j]) continue outer;
      var p = i + NSSTRING.length + 5, len = b[p];
      if (len === 0x81) { len = b[p + 1] | (b[p + 2] << 8); p += 3; }
      else if (len === 0x82) { len = b[p + 1] | (b[p + 2] << 8) | (b[p + 3] << 16); p += 4; }
      else p += 1;
      return new TextDecoder("utf-8").decode(b.subarray(p, p + len));
    }
    return "";
  }

  function toMs(v) {
    if (v === null || v === undefined) return 0;
    // since iOS 11 in nanoseconds, before that in seconds
    return (v > 1e11 ? v / 1e6 : v * 1000) + APPLE_EPOCH_MS;
  }

  function periodStart() {
    var r = box.querySelector("input[name=backup-period]:checked");
    return r ? Number(r.dataset.start) : 0;
  }

  function selected() {
    var start = periodStart();
    return messages.filter(function (m) { return m.at >= start && senders[m.sender].checked; });
  }

  function render() {
    var start = periodStart(), counts = {};
    messages.forEach(function (m) { if (m.at >= start) counts[m.sender] = (counts[m.sender] || 0) + 1; });
    list.textContent = "";
    Object.keys(senders).sort(function (a, b) { return (counts[b] || 0) - (counts[a] || 0); }).forEach(function (s) {
      var li = document.createElement("li"), label = document.createElement("label");
      var cb = document.createElement("input"), main = document.createElement("span");
      var t1 = document.createElement("span"), t2 = document.createElement("span"), n = document.createElement("span");
      cb.type = "checkbox";
      cb.checked = senders[s].checked;
      cb.addEventListener("change", function () { senders[s].checked = cb.checked; update(); });
      main.className = "main";
      t1.className = "t1 mono";
      t1.textContent = s;
      t2.className = "t2";
      t2.textContent = senders[s].sample;
      n.className = "n";
      n.textContent = fa(counts[s] || 0);
      main.append(t1, t2);
      label.append(cb, main, n);
      li.append(label);
      list.append(li);
    });
    sendersBox.classList.toggle("hidden", !Object.keys(senders).length);
    update();
  }

  function update() {
    var n = selected().length;
    sendBtn.disabled = n === 0;
    sendBtn.textContent = n ? "ارسال " + fa(n) + " پیامک بانک" : "ارسال پیامک‌های بانک";
  }

  function read(file) {
    messages = [];
    senders = {};
    resultEl.textContent = "";
    say("در حال خواندن فایل…");
    var engine = window.initSqlJs({ locateFile: function () { return box.dataset.wasm; } });
    Promise.all([engine, file.arrayBuffer()]).then(function (r) {
      var db = new r[0].Database(new Uint8Array(r[1]));
      try {
        var cols = db.exec("PRAGMA table_info(message)");
        if (!cols.length) throw new Error("not sms.db");
        var hasBody = cols[0].values.some(function (c) { return c[1] === "attributedBody"; });
        var stmt = db.prepare(
          "SELECT h.id, m.text, " + (hasBody ? "m.attributedBody" : "NULL") + ", m.date, m.service " +
          "FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id WHERE m.is_from_me = 0");
        var scanned = 0;
        while (stmt.step()) {
          scanned++;
          var row = stmt.get();
          var sender = row[0] || "?", text = (row[1] || fromAttributed(row[2]) || "").trim();
          if (row[4] === "iMessage" || !text || !BANK.test(text) || OTP.test(text)) continue;
          messages.push({ sender: sender, text: text, at: Math.round(toMs(row[3])) });
          if (!senders[sender]) {
            senders[sender] = { checked: !MOBILE.test(sender.replace(/[\s-]/g, "")) && sender.indexOf("@") < 0, sample: "" };
          }
          senders[sender].sample = text.split("\n")[0].slice(0, 60);  // the newest wins (rows are in order)
        }
        stmt.free();
        messages.sort(function (a, b) { return a.at - b.at; });
        say(messages.length
          ? fa(scanned) + " پیام خوانده شد؛ " + fa(messages.length) + " پیامک بانکی پیدا شد."
          : "در این فایل پیامک بانکی (با «موجودی» یا «مانده») پیدا نشد.", messages.length ? "" : "warn-text");
      } finally {
        db.close();
      }
      render();
    }).catch(function () {
      say("این فایل خوانده نشد. فایل درست (3d0d7e5f… یا sms.db) را انتخاب کنید؛ اگر پشتیبان رمزدار است، اول رمزگذاری را خاموش کنید.", "warn-text");
      render();
    });
  }

  function send() {
    var items = selected(), done = 0, total = items.length, counts = {};
    sendBtn.disabled = true;
    fileInput.disabled = true;
    resultEl.innerHTML = '<div class="progress"><i></i></div><p class="small muted"></p>';
    var bar = resultEl.querySelector("i"), line = resultEl.querySelector("p");
    function next() {
      if (done >= total) return finish();
      var chunk = items.slice(done, done + BATCH).map(function (m) { return { text: m.text, at: m.at }; });
      return fetch(box.dataset.url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf ? csrf.value : "" },
        body: JSON.stringify({ items: chunk }),
      }).then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      }).then(function (res) {
        Object.keys(res.count).forEach(function (k) { counts[k] = (counts[k] || 0) + res.count[k]; });
        done += chunk.length;
        bar.style.width = Math.round(100 * done / total) + "%";
        line.textContent = fa(done) + " از " + fa(total);
        return next();
      });
    }
    function finish() {
      var parts = [fa(counts.created || 0) + " تراکنش تازه ثبت شد", fa(counts.duplicate || 0) + " تکراری بود"];
      if (counts.unparsed) parts.push(fa(counts.unparsed) + " خوانده نشد (در «پیامک‌های خوانده‌نشده» می‌ماند)");
      if (counts.ignored) parts.push(fa(counts.ignored) + " کنار گذاشته شد");
      line.textContent = "✓ " + parts.join(" · ") + ".";
      line.className = "small";
      fileInput.disabled = false;
      update();
    }
    next().catch(function () {
      line.textContent = "ارسال نیمه‌کاره ماند (" + fa(done) + " از " + fa(total) + "). اینترنت را بررسی کنید و دوباره بزنید؛ تکراری‌ها دوباره ثبت نمی‌شوند.";
      line.className = "small warn-text";
      fileInput.disabled = false;
      update();
    });
  }

  fileInput.addEventListener("change", function () {
    if (!fileInput.files.length) return;
    if (!window.initSqlJs) { say("خواننده فایل بارگذاری نشد؛ صفحه را تازه کنید.", "warn-text"); return; }
    read(fileInput.files[0]);
  });
  box.querySelectorAll("input[name=backup-period]").forEach(function (r) { r.addEventListener("change", render); });
  sendBtn.addEventListener("click", send);
})();
