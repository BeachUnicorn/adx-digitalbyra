/* ==========================================================================
   E-postredigeraren i Brev (templates/flamingo/app/utskick/brev_editor.html,
   apps/utskick/app_views/brev.py, README för utskick F.2, F.6 till F.8).

   Blocken sköts av sidbyggarens skript (static/js/flamingo-pb.js, profilen
   "brev"): dra, lägga till, flytta, kopiera, ta bort, versioner och texten
   direkt i mejlet. Det här skriptet lägger till det som bara mejlet har,
   ovanpå window.FlamingoPB:

     avsändaren, ämnesraden, förhandstexten, loggans plats och färgen
                 sparas med blocken (FlamingoPB.saveExtra, ett anrop till
                 spara/ med rev); loggan och färgen ritar om mejlet
                 (FlamingoPB.renderExtra)
     reservtexterna  "Om förnamn saknas" för platshållarna som används
     villkoren   "Uppgifterna stämmer" för villkoren i erbjudandet (F.7)
     Fält        panelen med blockets fält: länkar, datum, tider, koder,
                 telefon, e-post, val, listor och text med fetstil
                 (FlamingoPB.setField); öppnas när ett sådant fält klickas
     Kontroller  email.checks som en lista och en siffra på knappen
     Testmejl    till den inloggades adress (app_utskick_test, kanal epost)
     AI          ett förslag till blocket eller ett nytt block (brev/ai/)
     Förhandsvisa  mejlet som mottagaren ser det: dator, mobil, mörkt läge

   Inga AI-typografitecken och inga utropstecken i texterna här.
   ========================================================================== */
