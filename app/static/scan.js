// Employee scan page: pick a quantity, submit, see instant confirmation.
// Progressive enhancement — the plain <form> POST still works without JS.
(function () {
  var card = document.getElementById("scanCard");
  if (!card) return;
  var tag = card.dataset.tag;
  var caseQty = parseFloat(card.dataset.case || "1") || 1;

  var form = document.getElementById("scanForm");
  var qtyInput = document.getElementById("qtyInput");
  var qtyDisplay = document.getElementById("qtyDisplay");
  var customRow = document.getElementById("customRow");
  var customBtn = document.getElementById("customBtn");
  var caseBtn = document.getElementById("caseBtn");
  var submitBtn = document.getElementById("submitBtn");
  var confirm = document.getElementById("confirm");
  var curCount = document.getElementById("curCount");

  // JS is on: hide the custom field until "Custom" is tapped (no-JS keeps it shown).
  customRow.classList.remove("show");

  function setQty(v) {
    qtyInput.value = v;
    qtyDisplay.textContent = String(v);
    document.querySelectorAll(".qty-btn").forEach(function (b) { b.classList.remove("active"); });
  }

  document.querySelectorAll(".qty-btn[data-qty]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setQty(parseFloat(btn.dataset.qty));
      btn.classList.add("active");
      customRow.classList.remove("show");
    });
  });

  caseBtn.addEventListener("click", function () {
    setQty(caseQty);
    caseBtn.classList.add("active");
    customRow.classList.remove("show");
  });

  customBtn.addEventListener("click", function () {
    customRow.classList.add("show");
    customBtn.classList.add("active");
    qtyInput.focus();
    qtyInput.select();
  });

  qtyInput.addEventListener("input", function () {
    qtyDisplay.textContent = qtyInput.value || "0";
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var qty = parseFloat(qtyInput.value);
    if (!qty || qty <= 0) { flash("Enter a quantity greater than zero.", true); return; }
    submitBtn.disabled = true;
    var data = new FormData();
    data.append("quantity", qty);
    fetch("/api/scan/" + encodeURIComponent(tag), { method: "POST", body: data })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.ok) {
          if (curCount) curCount.textContent = formatNum(res.new_count);
          flash(res.message, false, res.txn_id ? { txnId: res.txn_id, token: res.undo_token } : null);
        } else {
          flash(res.error || "Something went wrong.", true);
          submitBtn.disabled = false;
        }
      })
      .catch(function () { flash("Network error — try again.", true); submitBtn.disabled = false; });
  });

  function formatNum(n) {
    return (Math.round(n * 100) / 100).toString();
  }

  function flash(msg, isError, undo) {
    form.style.display = "none";
    confirm.style.display = "block";
    confirm.className = "scan-confirm" + (isError ? " scan-error" : "");
    var html = "<div>" + msg + "</div>";
    if (undo) {
      html += '<button class="btn ghost" style="margin-top:14px; margin-right:8px" id="undoBtn">Undo</button>';
    }
    html += '<button class="btn secondary" style="margin-top:14px" id="againBtn">Remove more</button>';
    confirm.innerHTML = html;
    document.getElementById("againBtn").addEventListener("click", function () {
      confirm.style.display = "none";
      form.style.display = "block";
      submitBtn.disabled = false;
      setQty(1);
    });
    if (undo) {
      var undoBtn = document.getElementById("undoBtn");
      undoBtn.addEventListener("click", function () {
        undoBtn.disabled = true;
        var data = new FormData();
        data.append("txn_id", undo.txnId);
        data.append("token", undo.token);
        fetch("/api/scan/" + encodeURIComponent(tag) + "/undo", { method: "POST", body: data })
          .then(function (r) { return r.json(); })
          .then(function (res) {
            if (res.ok) {
              if (curCount) curCount.textContent = formatNum(res.new_count);
              flash(res.message, false); // no second undo offered
            } else {
              flash(res.error || "Could not undo.", true);
            }
          })
          .catch(function () { undoBtn.disabled = false; });
      });
    }
  }
})();
