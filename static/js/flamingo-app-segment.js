/* ==========================================================================
   ADX Flamingo, segmentbyggaren (templates/flamingo/app/kontakter/segment.html,
   README I.11, S4). Sidan fungerar utan skript; det här gör den snabbare.

   1. + Villkor, + Grupp (ELLER), + Villkor i gruppen och Ta bort ändrar
      formuläret på sidan i stället för att skicka det. Nya rader klonas ur
      <template data-sg-row-tpl> och grupper ur <template data-sg-group-tpl>;
      "__N__" blir radens nummer (data-sg-next räknar upp, så att varje rad
      har sitt eget) och "__G__" gruppens nyckel. Knapparna låses vid
      gränserna (data-sg-max-rules, data-sg-max-groups).
   2. När fältet byts hämtas jämförelsen och värdet ur
      <template data-sg-tpl="fältet">; när jämförelsen byts visas bara de
      värden vars data-sg-ops har den.
   3. Räkningen: en stund efter en ändring skickas formuläret som det står
      (application/x-www-form-urlencoded, CSRF-token ur formuläret) till
      data-sg-count, och svaret skrivs i [data-sg-total], [data-sg-text],
      [data-sg-note] och [data-sg-lines]; ordet efter talet ([data-sg-unit])
      blir data-sg-one vid 1, annars data-sg-many. Ett svar som kommer efter ett
      nyare skrivs aldrig. Också 400 och 429 har en JSON-kropp med note:
      den visas, och siffrorna blir "-" så att inga gamla siffror står kvar
      som om de gällde. Efter 429 frågar skriptet en gång till efter en
      stund (data-sg-retry sekunder). Ett svar utan JSON ger
      data-sg-error-text.
   4. Knappen "Visa valen för fälten" ([data-sg-nojs]) behövs bara utan
      skript och döljs.
   ========================================================================== */
