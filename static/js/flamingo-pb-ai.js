/*
 * Sidbyggarens AI-panel och Konverteringskoll (mockupen skärm 05 och 06).
 *
 * Laddas av templates/flamingo/app/pages/editor.html efter flamingo-pb.js
 * och ritar i redigerarens paneler genom window.FlamingoPB:
 *
 *   panelEl("ai")    "Bygg sidan åt mig" (formulär, förslaget och varför,
 *                    "Använd förslaget") och "Skriv om" för det valda blocket
 *   panelEl("koll")  Konverteringskollen: ringen "7 av 9" och punkterna, de
 *                    som inte är ok först, var och en med en knapp som gör
 *                    jobbet (addBlock, select, openPanel eller en länk)
 *
 * Servern (app_views/page_ai.py) gör allt arbete och sparar ingenting:
 * förslaget blir utkast först när kunden väljer "Använd förslaget"
 * (FlamingoPB.setBlocks), och ett förslag från "Skriv om" blir en ny version
 * av blocket (FlamingoPB.addVersion med källan "ai").
 *
 * All text från servern sätts med textContent, aldrig som HTML.
 */
(function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var started = false;

  // ---------------------------------------------------------------------
  // Små hjälpare
  // ---------------------------------------------------------------------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === "text") node.textContent = value;
        else if (key === "className") node.className = value;
        else if (key === "hidden") node.hidden = true;
        else if (key === "disabled") node.disabled = true;
        else node.setAttribute(key, value === true ? "" : value);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  /* En ikon ur redigerarens symboler (#pb-u-..., #pb-i-...). */
  function icon(id, cls) {
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "pb-ic " + (cls || ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var use = document.createElementNS(SVG, "use");
    use.setAttribute("href", "#" + id);
    svg.appendChild(use);
    return svg;
  }

  /* Skölden vid AI-regeln (finns inte bland redigerarens symboler). */
  function shield() {
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "pb-ic pba-guard__icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var path = document.createElementNS(SVG, "path");
    path.setAttribute("d", "M12 3l7 3v5c0 4.5-3 8.4-7 10-4-1.6-7-5.5-7-10V6l7-3z");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.8");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function csrf() {
    var root = document.getElementById("pb");
    if (root && root.getAttribute("data-csrf")) return root.getAttribute("data-csrf");
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function readOnly() {
    var root = document.getElementById("pb");
    return !!(root && root.hasAttribute("data-read-only"));
  }

  function request(url, body) {
    var options = {
      method: body ? "POST" : "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    };
    if (body) {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-CSRFToken"] = csrf();
      options.body = JSON.stringify(body);
    }
    return fetch(url, options).then(function (response) {
      var type = response.headers.get("Content-Type") || "";
      if (type.indexOf("application/json") === -1) {
        throw new Error("Svaret gick inte att läsa. Ladda om sidan och försök igen.");
      }
      return response.json().then(function (data) {
        if (!response.ok) {
          throw new Error((data && data.error) || "Något gick fel. Försök igen.");
        }
        return data;
      });
    });
  }

  function idOf(payload) {
    if (typeof payload === "string") return payload;
    if (payload && typeof payload === "object") return payload.id || payload.blockId || payload.block_id || "";
    return "";
  }

  function nameOf(payload) {
    if (typeof payload === "string") return payload;
    if (payload && typeof payload === "object") return payload.name || payload.panel || "";
    return "";
  }

  /* Panelen öppnades (händelsen "panel" är {name, open, blockId}). */
  function opened(payload, name) {
    if (nameOf(payload) !== name) return false;
    return !(payload && typeof payload === "object" && payload.open === false);
  }

  /* Redigerarens funktioner ger ett löfte; ett fel ska aldrig fälla panelen. */
  function settle(result, onError) {
    return Promise.resolve(result).catch(function (err) {
      if (onError) onError(err);
      else if (window.console) window.console.error(err);
    });
  }

  function activeFields(block) {
    var versions = (block && block.versions) || [];
    var active = null;
    for (var i = 0; i < versions.length; i++) {
      if (versions[i].id === block.active) active = versions[i];
    }
    active = active || versions[versions.length - 1];
    return JSON.parse(JSON.stringify((active && active.fields) || {}));
  }

  function findBlock(pb, id) {
    var blocks = (pb.state() || {}).blocks || [];
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].id === id) return blocks[i];
    }
    return null;
  }

  /* Sökvägen "steps.1.text" i fälten satt till text (en ny rad läggs till). */
  function setPath(fields, path, text) {
    var parts = path.split(".");
    if (parts.length === 1) {
      fields[parts[0]] = text;
    } else if (parts.length === 2) {
      var lines = Array.isArray(fields[parts[0]]) ? fields[parts[0]] : [];
      lines[Number(parts[1])] = text;
      fields[parts[0]] = lines.filter(function (line) { return line !== undefined; });
    } else {
      var items = Array.isArray(fields[parts[0]]) ? fields[parts[0]] : [];
      var item = items[Number(parts[1])];
      if (!item) return false;
      item[parts[2]] = text;
      fields[parts[0]] = items;
    }
    return true;
  }

  function tag(label) {
    return el("span", { className: "pba-tag", text: label });
  }

  function spinner() {
    return el("span", { className: "pba-spin", "aria-hidden": "true" });
  }

  function setBusy(button, busy, label) {
    button.disabled = !!busy;
    button.setAttribute("aria-busy", busy ? "true" : "false");
    clear(button);
    if (busy) button.appendChild(spinner());
    else if (button.getAttribute("data-icon")) button.appendChild(icon(button.getAttribute("data-icon")));
    button.appendChild(document.createTextNode(label));
  }

  function hideFallback(pane) {
    var wrap = pane.parentNode;
    if (!wrap) return;
    var fallback = wrap.querySelector(".pb-panel__fallback");
    if (fallback) fallback.hidden = true;
  }

  /* Knappen för en åtgärd från servern (kollen och "Saknas"). */
  function actionButton(pb, action, ghost) {
    if (!action || !action.kind) return null;
    var label = action.label || "Visa";
    var cls = "fl-btn fl-btn--sm" + (ghost ? " fl-btn--ghost" : "");
    if (action.kind === "link") {
      var url = String(action.url || "");
      if (url.charAt(0) !== "/" || url.charAt(1) === "/") return null;
      return el("a", { className: cls, href: url, text: label });
    }
    var button = el("button", { type: "button", className: cls, text: label });
    if (action.kind === "add_block" && readOnly()) button.disabled = true;
    button.addEventListener("click", function () {
      try {
        if (action.kind === "add_block") {
          var options = {};
          if (action.after_id) options.afterId = action.after_id;
          button.disabled = true;
          settle(pb.addBlock(action.type, action.variant || null, options)).then(function () {
            button.disabled = false;
          });
        } else if (action.kind === "select_block") {
          pb.select(action.block_id);
        } else if (action.kind === "open_panel") {
          pb.openPanel(action.panel);
        }
      } catch (err) {
        if (window.console) window.console.error(err);
      }
    });
    return button;
  }

  // ---------------------------------------------------------------------
  // AI-panelen
  // ---------------------------------------------------------------------

  var GUARD =
    "AI använder bara dina bekräftade uppgifter. Inga påhittade omdömen, siffror eller falsk brådska, och inga löften om tider.";

  function mountAI(pb, pane) {
    var urls = pb.urls || {};
    if (!urls.ai_build || !urls.ai_rewrite) return;
    hideFallback(pane);
    clear(pane);

    var state = { form: null, proposal: null, loaded: false, loading: false, selected: "", suggestions: null };

    // Bygg sidan åt mig ------------------------------------------------
    var goalBox = el("div", { className: "pba-seg", role: "radiogroup", "aria-labelledby": "pba-goal-label" });
    var toneBox = el("div", { className: "pba-seg", role: "radiogroup", "aria-labelledby": "pba-tone-label" });
    var service = el("select", { className: "pba-select", id: "pba-service" });
    var chips = el("ul", { className: "pba-chips", "aria-label": "AI använder" });
    var missingList = el("ul", { className: "pba-missing__list" });
    var missingBox = el("div", { className: "pba-field pba-missing", hidden: true }, [
      el("p", { className: "pba-label", text: "Saknas" }),
      missingList
    ]);
    var aiNote = el("p", { className: "pba-note", hidden: true });
    var buildBtn = el("button", { type: "submit", className: "fl-btn pba-go", "data-icon": "pb-u-sparkle" });
    setBusy(buildBtn, false, "Bygg sidan");
    var buildError = el("p", { className: "pba-error", role: "alert", hidden: true });
    var form = el("form", { className: "pba-form", novalidate: true }, [
      el("div", { className: "pba-field" }, [
        el("p", { className: "pba-label", id: "pba-goal-label", text: "Hur köper kunderna?" }),
        goalBox
      ]),
      el("div", { className: "pba-field" }, [
        el("label", { className: "pba-label", for: "pba-service", text: "Tjänst" }),
        service
      ]),
      el("div", { className: "pba-field" }, [
        el("p", { className: "pba-label", id: "pba-tone-label", text: "Ton" }),
        toneBox
      ]),
      el("div", { className: "pba-field" }, [
        el("p", { className: "pba-label", text: "AI använder" }),
        chips
      ]),
      missingBox,
      aiNote,
      el("div", { className: "pba-actions" }, [buildBtn]),
      buildError
    ]);
    var buildCard = el("section", { className: "pba-card", "aria-labelledby": "pba-build-title" }, [
      el("div", { className: "pba-head" }, [
        el("span", { className: "pba-spark" }, [icon("pb-u-sparkle")]),
        el("h3", { className: "pba-title", id: "pba-build-title", text: "Bygg sidan åt mig" })
      ]),
      el("p", { className: "pba-text", text: "Blocken väljs efter hur kunderna köper, och texterna skrivs ur dina bekräftade uppgifter. Du ser förslaget innan något sparas." }),
      form
    ]);

    // Förslaget och varför ------------------------------------------------
    var resultSource = el("p", { className: "pba-source" });
    var resultList = el("ol", { className: "pba-list" });
    var resultProblems = el("p", { className: "pba-note pba-note--warn", hidden: true });
    var useBtn = el("button", { type: "button", className: "fl-btn", text: "Använd förslaget" });
    var againBtn = el("button", { type: "button", className: "fl-btn fl-btn--ghost", "data-icon": "pb-u-sparkle" });
    setBusy(againBtn, false, "Bygg om");
    var resultDone = el("p", { className: "pba-done", role: "status" });
    var resultCard = el("section", { className: "pba-card pba-result", "aria-labelledby": "pba-result-title", hidden: true }, [
      el("h3", { className: "pba-title", id: "pba-result-title", tabindex: "-1", text: "Förslaget och varför" }),
      resultSource,
      resultList,
      resultProblems,
      el("p", { className: "pba-small", text: "Förslaget ersätter blocken i utkastet. Inget publiceras förrän du publicerar sidan." }),
      el("div", { className: "pba-actions" }, [useBtn, againBtn]),
      resultDone
    ]);

    // Skriv om ----------------------------------------------------------
    var rwHint = el("p", { className: "pba-text", text: "Välj ett block på sidan, sedan fältet du vill skriva om." });
    var rwField = el("select", { className: "pba-select", id: "pba-rw-field" });
    var rwBtn = el("button", { type: "button", className: "fl-btn fl-btn--ghost", "data-icon": "pb-u-sparkle" });
    setBusy(rwBtn, false, "Ge tre förslag");
    var rwBox = el("div", { className: "pba-rw", hidden: true }, [
      el("div", { className: "pba-field" }, [
        el("label", { className: "pba-label", for: "pba-rw-field", text: "Fält" }),
        rwField
      ]),
      el("div", { className: "pba-actions" }, [rwBtn])
    ]);
    var rwSource = el("p", { className: "pba-source", hidden: true });
    var rwList = el("ul", { className: "pba-suggs" });
    var rwError = el("p", { className: "pba-error", role: "alert", hidden: true });
    var rwTitle = el("h3", { className: "pba-title", id: "pba-rw-title", text: "Skriv om" });
    var rwCard = el("section", { className: "pba-card", "aria-labelledby": "pba-rw-title" }, [
      el("div", { className: "pba-head" }, [el("span", { className: "pba-spark" }, [icon("pb-u-sparkle")]), rwTitle]),
      rwHint,
      rwBox,
      rwSource,
      rwList,
      rwError
    ]);

    var guard = el("p", { className: "pba-guard" }, [shield(), el("span", { text: GUARD })]);
    var shell = el("div", { className: "pba" }, [buildCard, resultCard, rwCard, guard]);
    pane.appendChild(shell);

    /* Öppnad från ett block ("Skriv om" i blockets verktyg): Skriv om
       först. Från knappen AI överst: Bygg sidan åt mig först. */
    function order(forBlock) {
      if (forBlock && shell.firstChild !== rwCard) shell.insertBefore(rwCard, buildCard);
      if (!forBlock && shell.firstChild === rwCard) shell.insertBefore(rwCard, guard);
    }

    if (readOnly()) {
      buildBtn.disabled = true;
      useBtn.disabled = true;
    }

    function radios(box, name, options, value) {
      clear(box);
      options.forEach(function (option) {
        var id = "pba-" + name + "-" + option.key;
        var input = el("input", { type: "radio", className: "pba-seg__input", name: "pba-" + name, id: id, value: option.key });
        input.checked = option.key === value;
        box.appendChild(el("span", { className: "pba-seg__item" }, [
          input,
          el("label", { className: "pba-seg__label", for: id, text: option.label })
        ]));
      });
    }

    function checked(name) {
      var input = form.querySelector('input[name="pba-' + name + '"]:checked');
      return input ? input.value : "";
    }

    function renderFacts(data) {
      clear(chips);
      (data.used_facts || []).forEach(function (fact) {
        chips.appendChild(el("li", { className: "pba-chip", title: fact.label + " (" + (fact.source || "") + ")" }, [
          icon("pb-u-check", "pba-chip__icon"),
          el("span", { text: fact.value })
        ]));
      });
      if (!chips.firstChild) chips.appendChild(el("li", { className: "pba-chip pba-chip--none", text: "Inga bekräftade uppgifter än" }));
      clear(missingList);
      (data.missing || []).forEach(function (item) {
        missingList.appendChild(el("li", { className: "pba-missing__item" }, [
          el("p", { className: "pba-missing__text" }, [el("b", { text: item.label + ". " }), item.hint]),
          actionButton(pb, item.action, true)
        ]));
      });
      missingBox.hidden = !missingList.firstChild;
    }

    function renderForm(data) {
      state.form = data;
      radios(goalBox, "goal", data.goals || [], data.goal);
      radios(toneBox, "tone", data.tones || [], data.tone || "saklig");
      clear(service);
      (data.services || []).forEach(function (s) {
        var option = el("option", { value: String(s.id), text: s.name });
        option.selected = s.id === data.service_id;
        service.appendChild(option);
      });
      service.disabled = !(data.services || []).length;
      renderFacts(data);
      var ai = data.ai || {};
      aiNote.textContent = ai.available ? "" : ai.note || "";
      aiNote.hidden = !!ai.available || !ai.note;
      if (data.guard) guard.lastChild.textContent = data.guard;
      updateRewrite();
    }

    function loadForm(extra) {
      var url = urls.ai_build;
      var params = [];
      if (extra && extra.service_id) params.push("service_id=" + encodeURIComponent(extra.service_id));
      if (extra && extra.goal) params.push("goal=" + encodeURIComponent(extra.goal));
      if (params.length) url += (url.indexOf("?") === -1 ? "?" : "&") + params.join("&");
      return request(url).then(function (data) {
        state.loaded = true;
        renderForm(data);
      }).catch(function (err) {
        buildError.textContent = err.message;
        buildError.hidden = false;
      });
    }

    service.addEventListener("change", function () {
      var chosen = null;
      (state.form && state.form.services || []).forEach(function (s) {
        if (String(s.id) === service.value) chosen = s;
      });
      // Tjänstens sätt att sälja blir förvalet, uppgifterna läses om.
      loadForm({ service_id: service.value, goal: chosen ? chosen.goal : checked("goal") });
    });

    function blockName(type) {
      var fields = (state.form && state.form.fields) || {};
      return (fields[type] && fields[type].name) || type;
    }

    function blockIcon(type) {
      var fields = (state.form && state.form.fields) || {};
      return (fields[type] && fields[type].icon) || "hero";
    }

    function renderResult(data) {
      state.proposal = data;
      var byId = {};
      (data.blocks || []).forEach(function (block) { byId[block.id] = block; });
      var formNote = (state.form && state.form.ai && state.form.ai.note) || "";
      resultSource.textContent = data.source === "ai"
        ? "Skrivet av AI ur dina bekräftade uppgifter och kontrollerat."
        : data.note && data.note !== formNote
          ? data.note
          : "Bygger på mallarna, bara ur dina bekräftade uppgifter.";
      clear(resultList);
      (data.explanations || []).forEach(function (item) {
        var block = byId[item.block_id] || {};
        resultList.appendChild(el("li", { className: "pba-li" }, [
          el("span", { className: "pba-li__icon" }, [icon("pb-i-" + blockIcon(block.type))]),
          el("div", { className: "pba-li__grow" }, [
            el("b", { className: "pba-li__title", text: item.title || blockName(block.type) }),
            el("p", { className: "pba-li__text", text: item.text }),
            el("div", { className: "pba-tags" }, [tag(item.principle_label)])
          ])
        ]));
      });
      var problems = data.problems || [];
      resultProblems.textContent = problems.length
        ? "Kontrollerna vill att du tittar på: " + problems.map(function (p) { return (p.where ? p.where + ": " : "") + p.message; }).join(" ")
        : "";
      resultProblems.hidden = !problems.length;
      resultDone.textContent = "";
      useBtn.disabled = readOnly() || !(data.blocks || []).length;
      resultCard.hidden = false;
      renderFacts({ used_facts: data.used_facts, missing: data.missing });
    }

    function build() {
      if (state.loading) return;
      state.loading = true;
      buildError.hidden = true;
      setBusy(buildBtn, true, "AI bygger sidan");
      setBusy(againBtn, true, "Bygger om");
      request(urls.ai_build, {
        goal: checked("goal"),
        tone: checked("tone"),
        service_id: service.value ? Number(service.value) : null
      }).then(function (data) {
        renderResult(data);
        var title = document.getElementById("pba-result-title");
        if (title) title.focus();
      }).catch(function (err) {
        buildError.textContent = err.message;
        buildError.hidden = false;
      }).then(function () {
        state.loading = false;
        setBusy(buildBtn, false, "Bygg sidan");
        setBusy(againBtn, false, "Bygg om");
        if (readOnly()) buildBtn.disabled = true;
      });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      build();
    });
    againBtn.addEventListener("click", build);
    useBtn.addEventListener("click", function () {
      if (!state.proposal || readOnly()) return;
      useBtn.disabled = true;
      resultDone.textContent = "";
      var failed = false;
      settle(
        (function () {
          try {
            return pb.setBlocks(state.proposal.blocks, { source: "ai" });
          } catch (err) {
            return Promise.reject(err);
          }
        })(),
        function (err) {
          failed = true;
          useBtn.disabled = false;
          buildError.textContent = (err && err.message) || "Förslaget gick inte att använda. Försök igen.";
          buildError.hidden = false;
        }
      ).then(function () {
        if (!failed) {
          resultDone.textContent = "Förslaget ligger nu i utkastet. Ändra fritt, och publicera när du är nöjd.";
        }
      });
    });

    // Skriv om: fälten för det valda blocket --------------------------------
    function fieldOptions(block) {
      var schema = (state.form && state.form.fields && state.form.fields[block.type]) || null;
      if (!schema) return [];
      var limits = (schema.variants && schema.variants[block.variant]) || {};
      var fields = activeFields(block);
      var out = [];
      schema.fields.forEach(function (spec) {
        if (spec.variants && spec.variants.length && spec.variants.indexOf(block.variant) === -1) return;
        var one = itemLabel(block.type, spec);
        if (spec.kind === "text" || spec.kind === "textarea") {
          out.push({ path: spec.key, label: spec.label });
        } else if (spec.kind === "lines") {
          var lines = Array.isArray(fields[spec.key]) ? fields[spec.key] : [];
          lines.forEach(function (_line, i) { out.push({ path: spec.key + "." + i, label: one + " " + (i + 1) }); });
        } else if (spec.kind === "items") {
          var items = Array.isArray(fields[spec.key]) ? fields[spec.key] : [];
          var limit = typeof limits[spec.key] === "number" ? limits[spec.key] : items.length;
          items.slice(0, limit).forEach(function (_item, i) {
            (spec.items || []).forEach(function (sub) {
              var same = sub.label.toLowerCase() === one.toLowerCase();
              out.push({ path: spec.key + "." + i + "." + sub.key, label: one + " " + (i + 1) + (same ? "" : ", " + sub.label.toLowerCase()) });
            });
          });
        }
      });
      return out;
    }

    /* En rad i en lista heter i singular ("Punkt 2", inte "Punkter 2"):
       item_label ur AI-schemat eller redigerarens schema, annars etiketten. */
    var editorSchema = null;
    function itemLabel(typeKey, spec) {
      if (spec.item_label) return spec.item_label;
      if (!editorSchema) editorSchema = typeof pb.schema === "function" ? pb.schema() : [];
      var found = "";
      editorSchema.forEach(function (type) {
        if (type.key !== typeKey) return;
        (type.fields || []).forEach(function (field) {
          if (field.key === spec.key && field.item_label) found = field.item_label;
        });
      });
      return found || spec.label;
    }

    function updateRewrite() {
      var block = state.selected ? findBlock(pb, state.selected) : null;
      var options = block ? fieldOptions(block) : [];
      var previous = rwField.value;
      clear(rwField);
      options.forEach(function (option) {
        var node = el("option", { value: option.path, text: option.label });
        node.selected = option.path === previous;
        rwField.appendChild(node);
      });
      if (block) {
        rwTitle.textContent = "Skriv om i " + blockName(block.type);
        rwHint.textContent = options.length
          ? "Tre förslag till fältet, var och ett med principen bakom."
          : "Det här blocket har ingen text att skriva om. Välj ett annat block.";
      } else {
        rwTitle.textContent = "Skriv om";
        rwHint.textContent = "Välj ett block på sidan, sedan fältet du vill skriva om.";
      }
      rwBox.hidden = !options.length;
    }

    function rewrite() {
      var block = state.selected ? findBlock(pb, state.selected) : null;
      if (!block || !rwField.value) return;
      var path = rwField.value;
      rwError.hidden = true;
      setBusy(rwBtn, true, "Skriver förslag");
      request(urls.ai_rewrite, {
        block_id: block.id,
        field: path,
        type: block.type,
        variant: block.variant,
        fields: activeFields(block),
        goal: checked("goal"),
        tone: checked("tone"),
        service_id: service.value ? Number(service.value) : null
      }).then(function (data) {
        clear(rwList);
        rwSource.textContent = data.source === "ai"
          ? "Förslag från AI till " + (data.label || "fältet").toLowerCase() + "."
          : data.note || "Förslag ur mallarna.";
        rwSource.hidden = false;
        if (!(data.suggestions || []).length) {
          rwList.appendChild(el("li", { className: "pba-sugg pba-sugg--none", text: "Inga förslag till det här fältet. Skriv själv i blocket." }));
        }
        (data.suggestions || []).forEach(function (suggestion) {
          var use = el("button", { type: "button", className: "fl-btn fl-btn--ghost fl-btn--sm", text: "Använd" });
          if (readOnly()) use.disabled = true;
          use.addEventListener("click", function () {
            var current = findBlock(pb, block.id);
            if (!current) return;
            var fields = activeFields(current);
            if (!setPath(fields, data.field || path, suggestion.text)) return;
            use.disabled = true;
            var failed = false;
            settle(
              (function () {
                try {
                  return pb.addVersion(block.id, fields, "ai");
                } catch (err) {
                  return Promise.reject(err);
                }
              })(),
              function (err) {
                failed = true;
                use.disabled = false;
                rwError.textContent = (err && err.message) || "Förslaget gick inte att använda. Försök igen.";
                rwError.hidden = false;
              }
            ).then(function () {
              if (!failed) use.textContent = "Använt";
            });
          });
          rwList.appendChild(el("li", { className: "pba-sugg" }, [
            el("p", { className: "pba-sugg__text", text: suggestion.text }),
            el("p", { className: "pba-sugg__why", text: suggestion.why }),
            el("div", { className: "pba-sugg__row" }, [tag(suggestion.principle_label), use])
          ]));
        });
      }).catch(function (err) {
        rwError.textContent = err.message;
        rwError.hidden = false;
      }).then(function () {
        setBusy(rwBtn, false, "Ge tre förslag");
      });
    }

    rwBtn.addEventListener("click", rewrite);
    rwField.addEventListener("change", function () {
      clear(rwList);
      rwSource.hidden = true;
    });

    pb.on("select", function (payload) {
      var id = idOf(payload);
      if (id !== state.selected) {
        state.selected = id;
        clear(rwList);
        rwSource.hidden = true;
        rwError.hidden = true;
      }
      updateRewrite();
    });
    pb.on("change", function () {
      if (state.selected) updateRewrite();
    });
    pb.on("panel", function (payload) {
      if (!opened(payload, "ai")) return;
      // Panelen öppnad från ett block: skriv om i just det blocket.
      var id = payload && typeof payload === "object" ? payload.blockId : "";
      if (id && id !== state.selected) {
        state.selected = id;
        clear(rwList);
        rwSource.hidden = true;
      }
      order(!!id);
      if (!state.loaded) loadForm();
      else updateRewrite();
    });

    var current = (pb.state() || {}).selectedId;
    if (current) state.selected = current;
    loadForm();
  }

  // ---------------------------------------------------------------------
  // Konverteringskollen
  // ---------------------------------------------------------------------

  function pageName() {
    var input = document.getElementById("pb-name");
    return (input && input.value.trim()) || "Sidan";
  }

  function mountKoll(pb, pane) {
    var urls = pb.urls || {};
    if (!urls.koll) return;
    hideFallback(pane);
    clear(pane);

    var ring = el("div", { className: "pbk-ring", role: "img" }, [el("span", { className: "pbk-ring__text" })]);
    var summary = el("p", { className: "pbk-sum", role: "status" });
    var heading = el("h3", { className: "pba-title", text: pageName() });
    var list = el("ol", { className: "pbk-list" });
    var error = el("p", { className: "pba-error", role: "alert", hidden: true });
    var refresh = el("button", { type: "button", className: "fl-btn fl-btn--ghost fl-btn--sm", text: "Läs sidan igen" });
    pane.appendChild(el("div", { className: "pbk" }, [
      el("div", { className: "pbk-head" }, [
        ring,
        el("div", { className: "pbk-head__text" }, [
          heading,
          summary
        ])
      ]),
      list,
      error,
      el("div", { className: "pbk-foot" }, [
        el("p", { className: "pba-small", text: "Kollen läser det sparade utkastet. Varje punkt har en knapp som gör jobbet." }),
        refresh
      ])
    ]));

    var timer = null;
    var busy = false;

    function badge(data) {
      var text = data.total ? data.score + " av " + data.total : "";
      if (typeof pb.badge === "function") {
        pb.badge("koll", text);
        return;
      }
      var node = document.getElementById("pb-koll-badge");
      if (!node) return;
      node.textContent = text;
      node.hidden = !text;
    }

    function render(data) {
      var total = data.total || 0;
      var score = data.score || 0;
      var share = total ? Math.round((score / total) * 1000) / 10 : 0;
      ring.style.setProperty("--pbk-p", String(share));
      ring.setAttribute("aria-label", score + " av " + total + " klara");
      ring.firstChild.textContent = total ? score + " av " + total : "0";
      summary.textContent = data.summary || "";
      heading.textContent = pageName();
      clear(list);
      (data.items || []).forEach(function (item) {
        list.appendChild(el("li", { className: "pbk-item " + (item.ok ? "is-ok" : "is-no") }, [
          el("span", { className: "pbk-st" }, [
            item.ok ? icon("pb-u-check") : el("span", { "aria-hidden": "true", text: "!" }),
            el("span", { className: "fl-sr", text: item.ok ? "Klart: " : "Att göra: " })
          ]),
          el("div", { className: "pbk-grow" }, [
            el("b", { className: "pbk-title", text: item.title }),
            item.text ? el("p", { className: "pbk-text", text: item.text }) : null,
            el("div", { className: "pba-tags" }, [tag(item.principle_label)])
          ]),
          item.ok ? null : actionButton(pb, item.action, false)
        ]));
      });
      badge(data);
    }

    function load() {
      if (busy) return;
      busy = true;
      refresh.disabled = true;
      var url = urls.koll + (urls.koll.indexOf("?") === -1 ? "?" : "&") + "which=draft";
      request(url).then(function (data) {
        error.hidden = true;
        render(data);
      }).catch(function (err) {
        error.textContent = err.message;
        error.hidden = false;
      }).then(function () {
        busy = false;
        refresh.disabled = false;
      });
    }

    function later() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(load, 300);
    }

    refresh.addEventListener("click", load);
    pb.on("saved", later);
    pb.on("panel", function (payload) {
      if (opened(payload, "koll")) later();
    });
    load();
  }

  // ---------------------------------------------------------------------
  // Start: när FlamingoPB finns
  // ---------------------------------------------------------------------

  function boot() {
    var pb = window.FlamingoPB;
    if (started || !pb || typeof pb.on !== "function" || typeof pb.panelEl !== "function") return;
    started = true;
    var aiPane = pb.panelEl("ai") || document.getElementById("pb-panel-ai");
    var kollPane = pb.panelEl("koll") || document.getElementById("pb-panel-koll");
    if (aiPane) {
      try { mountAI(pb, aiPane); } catch (err) { if (window.console) window.console.error(err); }
    }
    if (kollPane) {
      try { mountKoll(pb, kollPane); } catch (err) { if (window.console) window.console.error(err); }
    }
  }

  document.addEventListener("flamingo-pb:ready", boot);
  window.addEventListener("flamingo-pb:ready", boot);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  // Om redigeraren blev klar innan händelsen lyssnades på: pröva en stund.
  var tries = 0;
  var poll = setInterval(function () {
    tries += 1;
    if (started || tries > 40) {
      clearInterval(poll);
      return;
    }
    boot();
  }, 250);
})();