(function () {
  "use strict";

  var root = document.getElementById("pb");
  var configNode = document.getElementById("br-config");
  var pb = window.FlamingoPB;
  if (!root || !configNode || !pb || pb.profile !== "brev") {
    return;
  }

  var config = JSON.parse(configNode.textContent);
  var urls = config.urls || {};
  var csrf = root.getAttribute("data-csrf") || "";
  var readOnly = root.hasAttribute("data-read-only");
  var MOBILE = window.matchMedia("(max-width: 1023px)");
  var TAG_NAMES = ["förnamn", "efternamn", "namn", "företag"];
  var TOKEN_RE = /\{([^{}\n]{1,80})\}/g;
  var ACCENT_RE = /^#[0-9A-Fa-f]{6}$/;

  var doc = {
    subject: config.subject || "",
    preheader: config.preheader || "",
    accent: config.accent || "",
    logo_position: config.logoPosition || "left",
    sender_domain: config.sender && config.sender.domain_id ? config.sender.domain_id : null,
    from_name: (config.sender && config.sender.from_name) || "",
    fallbacks: Object.assign({}, config.fallbacks || {}),
  };
  var terms = { list: config.terms || [], confirmed: !!config.terms_confirmed, pending: null };

  // -------------------------------------------------------------------------
  // Små hjälpare
  // -------------------------------------------------------------------------

  function $(selector, scope) {
    return (scope || document).querySelector(selector);
  }

  function $all(selector, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
  }

  function h(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) {
        return;
      }
      if (key === "class") {
        node.className = value;
      } else if (key === "text") {
        node.textContent = value;
      } else if (key.slice(0, 2) === "on" && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else if (value === true) {
        node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(value));
      }
    });
    for (var i = 2; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === null || child === undefined || child === false) {
        continue;
      }
      (Array.isArray(child) ? child : [child]).forEach(function (c) {
        if (c !== null && c !== undefined && c !== false) {
          node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
        }
      });
    }
    return node;
  }

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function lower(text) {
    return String(text || "").toLowerCase();
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(fn, wait);
    };
  }

  function request(url, opts) {
    opts = opts || {};
    var headers = { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" };
    var body = opts.body;
    if (opts.method === "POST") {
      headers["X-CSRFToken"] = csrf;
      if (body && !(body instanceof FormData)) {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(body);
      }
    }
    return fetch(url, { method: opts.method || "GET", credentials: "same-origin", headers: headers, body: body }).then(
      function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            return { ok: response.ok, status: response.status, data: data || {} };
          });
      },
      function () {
        return { ok: false, status: 0, data: { error: "Ingen kontakt med servern. Försök igen." } };
      }
    );
  }

  function luminance(hex) {
    var c = hex.replace("#", "");
    function f(v) {
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    }
    return 0.2126 * f(parseInt(c.substr(0, 2), 16)) + 0.7152 * f(parseInt(c.substr(2, 2), 16)) + 0.0722 * f(parseInt(c.substr(4, 2), 16));
  }

  /* Ljus färg: kontrasten mot vitt under 4,5 (email.style.is_light). */
  function isLight(hex) {
    return ACCENT_RE.test(hex) && 1.05 / (luminance(hex) + 0.05) < 4.5;
  }

  function schema() {
    var out = {};
    pb.schema().forEach(function (type) {
      out[type.key] = type;
    });
    return out;
  }
  var TYPES = schema();
  var AVAILABLE = {};
  try {
    AVAILABLE = JSON.parse(document.getElementById("pb-config").textContent).available || {};
  } catch (error) {
    AVAILABLE = {};
  }

  function findBlock(id) {
    var blocks = pb.state().blocks || [];
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].id === id) {
        return blocks[i];
      }
    }
    return null;
  }

  function activeFields(block) {
    var versions = (block && block.versions) || [];
    var found = null;
    versions.forEach(function (v) {
      if (v.id === block.active) {
        found = v;
      }
    });
    found = found || versions[versions.length - 1];
    return (found && found.fields) || {};
  }

  function usedBy(spec, variant) {
    return !spec.variants || !spec.variants.length || spec.variants.indexOf(variant) >= 0;
  }

  // -------------------------------------------------------------------------
  // Spara och rita med blocken
  // -------------------------------------------------------------------------

  function shownTags() {
    return $all("[data-br-fallback]").map(function (input) {
      return input.getAttribute("data-br-fallback");
    });
  }

  pb.saveExtra(function () {
    var data = {
      subject: doc.subject,
      preheader: doc.preheader,
      accent: doc.accent,
      logo_position: doc.logo_position,
      sender_domain: doc.sender_domain,
      from_name: doc.sender_domain ? doc.from_name : "",
    };
    var tags = shownTags();
    if (tags.length) {
      data.merge_fallbacks = {};
      tags.forEach(function (tag) {
        data.merge_fallbacks[tag] = doc.fallbacks[tag] || "";
      });
    }
    if (terms.pending) {
      data.terms_ok = true;
      data.terms = terms.pending;
    }
    return data;
  });

  pb.renderExtra(function () {
    return { accent: doc.accent, logo_position: doc.logo_position };
  });

  var rerenderSoon = debounce(function () {
    pb.rerender();
  }, 250);

  function touch() {
    if (!readOnly) {
      pb.touch();
    }
  }

  var statusEl = $("#br-status");

  pb.on("saved", function (payload) {
    var data = (payload && payload.data) || {};
    showErrors("#br-subject-error", data.subject_errors || []);
    showErrors("#br-preheader-error", data.preheader_errors || []);
    if (data.message) {
      pb.toast(data.message);
    }
    if (data.status && statusEl) {
      statusEl.textContent = data.status === "scheduled" ? "Schemalagt" : "Utkast";
      statusEl.className = "fl-badge fl-badge--" + (data.status === "scheduled" ? "queued" : "draft");
    }
    if (data.terms) {
      terms.list = data.terms;
      terms.confirmed = !!data.terms_confirmed;
      if (terms.pending && terms.confirmed) {
        terms.pending = null;
      }
      renderTerms();
    }
    loadChecksSoon();
  });

  function showErrors(selector, errors) {
    var box = $(selector);
    if (!box) {
      return;
    }
    box.innerHTML = "";
    errors.forEach(function (text) {
      box.appendChild(h("p", { class: "fl-field__error", text: text }));
    });
  }

  // -------------------------------------------------------------------------
  // Ämnesraden, förhandstexten och avsändaren
  // -------------------------------------------------------------------------

  var subject = $("#br-subject");
  var preheader = $("#br-preheader");
  var headSubject = $("#br-head-subject");
  var lastText = subject;

  function bindLine(input, key) {
    if (!input) {
      return;
    }
    input.addEventListener("input", function () {
      doc[key] = input.value;
      if (key === "subject" && headSubject) {
        headSubject.textContent = input.value || "Ingen ämnesrad än";
      }
      touch();
      updateFallbacksSoon();
    });
    input.addEventListener("focus", function () {
      lastText = input;
    });
  }
  bindLine(subject, "subject");
  bindLine(preheader, "preheader");

  var insertRow = $("[data-br-insert-row]");
  if (insertRow && !readOnly) {
    (config.tags || []).forEach(function (tag) {
      insertRow.appendChild(
        h("button", {
          type: "button",
          class: "fl-btn fl-btn--ghost fl-btn--sm",
          text: tag.label,
          "aria-label": "Infoga " + lower(tag.label),
          onclick: function () {
            insertAt(lastText || subject, tag.token);
          },
        })
      );
    });
    insertRow.hidden = false;
  }

  function insertAt(input, token) {
    if (!input) {
      return;
    }
    var start = typeof input.selectionStart === "number" ? input.selectionStart : input.value.length;
    var end = typeof input.selectionEnd === "number" ? input.selectionEnd : start;
    input.value = input.value.slice(0, start) + token + input.value.slice(end);
    input.focus();
    var caret = start + token.length;
    try {
      input.setSelectionRange(caret, caret);
    } catch (error) {
      /* inte alla fält har markör */
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  var sender = $("#br-sender");
  var fromRow = $("#br-fromname-row");
  var fromName = $("#br-fromname");
  if (sender) {
    sender.addEventListener("change", function () {
      doc.sender_domain = sender.value ? Number(sender.value) : null;
      if (fromRow) {
        fromRow.hidden = !doc.sender_domain;
      }
      if (doc.sender_domain && fromName && !fromName.value) {
        var option = sender.options[sender.selectedIndex];
        fromName.value = option ? option.getAttribute("data-br-from-name") || "" : "";
        doc.from_name = fromName.value;
      }
      touch();
    });
  }
  if (fromName) {
    fromName.addEventListener("input", function () {
      doc.from_name = fromName.value;
      touch();
    });
  }

  // -------------------------------------------------------------------------
  // Loggan och färgen
  // -------------------------------------------------------------------------

  $all("[data-br-logo]").forEach(function (button) {
    button.addEventListener("click", function () {
      var value = button.getAttribute("data-br-logo");
      if (value === doc.logo_position || readOnly) {
        return;
      }
      doc.logo_position = value;
      $all("[data-br-logo]").forEach(function (b) {
        b.setAttribute("aria-pressed", String(b === button));
      });
      touch();
      pb.rerender();
    });
  });

  var swatches = $all("[data-br-color]");
  var colorInput = $("#br-color");
  var hexInput = $("#br-hex");
  var lightNote = $("#br-light");
  swatches.forEach(function (button) {
    button.style.backgroundColor = button.getAttribute("data-br-color");
    button.addEventListener("click", function () {
      setAccent(button.getAttribute("data-br-color"));
    });
  });

  function setAccent(value, fromHex) {
    value = String(value || "").toUpperCase();
    if (!ACCENT_RE.test(value) || readOnly) {
      return;
    }
    doc.accent = value;
    swatches.forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-br-color").toUpperCase() === value));
    });
    if (colorInput) {
      colorInput.value = value.toLowerCase();
    }
    if (hexInput && !fromHex) {
      hexInput.value = value;
    }
    if (lightNote) {
      lightNote.hidden = !isLight(value);
    }
    touch();
    rerenderSoon();
  }

  if (colorInput) {
    colorInput.addEventListener("input", function () {
      setAccent(colorInput.value);
    });
  }
  if (hexInput) {
    hexInput.addEventListener("input", function () {
      var value = hexInput.value.trim();
      if (value && value.charAt(0) !== "#") {
        value = "#" + value;
      }
      hexInput.classList.toggle("is-error", !!value && !ACCENT_RE.test(value));
      if (ACCENT_RE.test(value)) {
        setAccent(value, true);
      }
    });
  }

  // -------------------------------------------------------------------------
  // Reservtexterna (F.3)
  // -------------------------------------------------------------------------

  var mergeBox = $("#br-merge");
  var mergeRows = $("#br-merge-rows");
  var fieldLabels = {};
  (config.tags || []).forEach(function (tag) {
    fieldLabels[tag.token.slice(1, -1)] = tag.label;
  });

  function tagOf(inner) {
    var name = inner.split("|")[0].trim();
    var low = lower(name);
    if (TAG_NAMES.indexOf(low) >= 0) {
      return low;
    }
    if (low.indexOf("fält:") === 0 && /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/.test(name.slice(5).trim())) {
      return "fält:" + name.slice(5).trim();
    }
    return "";
  }

  function collectTags(value, out) {
    if (typeof value === "string") {
      var match;
      TOKEN_RE.lastIndex = 0;
      while ((match = TOKEN_RE.exec(value))) {
        var tag = tagOf(match[1]);
        if (tag && out.indexOf(tag) < 0) {
          out.push(tag);
        }
      }
    } else if (Array.isArray(value)) {
      value.forEach(function (v) {
        collectTags(v, out);
      });
    } else if (value && typeof value === "object") {
      Object.keys(value).forEach(function (k) {
        collectTags(value[k], out);
      });
    }
    return out;
  }

  function usedTags() {
    var out = collectTags(doc.subject, []);
    collectTags(doc.preheader, out);
    (pb.state().blocks || []).forEach(function (block) {
      collectTags(activeFields(block), out);
    });
    return out;
  }

  function updateFallbacks() {
    if (!mergeBox || !mergeRows) {
      return;
    }
    var tags = usedTags();
    var existing = {};
    $all("[data-br-fallback]", mergeRows).forEach(function (input) {
      existing[input.getAttribute("data-br-fallback")] = input;
    });
    var focused = document.activeElement;
    var same = tags.length === Object.keys(existing).length && tags.every(function (t) {
      return existing[t];
    });
    if (same) {
      return;
    }
    if (focused && mergeRows.contains(focused)) {
      return;
    }
    mergeRows.innerHTML = "";
    tags.forEach(function (tag, i) {
      var id = "br-fallback-" + i;
      var label = tag.indexOf("fält:") === 0 ? "fältet " + (fieldLabels[tag] || tag.slice(5)) : tag;
      var input = h("input", {
        class: "fl-input",
        id: id,
        type: "text",
        maxlength: "60",
        autocomplete: "off",
        placeholder: "Lämna tomt för att hoppa över ordet",
        "data-br-fallback": tag,
        readonly: readOnly ? true : null,
      });
      input.value = doc.fallbacks[tag] || "";
      input.addEventListener("input", function () {
        doc.fallbacks[tag] = input.value;
        touch();
      });
      mergeRows.appendChild(
        h("div", { class: "fl-field fl-br-merge__row" }, h("label", { class: "fl-field__label fl-br-sublabel", for: id, text: "Om " + label + " saknas, skriv" }), input)
      );
    });
    mergeBox.hidden = !tags.length;
  }
  var updateFallbacksSoon = debounce(updateFallbacks, 400);

  // -------------------------------------------------------------------------
  // Villkoren i erbjudandet (F.7)
  // -------------------------------------------------------------------------

  var termsBox = $("#br-terms");
  var termsList = $("#br-terms-list");
  var termsOk = $("#br-terms-ok");

  function renderTerms() {
    if (!termsBox) {
      return;
    }
    termsBox.hidden = !terms.list.length;
    if (termsList) {
      termsList.innerHTML = "";
      terms.list.forEach(function (t) {
        termsList.appendChild(h("li", { text: [t.label, t.value].filter(Boolean).join(" ") }));
      });
    }
    if (termsOk) {
      termsOk.checked = terms.confirmed || !!terms.pending;
      termsOk.disabled = terms.confirmed || readOnly;
    }
  }

  if (termsOk) {
    termsOk.addEventListener("change", function () {
      if (termsOk.checked && terms.list.length) {
        terms.pending = clone(terms.list);
        touch();
        pb.flush().catch(function () {});
      } else {
        terms.pending = null;
      }
    });
  }

  // -------------------------------------------------------------------------
  // Fält: blockets fält som formulär
  // -------------------------------------------------------------------------

  var KIND_HELP = {
    url: "Hela adressen med https://, eller mailto: eller tel:.",
    rich_basic: "Tom rad ger ett nytt stycke. **fet**, *kursiv*, [text](https://...) och rader som börjar med - blir en lista.",
    rich_bold: "Tom rad ger ett nytt stycke. **fet** text blir fet.",
    code: "Stora bokstäver, siffror och bindestreck, 3 till 20 tecken.",
    phone: "Till exempel 08-123 456 78.",
    date: "",
    time: "",
  };
  var ADD_WORDS = {
    columns: "kolumn",
    prices: "tjänst",
    steps: "steg",
    gallery: "bild",
    faq: "fråga",
    social: "länk",
  };

  var fieldsPane = pb.panelEl("falt");
  var fieldsBlock = null;
  var lastPanelInput = null;

  function panelOpen(name) {
    var wrap = document.querySelector('[data-pb-panel="' + name + '"]');
    var panel = $("#pb-panel");
    return !!(wrap && panel && !panel.hidden && !wrap.hidden);
  }

  function fieldId(path) {
    return "br-f-" + path.replace(/[^A-Za-z0-9_-]/g, "-");
  }

  function control(blockId, path, spec, value) {
    var id = fieldId(path);
    var kind = spec.kind;
    var input;
    if (kind === "media") {
      var has = value !== null && value !== undefined && value !== "";
      return h(
        "div",
        { class: "fl-br-media" },
        h("span", { class: "fl-br-media__state", text: has ? "Bild vald" : "Ingen bild" }),
        h("button", {
          type: "button",
          class: "pb-btn pb-btn--ghost pb-btn--sm",
          id: id,
          text: has ? "Byt bild" : "Välj bild",
          disabled: readOnly ? true : null,
          onclick: function () {
            pb.openMedia(blockId, path);
          },
        })
      );
    }
    if (kind === "choice") {
      input = h("select", { class: "fl-input", id: id, disabled: readOnly ? true : null });
      (spec.choices || []).forEach(function (choice) {
        input.appendChild(h("option", { value: choice[0], text: choice[1], selected: value === choice[0] ? true : null }));
      });
      input.addEventListener("change", function () {
        pb.setField(blockId, path, input.value, { delay: 0 });
      });
      return input;
    }
    var multi = kind === "textarea" || kind === "rich_basic";
    if (multi) {
      input = h("textarea", { class: "fl-input fl-br-textarea", id: id, rows: kind === "rich_basic" ? "6" : "3", maxlength: spec.max_length || null, readonly: readOnly ? true : null });
    } else {
      var type = { date: "date", time: "time", email: "email", phone: "tel" }[kind] || "text";
      input = h("input", {
        class: "fl-input",
        id: id,
        type: type,
        maxlength: kind === "date" || kind === "time" ? null : spec.max_length || (kind === "url" ? 500 : null),
        inputmode: kind === "url" ? "url" : null,
        placeholder: kind === "url" ? "https://" : spec.placeholder || null,
        autocomplete: "off",
        spellcheck: kind === "url" || kind === "code" || kind === "email" ? "false" : null,
        readonly: readOnly ? true : null,
      });
    }
    input.value = value === null || value === undefined ? "" : String(value);
    input.setAttribute("data-br-path", path);
    input.addEventListener("focus", function () {
      lastPanelInput = input;
    });
    input.addEventListener("input", function () {
      var next = input.value;
      if (kind === "code") {
        next = next.toUpperCase();
        if (next !== input.value) {
          var at = input.selectionStart;
          input.value = next;
          input.setSelectionRange(at, at);
        }
      }
      pb.setField(blockId, path, next);
    });
    if (kind === "rich_basic") {
      return h("div", { class: "fl-br-rich" }, richBar(input, !!spec.bold_only), input);
    }
    return input;
  }

  /* Knapparna B, I, Länk och Lista för text med formatering (F.6). */
  function richBar(textarea, boldOnly) {
    function wrap(before, after, placeholder) {
      var start = textarea.selectionStart;
      var end = textarea.selectionEnd;
      var picked = textarea.value.slice(start, end) || placeholder;
      textarea.value = textarea.value.slice(0, start) + before + picked + after + textarea.value.slice(end);
      textarea.focus();
      textarea.setSelectionRange(start + before.length, start + before.length + picked.length);
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
    function list() {
      var start = textarea.value.lastIndexOf("\n", textarea.selectionStart - 1) + 1;
      textarea.value = textarea.value.slice(0, start) + "- " + textarea.value.slice(start);
      textarea.focus();
      textarea.setSelectionRange(start + 2, start + 2);
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
    var bar = h("div", { class: "fl-br-richbar", role: "toolbar", "aria-label": "Formatering" });
    function button(text, label, fn, cls) {
      bar.appendChild(h("button", { type: "button", class: "fl-btn fl-btn--ghost fl-btn--sm " + (cls || ""), text: text, "aria-label": label, disabled: readOnly ? true : null, onclick: fn }));
    }
    button("B", "Fetstil", function () {
      wrap("**", "**", "fet text");
    }, "fl-br-richbar__b");
    if (!boldOnly) {
      button("I", "Kursiv", function () {
        wrap("*", "*", "kursiv text");
      }, "fl-br-richbar__i");
      button("Länk", "Länk", function () {
        wrap("[", "](https://)", "länktext");
      });
      button("Lista", "Lista", list);
    }
    return bar;
  }

  function fieldRow(blockId, path, spec, value) {
    var help = spec.kind === "rich_basic" ? KIND_HELP[spec.bold_only ? "rich_bold" : "rich_basic"] : KIND_HELP[spec.kind] || "";
    var labelText = spec.label + (spec.required ? " (krävs)" : "");
    var label = spec.kind === "media"
      ? h("span", { class: "fl-field__label", text: labelText })
      : h("label", { class: "fl-field__label", for: fieldId(path), text: labelText });
    return h("div", { class: "fl-field fl-br-field", "data-br-field": path }, label, control(blockId, path, spec, value), help ? h("p", { class: "fl-field__help", text: help }) : null);
  }

  function emptyItem(spec) {
    var item = {};
    (spec.items || []).forEach(function (sub) {
      if (sub.kind === "media") {
        item[sub.key] = null;
      } else if (sub.kind === "choice" && sub.choices && sub.choices.length) {
        item[sub.key] = sub.choices[0][0];
      } else {
        item[sub.key] = "";
      }
    });
    return item;
  }

  function listEditor(block, type, spec, value) {
    var rows = Array.isArray(value) ? value : [];
    var word = ADD_WORDS[type.key] || lower(spec.item_label || spec.label) || "rad";
    var max = spec.max_items || 0;
    var box = h("fieldset", { class: "fl-br-list", "data-br-field": spec.key }, h("legend", { class: "fl-field__label", text: spec.label }));
    function write(fn) {
      var next = clone(rows);
      fn(next);
      pb.setField(block.id, spec.key, next, { delay: 0 });
    }
    rows.forEach(function (item, i) {
      var row = h("div", { class: "fl-br-list__row" }, h("p", { class: "fl-br-list__n", text: (spec.item_label || spec.label) + " " + (i + 1) }));
      (spec.items || []).forEach(function (sub) {
        if (sub.kind === "key") {
          return;
        }
        row.appendChild(fieldRow(block.id, spec.key + "." + i + "." + sub.key, sub, (item || {})[sub.key]));
      });
      var tools = h("div", { class: "fl-br-list__tools" });
      function tool(text, label, disabled, fn) {
        tools.appendChild(h("button", { type: "button", class: "fl-btn fl-btn--ghost fl-btn--sm", text: text, "aria-label": label, disabled: disabled || readOnly ? true : null, onclick: fn }));
      }
      tool("Upp", "Flytta upp " + word + " " + (i + 1), i === 0, function () {
        write(function (arr) {
          arr.splice(i - 1, 0, arr.splice(i, 1)[0]);
        });
      });
      tool("Ner", "Flytta ner " + word + " " + (i + 1), i === rows.length - 1, function () {
        write(function (arr) {
          arr.splice(i + 1, 0, arr.splice(i, 1)[0]);
        });
      });
      tool("Ta bort", "Ta bort " + word + " " + (i + 1), false, function () {
        write(function (arr) {
          arr.splice(i, 1);
        });
      });
      row.appendChild(tools);
      box.appendChild(row);
    });
    var full = max && rows.length >= max;
    box.appendChild(
      h("button", {
        type: "button",
        class: "pb-btn pb-btn--ghost pb-btn--sm",
        text: "Lägg till " + word,
        disabled: full || readOnly ? true : null,
        onclick: function () {
          write(function (arr) {
            arr.push(emptyItem(spec));
          });
        },
      })
    );
    if (max) {
      box.appendChild(h("p", { class: "fl-field__help", text: "Högst " + max + "." }));
    }
    return box;
  }

  function renderFields(focusPath) {
    if (!fieldsPane) {
      return;
    }
    var state = pb.state();
    var block = findBlock(state.selectedId || fieldsBlock);
    fieldsBlock = block ? block.id : null;
    fieldsPane.innerHTML = "";
    if (!block) {
      fieldsPane.appendChild(h("p", { class: "pb-panel__text", text: "Välj ett block i mejlet." }));
      return;
    }
    var type = TYPES[block.type];
    if (!type) {
      return;
    }
    var fields = activeFields(block);
    fieldsPane.appendChild(h("p", { class: "fl-br-fields__lead", text: type.name }));
    if (type.why) {
      fieldsPane.appendChild(h("p", { class: "pb-panel__text", text: type.why }));
    }
    var form = h("div", { class: "fl-br-fields__form" });
    type.fields.forEach(function (spec) {
      if (!usedBy(spec, block.variant) || spec.kind === "key") {
        return;
      }
      if (spec.kind === "items") {
        form.appendChild(listEditor(block, type, spec, fields[spec.key]));
      } else if (spec.kind === "lines") {
        form.appendChild(fieldRow(block.id, spec.key, { kind: "textarea", label: spec.label, max_length: 2000 }, (fields[spec.key] || []).join("\n")));
      } else {
        form.appendChild(fieldRow(block.id, spec.key, spec, fields[spec.key]));
      }
    });
    if (!form.childNodes.length) {
      form.appendChild(h("p", { class: "pb-panel__text", text: "Blocket har inga fält att fylla i." }));
    }
    fieldsPane.appendChild(form);
    if (!readOnly && (config.tags || []).length) {
      var row = h("div", { class: "fl-br-insert fl-br-insert--panel" }, h("span", { class: "fl-br-insert__label", text: "Infoga" }));
      (config.tags || []).forEach(function (tag) {
        row.appendChild(
          h("button", {
            type: "button",
            class: "fl-btn fl-btn--ghost fl-btn--sm",
            text: tag.label,
            "aria-label": "Infoga " + lower(tag.label) + " i fältet du skrev i senast",
            onclick: function () {
              if (lastPanelInput && lastPanelInput.isConnected) {
                insertAt(lastPanelInput, tag.token);
              } else {
                pb.toast("Klicka i ett fält först.");
              }
            },
          })
        );
      });
      fieldsPane.appendChild(row);
    }
    if (focusPath) {
      var target = document.getElementById(fieldId(focusPath));
      if (target) {
        target.focus();
        if (target.scrollIntoView) {
          target.scrollIntoView({ block: "center" });
        }
      }
    }
  }

  function openFields(path) {
    if (!pb.state().selectedId) {
      return;
    }
    if (!panelOpen("falt")) {
      pb.openPanel("falt", { title: "Fält" });
    }
    renderFields(path);
  }

  pb.on("field", function (payload) {
    if (!payload) {
      return;
    }
    if (payload.blockId && pb.state().selectedId !== payload.blockId) {
      pb.select(payload.blockId);
    }
    openFields(payload.path || null);
  });

  pb.on("select", function (payload) {
    var id = payload && payload.blockId;
    updateMbar(id);
    if (!id) {
      if (panelOpen("falt")) {
        renderFields();
      }
      return;
    }
    if (panelOpen("falt")) {
      renderFields();
    }
  });

  pb.on("panel", function (payload) {
    if (payload && payload.open && payload.name === "falt") {
      renderFields();
    }
    if (payload && payload.open && payload.name === "checks") {
      loadChecks(false);
    }
    if (payload && payload.open && payload.name === "ai") {
      renderAi(payload.blockId || pb.state().selectedId);
    }
  });

  pb.on("change", function (payload) {
    updateFallbacksSoon();
    if (!payload) {
      return;
    }
    if (payload.reason === "media" && payload.blockId && payload.path) {
      makeRendition(payload.blockId, payload.path);
    }
    if (panelOpen("falt") && payload.reason !== "field" && payload.reason !== "text") {
      renderFields();
    } else if (panelOpen("falt") && payload.reason === "text") {
      syncFieldValues();
    } else if (panelOpen("falt") && payload.reason === "field" && /^[a-z_]+$/.test(payload.path || "")) {
      var block = findBlock(payload.blockId);
      var type = block && TYPES[block.type];
      var spec = type ? type.fields.filter(function (f) {
        return f.key === payload.path;
      })[0] : null;
      if (spec && spec.kind === "items") {
        renderFields();
      }
    }
  });

  /* Texten skrevs direkt i mejlet: fälten i panelen följer med, utom det
     som har fokus. */
  function syncFieldValues() {
    var block = findBlock(fieldsBlock);
    if (!block) {
      return;
    }
    var fields = activeFields(block);
    $all("[data-br-path]", fieldsPane).forEach(function (input) {
      if (input === document.activeElement) {
        return;
      }
      var parts = input.getAttribute("data-br-path").split(".");
      var value = parts.reduce(function (o, part) {
        return o === null || o === undefined ? undefined : o[/^\d+$/.test(part) ? +part : part];
      }, fields);
      var text = value === null || value === undefined ? "" : String(value);
      if (input.value !== text) {
        input.value = text;
      }
    });
  }

  // Mobilens rad: Fält.

  var mbarFields = $('#pb-mbar [data-pb-m="falt"]');

  function updateMbar(id) {
    if (mbarFields) {
      mbarFields.setAttribute("aria-disabled", String(!id || readOnly));
    }
  }

  if (mbarFields) {
    mbarFields.addEventListener("click", function () {
      if (mbarFields.getAttribute("aria-disabled") === "true") {
        return;
      }
      openFields(null);
    });
  }

  // Bilderna: en version för e-post när en bild väljs (app_brev_image).

  function purposeFor(path) {
    var key = path.split(".").pop();
    if (key === "thumbnail") {
      return "video";
    }
    if (key === "photo") {
      return "avatar";
    }
    return "content";
  }

  function makeRendition(blockId, path) {
    var block = findBlock(blockId);
    if (!block || !urls.image) {
      return;
    }
    var value = path.split(".").reduce(function (o, part) {
      return o === null || o === undefined ? undefined : o[/^\d+$/.test(part) ? +part : part];
    }, activeFields(block));
    if (value === null || value === undefined || value === "") {
      return;
    }
    request(urls.image, { method: "POST", body: { asset: value, purpose: purposeFor(path) } }).then(function (res) {
      if (!res.ok) {
        pb.toast(res.data.error || "Bilden gick inte att göra om för mejl.");
        return;
      }
      pb.rerender();
    });
  }

  // -------------------------------------------------------------------------
  // Kontrollerna (F.5)
  // -------------------------------------------------------------------------

  var checklist = $("#br-checklist");
  var checksSeq = 0;
  var LEVEL_WORDS = { blocks: "Måste rättas", warns: "Varning", tick: "Klart" };

  function loadChecks(full) {
    if (!urls.checks) {
      return;
    }
    var seq = ++checksSeq;
    var params = [];
    if (previewState.kontakt) {
      params.push("kontakt=" + previewState.kontakt);
    }
    if (full === true) {
      params.push("lankar=1");
    }
    request(urls.checks + (params.length ? "?" + params.join("&") : "")).then(function (res) {
      if (seq !== checksSeq) {
        return;
      }
      if (!res.ok) {
        if (checklist) {
          checklist.innerHTML = "";
          checklist.appendChild(h("li", { class: "fl-br-check fl-br-check--warns" }, h("span", { class: "fl-br-check__text", text: res.data.error || "Kontrollerna gick inte att hämta." })));
        }
        return;
      }
      var items = res.data.items || [];
      var count = items.filter(function (item) {
        return item.level !== "tick";
      }).length;
      pb.badge("checks", count ? String(count) : "");
      if (!checklist) {
        return;
      }
      checklist.innerHTML = "";
      items.forEach(function (item) {
        checklist.appendChild(
          h(
            "li",
            { class: "fl-br-check fl-br-check--" + item.level },
            h("span", { class: "fl-br-check__icon", "aria-hidden": "true" }),
            h("span", { class: "fl-br-check__text" }, h("span", { class: "fl-sr", text: (LEVEL_WORDS[item.level] || "Information") + ": " }), item.text)
          )
        );
      });
      if (!items.length) {
        checklist.appendChild(h("li", { class: "fl-br-check fl-br-check--tick" }, h("span", { class: "fl-br-check__text", text: "Inget att rätta." })));
      }
    });
  }
  var loadChecksSoon = debounce(function () {
    loadChecks(false);
  }, 1200);

  var recheck = $("[data-br-recheck]");
  if (recheck) {
    recheck.addEventListener("click", function () {
      recheck.disabled = true;
      pb.flush()
        .catch(function () {})
        .then(function () {
          loadChecks(true);
          window.setTimeout(function () {
            recheck.disabled = false;
          }, 4000);
        });
    });
  }

  // -------------------------------------------------------------------------
  // Förhandsvisningen (F.4: mobil, dator, mörkt läge)
  // -------------------------------------------------------------------------

  var dialog = $("#br-preview");
  var previewState = { lage: MOBILE.matches ? "mobil" : "dator", kontakt: null, next: null };
  var pvFrame = dialog ? $("[data-br-pv-frame]", dialog) : null;
  var pvStage = dialog ? $("[data-br-pv-stage]", dialog) : null;
  var WIDTHS = { dator: 680, morkt: 680, mobil: 375 };

  function setPv(name, text) {
    var el = dialog ? $('[data-br-pv="' + name + '"]', dialog) : null;
    if (el) {
      el.textContent = text || "";
    }
  }

  function fitPreview() {
    if (!pvFrame || !pvStage) {
      return;
    }
    var width = WIDTHS[previewState.lage] || 680;
    var available = pvStage.clientWidth || width;
    var scale = Math.min(1, available / width);
    pvFrame.style.width = width + "px";
    pvFrame.style.transform = scale < 1 ? "scale(" + scale + ")" : "";
    pvStage.style.height = "";
    if (MOBILE.matches) {
      /* I telefonen fyller dialogen skärmen: ramen fyller resten av den. */
      pvFrame.style.height = Math.max(320, Math.round(pvStage.clientHeight / scale)) + "px";
    }
    var height = pvFrame.offsetHeight;
    pvStage.style.height = Math.round(height * scale) + "px";
  }

  function loadPreview() {
    if (!urls.preview || !dialog) {
      return;
    }
    var params = "?lage=" + encodeURIComponent(previewState.lage) + (previewState.kontakt ? "&kontakt=" + previewState.kontakt : "");
    setPv("who", "Hämtar mejlet");
    request(urls.preview + params).then(function (res) {
      if (!res.ok) {
        setPv("who", res.data.error || "Förhandsvisningen gick inte att hämta.");
        return;
      }
      var data = res.data;
      setPv("from", data.from || "");
      setPv("subject", data.subject || "(ingen ämnesrad)");
      setPv("preheader", data.preheader || "");
      previewState.kontakt = data.kontakt ? data.kontakt.pk : null;
      previewState.next = data.next || null;
      setPv("who", data.kontakt ? "Förhandsvisning med " + data.kontakt.name : "Förhandsvisning utan kontakt: reservtexterna visas.");
      var next = $("[data-br-pv-next]", dialog);
      if (next) {
        next.hidden = !previewState.next;
      }
      var note = $("[data-br-pv-note]", dialog);
      if (note) {
        note.hidden = previewState.lage !== "morkt";
      }
      $all("[data-br-test-kontakt]").forEach(function (input) {
        input.value = previewState.kontakt || "";
      });
      if (pvFrame) {
        pvFrame.srcdoc = data.html || "";
      }
      fitPreview();
    });
  }

  if (dialog) {
    $all("[data-br-lage]", dialog).forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-br-lage") === previewState.lage));
      button.addEventListener("click", function () {
        previewState.lage = button.getAttribute("data-br-lage");
        $all("[data-br-lage]", dialog).forEach(function (b) {
          b.setAttribute("aria-pressed", String(b === button));
        });
        loadPreview();
      });
    });
    var nextButton = $("[data-br-pv-next]", dialog);
    if (nextButton) {
      nextButton.addEventListener("click", function () {
        if (previewState.next) {
          previewState.kontakt = previewState.next;
          loadPreview();
        }
      });
    }
    var closeButton = $("[data-br-preview-close]", dialog);
    if (closeButton) {
      closeButton.addEventListener("click", function () {
        dialog.close();
      });
    }
    window.addEventListener("resize", debounce(fitPreview, 150));
  }

  $all("[data-br-preview]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (!dialog || typeof dialog.showModal !== "function") {
        window.open(urls.preview, "_blank", "noopener");
        return;
      }
      pb.flush()
        .catch(function () {})
        .then(function () {
          dialog.showModal();
          loadPreview();
        });
    });
  });

  // -------------------------------------------------------------------------
  // Testmejlet (F.8)
  // -------------------------------------------------------------------------

  $all("form[data-br-test]").forEach(function (form) {
    var result = $("[data-br-test-result]", form);
    var submitter = null;
    form.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest('button[type="submit"]') : null;
      if (button) {
        submitter = button;
      }
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = event.submitter || submitter;
      var data = new FormData(form);
      if (button && button.name) {
        data.set(button.name, button.value);
      }
      if (button) {
        button.disabled = true;
      }
      if (result) {
        result.textContent = "Skickar testet";
        result.className = "fl-br-result";
      }
      pb.flush()
        .then(function () {
          return request(form.action, { method: "POST", body: data });
        })
        .catch(function () {
          return { ok: false, data: { text: "Mejlet gick inte att spara, så testet skickades inte." } };
        })
        .then(function (res) {
          if (button) {
            button.disabled = false;
          }
          if (result) {
            result.textContent = res.data.text || res.data.error || (res.ok ? "Testet är skickat." : "Testet gick inte att skicka.");
            result.className = "fl-br-result " + (res.ok ? "is-ok" : "is-error");
          }
        });
    });
  });

  // -------------------------------------------------------------------------
  // AI (F.7)
  // -------------------------------------------------------------------------

  var aiPane = pb.panelEl("ai");
  var aiState = { blockId: null, busy: false };
  var GUARD_LINE = "AI använder bara dina bekräftade uppgifter. Inga påhittade omdömen, siffror eller falsk brådska, och inga löften om tider.";

  function fieldLabel(type, key) {
    var spec = (type.fields || []).filter(function (f) {
      return f.key === key;
    })[0];
    return spec ? spec.label : key;
  }

  function showSuggestion(box, type, data, use) {
    box.innerHTML = "";
    if (data.note) {
      box.appendChild(h("p", { class: "pb-panel__note", text: data.note }));
    }
    var list = h("dl", { class: "fl-br-ai__fields" });
    Object.keys(data.fields || {}).forEach(function (key) {
      var value = data.fields[key];
      var text = Array.isArray(value)
        ? value
            .map(function (row) {
              return Object.keys(row || {})
                .map(function (k) {
                  return typeof row[k] === "string" ? row[k] : "";
                })
                .filter(Boolean)
                .join(" · ");
            })
            .join("\n")
        : String(value);
      list.appendChild(h("div", { class: "fl-br-ai__row" }, h("dt", { text: fieldLabel(type, key) }), h("dd", { text: text })));
    });
    box.appendChild(list);
    (data.warnings || []).forEach(function (warning) {
      box.appendChild(h("p", { class: "fl-br-warn", text: warning }));
    });
    if (Object.keys(data.fields || {}).length && !readOnly) {
      box.appendChild(use);
    }
  }

  function aiRequest(body, button, box, after) {
    if (aiState.busy) {
      return;
    }
    aiState.busy = true;
    button.disabled = true;
    var label = button.textContent;
    button.textContent = "Skriver förslag";
    box.innerHTML = "";
    pb.flush()
      .catch(function () {})
      .then(function () {
        return request(urls.ai, { method: "POST", body: body });
      })
      .then(function (res) {
        aiState.busy = false;
        button.disabled = false;
        button.textContent = label;
        if (!res.ok || !res.data.ok) {
          box.appendChild(h("p", { class: "pb-panel__error", role: "alert", text: res.data.error || "AI gick inte att använda just nu. Försök igen om en stund." }));
          return;
        }
        after(res.data);
      });
  }

  function renderAi(blockId) {
    if (!aiPane) {
      return;
    }
    aiState.blockId = blockId || null;
    aiPane.innerHTML = "";
    aiPane.appendChild(h("p", { class: "pb-panel__note", text: GUARD_LINE }));
    if (config.information) {
      aiPane.appendChild(h("p", { class: "pb-panel__note", text: "Det här är information: AI skriver inga erbjudanden, priser eller koder." }));
    }
    var block = blockId ? findBlock(blockId) : null;
    var type = block ? TYPES[block.type] : null;
    if (block && type) {
      var section = h("section", { class: "fl-br-ai" }, h("h3", { class: "fl-br-ai__title", text: "Skriv om " + type.name }));
      var briefId = "br-ai-brief-block";
      var brief = h("textarea", { class: "fl-input", id: briefId, rows: "3", maxlength: "300", placeholder: "Till exempel: påminn om höstservicen och att det går att boka på nätet." });
      var go = h("button", { type: "button", class: "pb-btn pb-btn--sm", text: "Skriv förslag", disabled: readOnly ? true : null });
      var out = h("div", { class: "fl-br-ai__out", "aria-live": "polite" });
      go.addEventListener("click", function () {
        var current = findBlock(block.id);
        aiRequest({ type: block.type, block_id: block.id, variant: block.variant, fields: activeFields(current || block), brief: brief.value }, go, out, function (data) {
          var use = h("button", { type: "button", class: "pb-btn pb-btn--primary pb-btn--sm", text: "Använd förslaget" });
          use.addEventListener("click", function () {
            use.disabled = true;
            pb.addVersion(block.id, data.fields, "ai").then(
              function () {
                use.textContent = "Använt";
                pb.toast("Förslaget används. Den förra texten finns under Versioner.");
              },
              function (error) {
                use.disabled = false;
                out.appendChild(h("p", { class: "pb-panel__error", role: "alert", text: (error && error.message) || "Förslaget gick inte att använda." }));
              }
            );
          });
          showSuggestion(out, type, data, use);
        });
      });
      section.appendChild(h("label", { class: "fl-field__label", for: briefId, text: "Vad ska blocket säga? (valfritt)" }));
      section.appendChild(brief);
      section.appendChild(go);
      section.appendChild(out);
      aiPane.appendChild(section);
    }
    var fresh = h("section", { class: "fl-br-ai" }, h("h3", { class: "fl-br-ai__title", text: "Nytt block med AI" }));
    var selectId = "br-ai-type";
    var select = h("select", { class: "fl-input", id: selectId });
    Object.keys(TYPES).forEach(function (key) {
      var state = AVAILABLE[key] || { ok: true };
      select.appendChild(h("option", { value: key, text: TYPES[key].name + (state.ok ? "" : ": " + (state.reason || "går inte nu")), disabled: state.ok ? null : true }));
    });
    var newBriefId = "br-ai-brief-new";
    var newBrief = h("textarea", { class: "fl-input", id: newBriefId, rows: "3", maxlength: "300", placeholder: "Vad ska blocket handla om?" });
    var newGo = h("button", { type: "button", class: "pb-btn pb-btn--ghost pb-btn--sm", text: "Skriv ett nytt block", disabled: readOnly ? true : null });
    var newOut = h("div", { class: "fl-br-ai__out", "aria-live": "polite" });
    newGo.addEventListener("click", function () {
      var key = select.value;
      aiRequest({ type: key, brief: newBrief.value }, newGo, newOut, function (data) {
        var use = h("button", { type: "button", class: "pb-btn pb-btn--primary pb-btn--sm", text: "Lägg till i mejlet" });
        use.addEventListener("click", function () {
          use.disabled = true;
          pb.addBlock(key, null, { fields: data.fields, source: "ai", afterId: pb.state().selectedId || undefined }).then(
            function () {
              use.textContent = "Tillagt";
            },
            function (error) {
              use.disabled = false;
              newOut.appendChild(h("p", { class: "pb-panel__error", role: "alert", text: (error && error.message) || "Blocket gick inte att lägga till." }));
            }
          );
        });
        showSuggestion(newOut, TYPES[key], data, use);
      });
    });
    fresh.appendChild(h("label", { class: "fl-field__label", for: selectId, text: "Block" }));
    fresh.appendChild(select);
    fresh.appendChild(h("label", { class: "fl-field__label", for: newBriefId, text: "Vad ska blocket säga? (valfritt)" }));
    fresh.appendChild(newBrief);
    fresh.appendChild(newGo);
    fresh.appendChild(newOut);
    aiPane.appendChild(fresh);
    aiPane.appendChild(h("p", { class: "pb-panel__note", text: "Förslaget sparas först när du använder det. Kontrollera alltid texten innan utskicket skickas." }));
  }

  // -------------------------------------------------------------------------
  // Lämna redigeraren: spara först
  // -------------------------------------------------------------------------

  $all("[data-br-leave]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) {
        return;
      }
      event.preventDefault();
      pb.flush().then(
        function () {
          window.location.href = link.href;
        },
        function (res) {
          if (!res || res.status !== 409) {
            window.location.href = link.href;
          }
        }
      );
    });
  });

  // -------------------------------------------------------------------------
  // Start
  // -------------------------------------------------------------------------

  var head = $("#br-head");
  if (head && MOBILE.matches) {
    head.open = false;
  }
  renderTerms();
  updateFallbacks();
  updateMbar(pb.state().selectedId);
  loadChecks(false);
})();