(function () {
  "use strict";

  var form = document.querySelector("form[data-sg]");
  if (!form) {
    return;
  }
  var items = form.querySelector("[data-sg-items]");
  var rowTpl = document.querySelector("template[data-sg-row-tpl]");
  var groupTpl = document.querySelector("template[data-sg-group-tpl]");
  var fieldTpls = {};
  document.querySelectorAll("template[data-sg-tpl]").forEach(function (tpl) {
    fieldTpls[tpl.getAttribute("data-sg-tpl")] = tpl;
  });
  var next = parseInt(form.getAttribute("data-sg-next"), 10) || 0;
  var groupNext = parseInt(form.getAttribute("data-sg-groups"), 10) || 0;
  var maxRules = parseInt(form.getAttribute("data-sg-max-rules"), 10) || 20;
  var maxGroups = parseInt(form.getAttribute("data-sg-max-groups"), 10) || 5;
  var STAMPED = ["name", "id", "for", "value", "data-sg-n", "data-sg-key"];

  // ------------------------------------------------------------ raderna

  function stamp(root, token, value) {
    var nodes = [root].concat(Array.prototype.slice.call(root.querySelectorAll("*")));
    nodes.forEach(function (node) {
      STAMPED.forEach(function (attr) {
        var current = node.getAttribute(attr);
        if (current && current.indexOf(token) !== -1) {
          node.setAttribute(attr, current.split(token).join(value));
        }
      });
    });
  }

  function applyOp(row) {
    var op = row.querySelector("[data-sg-op]");
    var current = op ? op.value : "";
    row.querySelectorAll("[data-sg-ops]").forEach(function (box) {
      var ops = (box.getAttribute("data-sg-ops") || "").split(" ");
      box.hidden = ops.indexOf(current) === -1;
    });
  }

  function clearError(row) {
    row.classList.remove("is-error");
    var error = row.querySelector(".fl-sg-rule__error");
    if (error) {
      error.remove();
    }
  }

  function swapField(row) {
    var select = row.querySelector("[data-sg-field]");
    var holder = row.querySelector("[data-sg-opval]");
    var tpl = fieldTpls[select.value];
    holder.textContent = "";
    if (tpl) {
      var part = tpl.content.cloneNode(true);
      Array.prototype.forEach.call(part.children, function (child) {
        stamp(child, "__N__", row.getAttribute("data-sg-n"));
      });
      holder.appendChild(part);
    }
    var placeholder = select.querySelector('option[value=""]');
    if (placeholder && select.value) {
      placeholder.remove();
    }
    clearError(row);
    applyOp(row);
  }

  function newRow(groupKey) {
    var row = rowTpl.content.firstElementChild.cloneNode(true);
    stamp(row, "__N__", String(next));
    next += 1;
    var group = row.querySelector("[data-sg-group-input]");
    if (group) {
      group.value = groupKey || "";
    }
    applyOp(row);
    return row;
  }

  function newGroup() {
    groupNext += 1;
    var key = "g" + groupNext;
    var box = groupTpl.content.firstElementChild.cloneNode(true);
    stamp(box, "__G__", key);
    var rows = box.querySelector("[data-sg-rows]");
    rows.appendChild(newRow(key));
    rows.appendChild(newRow(key));
    return box;
  }

  function focusField(row) {
    var field = row && row.querySelector("[data-sg-field]");
    if (field) {
      field.focus();
    }
  }

  function updateButtons() {
    var rules = items.querySelectorAll("[data-sg-row]").length;
    var groups = items.querySelectorAll("[data-sg-groupbox]").length;
    form.querySelectorAll("[data-sg-add], [data-sg-add-to]").forEach(function (button) {
      button.disabled = rules >= maxRules;
    });
    form.querySelectorAll("[data-sg-add-group]").forEach(function (button) {
      button.disabled = rules + 2 > maxRules || groups >= maxGroups;
    });
  }

  form.addEventListener("click", function (event) {
    var button = event.target.closest("button");
    if (!button || !form.contains(button)) {
      return;
    }
    var row;
    if (button.hasAttribute("data-sg-add")) {
      event.preventDefault();
      row = newRow("");
      items.appendChild(row);
      focusField(row);
    } else if (button.hasAttribute("data-sg-add-group")) {
      event.preventDefault();
      var box = newGroup();
      items.appendChild(box);
      focusField(box.querySelector("[data-sg-row]"));
    } else if (button.hasAttribute("data-sg-add-to")) {
      event.preventDefault();
      var group = button.closest("[data-sg-groupbox]");
      row = newRow(group.getAttribute("data-sg-key"));
      group.querySelector("[data-sg-rows]").appendChild(row);
      focusField(row);
    } else if (button.hasAttribute("data-sg-remove")) {
      event.preventDefault();
      row = button.closest("[data-sg-row]");
      var owner = row.closest("[data-sg-groupbox]");
      row.remove();
      if (owner && !owner.querySelector("[data-sg-row]")) {
        owner.remove();
      }
      var add = form.querySelector("[data-sg-add]");
      if (add) {
        add.focus();
      }
    } else {
      return;
    }
    updateButtons();
    schedule();
  });

  form.addEventListener("change", function (event) {
    var target = event.target;
    var row = target.closest("[data-sg-row]");
    if (row && target.hasAttribute("data-sg-field")) {
      swapField(row);
    } else if (row && target.hasAttribute("data-sg-op")) {
      applyOp(row);
    }
    if (row) {
      schedule();
    }
  });

  form.addEventListener("input", function (event) {
    if (event.target.closest("[data-sg-row]") && event.target.matches("input")) {
      schedule();
    }
  });

  // ---------------------------------------------------------- räkningen

  var url = form.getAttribute("data-sg-count");
  var total = form.querySelector("[data-sg-total]");
  var unit = form.querySelector("[data-sg-unit]");
  var text = form.querySelector("[data-sg-text]");
  var note = form.querySelector("[data-sg-note]");
  var lines = form.querySelector("[data-sg-lines]");
  var emptyLine = lines && lines.querySelector(".fl-sg-lines__empty");
  var emptyText = emptyLine ? emptyLine.textContent : "";
  var timer = null;
  var retryTimer = null;
  var asked = 0;
  var retrySeconds = parseInt(form.getAttribute("data-sg-retry"), 10) || 20;
  var errorText = form.getAttribute("data-sg-error-text") || "";

  function csrfToken() {
    var input = form.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function render(data) {
    if (unit) {
      // "1 kontakt", annars "kontakter" (också vid "-").
      var one = data.ok && data.total === 1;
      unit.textContent = unit.getAttribute(one ? "data-sg-one" : "data-sg-many");
    }
    if (!data.ok) {
      // Inga gamla siffror som ser aktuella ut.
      if (total) { total.textContent = "-"; }
      if (text) { text.textContent = "-"; }
    }
    if (data.ok) {
      if (total) { total.textContent = data.total_text; }
      if (text) { text.textContent = data.text; }
      if (lines) {
        lines.textContent = "";
        (data.lines && data.lines.length ? data.lines : [emptyText]).forEach(function (line) {
          var item = document.createElement("li");
          item.textContent = line;
          lines.appendChild(item);
        });
      }
    }
    if (note) {
      note.textContent = data.note || "";
      note.hidden = !data.note;
    }
  }

  function ask() {
    asked += 1;
    var mine = asked;
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrfToken(),
        "X-Requested-With": "XMLHttpRequest",
        Accept: "application/json"
      },
      body: new URLSearchParams(new FormData(form)).toString()
    })
      .then(function (response) {
        // 400 och 429 har också en kropp med note (segment_count).
        return response.json().then(
          function (data) { return { status: response.status, data: data }; },
          function () { return { status: response.status, data: null }; }
        );
      })
      .then(function (result) {
        if (mine !== asked) {
          return;
        }
        var data = result.data;
        if (!data || typeof data !== "object") {
          data = { ok: false, note: errorText };
        }
        render(data);
        if (result.status === 429 && !retryTimer) {
          retryTimer = window.setTimeout(function () {
            retryTimer = null;
            ask();
          }, retrySeconds * 1000);
        }
      })
      .catch(function () {
        if (mine === asked) {
          render({ ok: false, note: errorText });
        }
      });
  }

  function schedule() {
    if (!url || !window.fetch || !window.URLSearchParams) {
      return;
    }
    window.clearTimeout(timer);
    timer = window.setTimeout(ask, 400);
  }

  // ------------------------------------------------------------- start

  form.querySelectorAll("[data-sg-nojs]").forEach(function (element) {
    element.hidden = true;
  });
  form.querySelectorAll("[data-sg-row]").forEach(applyOp);
  updateButtons();
})();
