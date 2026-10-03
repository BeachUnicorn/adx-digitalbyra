/* ==========================================================================
   Sidbyggarens redigerare (templates/flamingo/app/pages/editor.html,
   apps/flamingo/app_views/pages.py). UX: adx-marketing/sidbyggaren-mockup.html
   skärm 01, 02 och 04.

   Sidan ritas av servern i designen Ren i en ram (iframe med srcdoc), i
   redigeringsläget: varje block har data-pb-block, varje text
   data-pb-field ("title", "points.0", "steps.1.title"), varje bild
   data-pb-media och tomma fält data-pb-empty. Ramen är lika hög som sidan,
   så det är sidan runt den som rullar, och verktygen (markeringen,
   verktygsraden, "Lägg till block", linjen när något dras) ligger i ett
   lager ovanpå ramen i den här sidan.

   Kunden klickar i en text och skriver (contenteditable, bara vanlig text:
   formatering tas bort när något klistras in, Enter ger en ny rad i en
   längre text och lämnar en kort, gränserna kommer från registret). Den
   första ändringen i en version som någon annan skrivit (mallen, AI,
   kunden eller ADX) blir en ny version i den inloggades namn, så att det
   som stod förut finns kvar under Versioner.

   Hela blocklistan sparas med rev (POST spara/) en stund efter den senaste
   ändringen. 409 betyder att någon annan sparat emellan: ingenting skrivs
   över, och redigeraren ber om att sidan laddas om. Efter en ändring av
   formen (variant, nytt block, flytt, bild) ritas sidan om (POST rita/) och
   bara de block som ändrats byts ut i ramen.

   ---------------------------------------------------------------------------
   Det publika gränssnittet, för AI-panelen och konverteringskollen
   (static/js/flamingo-pb-ai.js) och andra delar:

     window.FlamingoPB = {
       state()  -> {pageId, rev, blocks, palette, selectedId}
                   en kopia; ändra bara med funktionerna nedan
       on(event, fn) -> en funktion som tar bort lyssnaren
                   "change"  {reason, blockId, blocks}   blocken ändrades
                             (reason: text, variant, version, add, move,
                             copy, delete, undo, list, media, replace,
                             palette)
                   "select"  {blockId}       null när inget block är valt
                   "saved"   {rev, savedAt, problems}
                   "panel"   {name, open, blockId}
       select(blockId)
       addBlock(type, variant, {afterId, fields, source}) -> Promise<block>
                   ett nytt block med mallens innehåll; med fields läggs en
                   version till ovanpå (source "ai" eller kundens). Utan
                   afterId hamnar det efter det valda blocket, annars före
                   avslutet (formulär och ringremsa)
       addVersion(blockId, fields, source) -> Promise<version>
                   en ny aktiv version; fields slås ihop med den aktiva
                   versionens fält. source "ai" | "customer" | "adx"
                   ("customer" och "adx" blir alltid den inloggades sort).
                   Löftet uppfylls när versionen är sparad och nekas (och
                   ändringen tas tillbaka) om servern säger nej
       setBlocks(blocks, {source}) -> Promise
                   byter hela utkastet (AI bygger en sida), med "Ångra"
       openPanel(name, {blockId}), closePanel()
                   "ai", "koll" eller "media": en panel till höger i datorn,
                   ett ark nedifrån i mobilen
       panelEl(name) -> HTMLElement   #pb-panel-<name>, panelens innehåll
       badge(name, text)              text i knappen "AI" eller
                                      "Konverteringskoll" ("7 av 9"); tom
                                      text tar bort den
       urls: {save, renderBlock, newBlock, publish, settings, pages,
              ai_build, ai_rewrite, koll, media_json, media_upload, media}
                   null för en adress som inte finns än
     }

   Inga AI-typografitecken skrivs här: tecknen byggs med fromCharCode.
   ========================================================================== */
(function () {
  "use strict";

  var root = document.getElementById("pb");
  var configNode = document.getElementById("pb-config");
  if (!root || !configNode) {
    return;
  }

  var config = JSON.parse(configNode.textContent);
  var urls = config.urls || {};
  var me = config.me || {};
  var readOnly = !!config.readOnly || root.hasAttribute("data-read-only");
  var csrf = root.getAttribute("data-csrf") || "";
  var MAX_BLOCKS = config.maxBlocks || 40;
  var MAX_VERSIONS = config.maxVersions || 12;
  var SOURCE_LABELS = { template: "Mallen", ai: "AI", customer: "Kunden", adx: "ADX" };
  /* Mobilens läge (sidan först, verktygen i en rad längst ner) gäller upp
     till 1023 px, samma brytpunkt som i flamingo-pb.css. */
  var MOBILE = window.matchMedia("(max-width: 1023px)");
  /* Rens brytpunkt för två spalter: "Dator" visas aldrig smalare än så. */
  var DESKTOP_MIN = 1024;
  var PHONE_WIDTH = 390;

  var state = {
    pageId: config.pageId,
    rev: config.rev,
    name: config.name,
    palette: config.palette,
    blocks: Array.isArray(config.blocks) ? config.blocks : [],
    selectedId: null,
    published: !!(config.state && config.state.published),
    changes: !!(config.state && config.state.changes),
    live: (config.state && config.state.live) || [],
    problems: config.problems || [],
    panel: null,
    panelBlock: null,
  };

  var TYPES = {};
  (config.schema || []).forEach(function (type) {
    TYPES[type.key] = type;
  });
  var AVAILABLE = config.available || {};

  /* Ordet för en ny rad i en lista ("Lägg till fråga"). */
  var ADD_WORDS = {
    "hero.points": "punkt",
    "area.places": "ort",
    "guarantee.terms": "villkor",
    "steps.steps": "steg",
    "faq.items": "fråga",
    "certificates.items": "certifikat",
    "price.items": "prisexempel",
    "form.questions": "fråga",
  };

  /* Skisserna i variantväljaren (mockupen .wf): ett ord per rad, "row:"
     delar i två spalter med |, "bar:" är en färgad remsa. */
  var WIREFRAMES = {
    hero: { call: "h l b", form: "row:h l|l l b", image: "img h b", text: "h l l" },
    price: { from: "row:h l|big", examples: "h cards", fixed: "card" },
    reviews_google: { cards: "h cards", quote: "quote", line: "stars" },
    reviews_reco: { stor: "h quote", medel: "h stars", liten: "stars", staende: "h card" },
    certificates: { badges: "h chips", icons: "h cards" },
    guarantee: { short: "icon h l", terms: "icon h checks" },
    person: { image: "row:img|h l", noimage: "row:circle|h l" },
    steps: { three: "h nums3", four: "h nums4" },
    before_after: { slider: "h split", pair: "h pair" },
    area: { list: "h chips", map: "row:h chips|map" },
    faq: { three: "h rows3", six: "h rows6" },
    form: { short: "h in in b", questions: "h in in in b", booking: "h date in b" },
    callbar: { call: "bar:b", call_write: "bar:b b2" },
  };

  // -------------------------------------------------------------------------
  // Små hjälpare
  // -------------------------------------------------------------------------

  function $(selector, scope) {
    return (scope || document).querySelector(selector);
  }

  function $all(selector, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
  }

  function append(node, child) {
    if (child === null || child === undefined || child === false) {
      return;
    }
    if (Array.isArray(child)) {
      child.forEach(function (c) {
        append(node, c);
      });
      return;
    }
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }

  function h(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
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
    }
    for (var i = 2; i < arguments.length; i++) {
      append(node, arguments[i]);
    }
    return node;
  }

  var SVG = "http://www.w3.org/2000/svg";

  function icon(id, extra) {
    var svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "pb-ic" + (extra ? " " + extra : ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var use = document.createElementNS(SVG, "use");
    use.setAttribute("href", "#" + id);
    svg.appendChild(use);
    return svg;
  }

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  var ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

  function uid(prefix) {
    var bytes = new Uint8Array(12);
    window.crypto.getRandomValues(bytes);
    var out = prefix;
    for (var i = 0; i < bytes.length; i++) {
      out += ALPHABET[bytes[i] % ALPHABET.length];
    }
    return out;
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function clock(date) {
    return pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  var MONTHS = ["jan", "feb", "mars", "april", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec"];

  function whenText(iso) {
    var date = new Date(iso);
    if (isNaN(date.getTime())) {
      return "";
    }
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    var diff = Math.round((today - day) / 86400000);
    if (diff === 0) {
      return "i dag " + clock(date);
    }
    if (diff === 1) {
      return "i går " + clock(date);
    }
    var text = date.getDate() + " " + MONTHS[date.getMonth()];
    return date.getFullYear() !== now.getFullYear() ? text + " " + date.getFullYear() : text;
  }

  /* AI-typografin blir vanliga tecken redan när den skrivs eller klistras in
     (servern gör samma sak när den sparar). */
  var TYPO = {};
  TYPO[String.fromCharCode(0x2013)] = "-";
  TYPO[String.fromCharCode(0x2014)] = "-";
  TYPO[String.fromCharCode(0x201c)] = '"';
  TYPO[String.fromCharCode(0x201d)] = '"';
  TYPO[String.fromCharCode(0x2018)] = "'";
  TYPO[String.fromCharCode(0x2019)] = "'";
  TYPO[String.fromCharCode(0x2026)] = "...";
  var TYPO_RE = new RegExp("[" + Object.keys(TYPO).join("") + "]", "g");

  function plain(text) {
    return String(text || "")
      .replace(TYPO_RE, function (ch) {
        return TYPO[ch];
      })
      .replace(/ /g, " ");
  }

  function lower(text) {
    return String(text || "").toLowerCase();
  }

  function fold(text) {
    return lower(text)
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "");
  }

  function isMobile() {
    return MOBILE.matches;
  }

  // -------------------------------------------------------------------------
  // Händelser (FlamingoPB.on)
  // -------------------------------------------------------------------------

  var listeners = { change: [], select: [], saved: [], panel: [] };

  function on(event, fn) {
    if (!listeners[event] || typeof fn !== "function") {
      return function () {};
    }
    listeners[event].push(fn);
    return function () {
      listeners[event] = listeners[event].filter(function (f) {
        return f !== fn;
      });
    };
  }

  function emit(event, detail) {
    (listeners[event] || []).slice().forEach(function (fn) {
      try {
        fn(detail);
      } catch (error) {
        if (window.console) {
          window.console.error(error);
        }
      }
    });
  }

  // -------------------------------------------------------------------------
  // Blocken som data
  // -------------------------------------------------------------------------

  function findBlock(id) {
    for (var i = 0; i < state.blocks.length; i++) {
      if (state.blocks[i].id === id) {
        return state.blocks[i];
      }
    }
    return null;
  }

  function indexOf(id) {
    for (var i = 0; i < state.blocks.length; i++) {
      if (state.blocks[i].id === id) {
        return i;
      }
    }
    return -1;
  }

  function typeOf(block) {
    return block ? TYPES[block.type] : null;
  }

  function findField(type, key) {
    var fields = (type && type.fields) || [];
    for (var i = 0; i < fields.length; i++) {
      if (fields[i].key === key) {
        return fields[i];
      }
    }
    return null;
  }

  function variantOf(block) {
    var type = typeOf(block);
    var variants = (type && type.variants) || [];
    for (var i = 0; i < variants.length; i++) {
      if (variants[i].key === block.variant) {
        return variants[i];
      }
    }
    return null;
  }

  function usedBy(spec, variant) {
    return !spec.variants || !spec.variants.length || spec.variants.indexOf(variant) >= 0;
  }

  function activeVersion(block) {
    var versions = (block && block.versions) || [];
    for (var i = 0; i < versions.length; i++) {
      if (versions[i].id === block.active) {
        return versions[i];
      }
    }
    return versions[versions.length - 1] || null;
  }

  function activeFields(block) {
    var version = activeVersion(block);
    return (version && version.fields) || {};
  }

  function isIndex(part) {
    return /^\d+$/.test(part);
  }

  function getPath(obj, path) {
    return path.split(".").reduce(function (o, part) {
      return o === null || o === undefined ? undefined : o[isIndex(part) ? +part : part];
    }, obj);
  }

  function setPath(obj, path, value) {
    var parts = path.split(".");
    var node = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      var key = isIndex(parts[i]) ? +parts[i] : parts[i];
      if (node[key] === null || node[key] === undefined || typeof node[key] !== "object") {
        node[key] = isIndex(parts[i + 1]) ? [] : {};
      }
      node = node[key];
    }
    var last = parts[parts.length - 1];
    node[isIndex(last) ? +last : last] = value;
  }

  /* En rad i en lista heter i singular ("Punkt" i "Punkter"): registrets
     item_label, annars listans etikett. */
  function itemLabel(spec) {
    return (spec && (spec.item_label || spec.label)) || "";
  }

  /* Etiketten för en text med plats i en lista: "Punkt 2", "Fråga 1",
     "Fråga 1, svar", "Steg 3, text". Utan plats: fältets etikett. */
  function fieldLabel(spec) {
    if (!spec) {
      return "";
    }
    if (spec.index === undefined) {
      return spec.label;
    }
    var base = itemLabel(spec.top) + " " + (spec.index + 1);
    if (spec.sub && lower(spec.sub.label) !== lower(itemLabel(spec.top))) {
      return base + ", " + lower(spec.sub.label);
    }
    return base;
  }

  /* Fältets regler ur registret: sort, högsta längd, etikett och om det är
     en längre text med radbrytningar. */
  function fieldSpec(typeKey, path) {
    var type = TYPES[typeKey];
    if (!type || !path) {
      return null;
    }
    var parts = path.split(".");
    var top = findField(type, parts[0]);
    if (!top) {
      return null;
    }
    if (parts.length === 1) {
      return { top: top, kind: top.kind, max: top.max_length, label: top.label, multi: top.kind === "textarea" };
    }
    if (top.kind === "lines" && parts.length === 2) {
      return { top: top, kind: "text", max: top.max_length, label: itemLabel(top), line: true, index: +parts[1], multi: false };
    }
    if (top.kind === "items" && parts.length === 3) {
      var sub = null;
      (top.items || []).forEach(function (f) {
        if (f.key === parts[2]) {
          sub = f;
        }
      });
      if (!sub) {
        return null;
      }
      return { top: top, sub: sub, kind: sub.kind, max: sub.max_length, label: sub.label, index: +parts[1], multi: sub.kind === "textarea" };
    }
    return null;
  }

  function mine(version) {
    return !!version && version.source === me.source && version.by === me.id;
  }

  function trimVersions(block) {
    while (block.versions.length > MAX_VERSIONS) {
      var drop = -1;
      for (var i = 0; i < block.versions.length; i++) {
        if (block.versions[i].id !== block.active) {
          drop = i;
          break;
        }
      }
      if (drop < 0) {
        break;
      }
      block.versions.splice(drop, 1);
    }
  }

  /* Versionen den inloggade skriver i: den aktiva om den är hens egen,
     annars en ny kopia av den (det som stod förut finns kvar). */
  function ownVersion(block) {
    var current = activeVersion(block);
    if (mine(current)) {
      return current;
    }
    var version = {
      id: uid("v_"),
      fields: clone((current && current.fields) || {}),
      source: me.source,
      by: me.id,
      at: nowIso(),
    };
    block.versions.push(version);
    block.active = version.id;
    trimVersions(block);
    return version;
  }

  function newVersion(block, fields, source) {
    var version = {
      id: uid("v_"),
      fields: fields,
      source: source === "ai" ? "ai" : me.source,
      by: me.id,
      at: nowIso(),
    };
    block.versions.push(version);
    block.active = version.id;
    trimVersions(block);
    return version;
  }

  function blockTitle(block) {
    var fields = activeFields(block);
    var text = fields.title || fields.name || fields.text || fields.lead || "";
    if (!text) {
      var type = typeOf(block);
      (type ? type.fields : []).some(function (spec) {
        var value = fields[spec.key];
        if (spec.kind === "lines" && value && value.length) {
          text = value[0];
        } else if (spec.kind === "items" && value && value.length) {
          var first = value[0] || {};
          text = first.title || first.q || first.label || first.name || "";
        }
        return !!text;
      });
    }
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function canAdd(typeKey) {
    var type = TYPES[typeKey];
    if (!type || readOnly) {
      return { ok: false, reason: "" };
    }
    var base = AVAILABLE[typeKey] || { ok: true, reason: "" };
    if (!base.ok) {
      return base;
    }
    if (state.blocks.length >= MAX_BLOCKS) {
      return { ok: false, reason: "Högst " + MAX_BLOCKS + " block på en sida." };
    }
    if (type.single && state.blocks.some(function (b) {
      return b.type === typeKey;
    })) {
      return { ok: false, reason: "Finns redan på sidan." };
    }
    return { ok: true, reason: "" };
  }

  /* Platsen efter blocket på index. Toppen med formulär och ett
     formulärblock direkt efter står bredvid varandra på en bred skärm
     (pagebuilder/__init__.py): ett nytt block hamnar efter paret, aldrig
     mellan dem. */
  function gapAfter(index) {
    var block = state.blocks[index];
    var next = state.blocks[index + 1];
    if (block && block.type === "hero" && block.variant === "form" && next && next.type === "form") {
      return index + 2;
    }
    return index + 1;
  }

  /* Var ett nytt block hamnar utan angiven plats: efter det valda, annars
     före avslutet (formulär och ringremsa sist på sidan). */
  function defaultGap(typeKey) {
    if (state.selectedId && indexOf(state.selectedId) >= 0) {
      return gapAfter(indexOf(state.selectedId));
    }
    var type = TYPES[typeKey];
    if (type && type.group !== "end") {
      for (var i = 0; i < state.blocks.length; i++) {
        var t = TYPES[state.blocks[i].type];
        if (t && t.group === "end") {
          return i;
        }
      }
    }
    return state.blocks.length;
  }

  // -------------------------------------------------------------------------
  // Nätet
  // -------------------------------------------------------------------------

  function post(url, data) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRFToken": csrf,
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify(data || {}),
    }).then(
      function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (body) {
            return { status: response.status, ok: response.ok, data: body || {} };
          });
      },
      function () {
        return { status: 0, ok: false, data: { error: "Ingen kontakt med servern." } };
      }
    );
  }

  // -------------------------------------------------------------------------
  // Toppraden: sparat, läget, publicera
  // -------------------------------------------------------------------------

  var savedEl = $("#pb-saved");
  var stateEl = $("#pb-state");
  var publishBtn = $("#pb-publish");
  var problemsBtn = $("#pb-problems-btn");
  var problemsCount = $("#pb-problems-count");
  var publishing = false;
  var announcer = h("p", { class: "fl-sr", "aria-live": "polite" });
  root.appendChild(announcer);

  function announce(text) {
    announcer.textContent = "";
    window.setTimeout(function () {
      announcer.textContent = text;
    }, 30);
  }

  function setSaved(kind, text) {
    if (!savedEl) {
      return;
    }
    savedEl.dataset.state = kind;
    savedEl.textContent = text;
  }

  function stateText() {
    if (!state.published) {
      return { label: "Utkast", badge: "draft" };
    }
    if (state.changes) {
      return { label: "Ändringar ej publicerade", badge: "needs_customer" };
    }
    if (state.live.length) {
      return { label: "Live", badge: "live" };
    }
    /* Publicerad men ingen kampanj som visar sidan är live: inte grön, så
       att den inte förväxlas med Live (samma text i sidlistan). */
    return { label: "Publicerad, ingen kampanj live", badge: "paused" };
  }

  function updatePublish() {
    var info = stateText();
    if (stateEl) {
      stateEl.textContent = info.label;
      stateEl.className = "fl-badge fl-badge--" + info.badge;
    }
    if (!publishBtn) {
      return;
    }
    var label;
    var enabled;
    if (!state.published) {
      label = "Publicera";
      enabled = state.blocks.length > 0;
    } else {
      label = "Publicera ändringarna";
      enabled = state.changes;
    }
    if (publishing) {
      label = "Publicerar";
      enabled = false;
    }
    publishBtn.innerHTML = "";
    publishBtn.appendChild(h("span", { class: "pb-long", text: label }));
    publishBtn.appendChild(h("span", { class: "pb-short", text: publishing ? "Publicerar" : "Publicera" }));
    publishBtn.disabled = !enabled || stale;
    publishBtn.title = !enabled && state.published && !publishing ? "Allt är publicerat." : "";
  }

  // -------------------------------------------------------------------------
  // Spara (hela utkastet med rev)
  // -------------------------------------------------------------------------

  var saveTimer = null;
  var saveInflight = null;
  var dirty = false;
  var stale = false;
  var retryDelay = 4000;

  function markDirty() {
    if (readOnly || stale) {
      return;
    }
    dirty = true;
    setSaved("dirty", "Ändrat");
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(flushQuietly, 900);
  }

  function flushQuietly() {
    flush().catch(function () {
      /* svaret visas i "Sparat" och i meddelandet */
    });
  }

  function showStale() {
    stale = true;
    dirty = false;
    window.clearTimeout(saveTimer);
    setSaved("error", "Inte sparat");
    var box = $("#pb-stale");
    if (box) {
      box.hidden = false;
      var button = $("#pb-stale-reload");
      if (button) {
        button.focus();
      }
    }
    updatePublish();
  }

  /* Spara nu. Löftet uppfylls med serverns svar (eller undefined när inget
     behövde sparas) och nekas med {status, data}. */
  function flush() {
    window.clearTimeout(saveTimer);
    if (stale) {
      return Promise.reject({ status: 409, data: { error: "Sidan har ändrats på ett annat ställe." } });
    }
    if (saveInflight) {
      return saveInflight.then(
        function () {
          return dirty ? flush() : undefined;
        },
        function () {
          return dirty ? flush() : undefined;
        }
      );
    }
    if (!dirty) {
      return Promise.resolve();
    }
    dirty = false;
    setSaved("saving", "Sparar");
    var sent = JSON.parse(JSON.stringify(state.blocks));
    saveInflight = post(urls.save, { rev: state.rev, blocks: sent }).then(function (res) {
      saveInflight = null;
      if (res.ok) {
        retryDelay = 4000;
        state.rev = res.data.rev;
        state.published = !!(res.data.state && res.data.state.published);
        state.changes = dirty || !!(res.data.state && res.data.state.changes);
        state.live = (res.data.state && res.data.state.live) || state.live;
        setSaved("saved", "Sparat " + (res.data.saved_text || clock(new Date())));
        setProblems(res.data.problems || []);
        updatePublish();
        emit("saved", { rev: state.rev, savedAt: res.data.saved_at, problems: state.problems });
        if (dirty) {
          markDirty();
        }
        return res.data;
      }
      if (res.status === 409) {
        showStale();
        throw res;
      }
      if (res.status === 0 || res.status >= 500) {
        dirty = true;
        setSaved("error", "Inte sparat, försöker igen");
        window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(flushQuietly, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 60000);
        throw res;
      }
      setSaved("error", "Inte sparat");
      toast("Ändringen gick inte att spara. " + (res.data.error || ""));
      throw res;
    });
    return saveInflight;
  }

  window.addEventListener("beforeunload", function (event) {
    if ((dirty || saveInflight) && !stale) {
      flushQuietly();
      event.preventDefault();
      event.returnValue = "";
    }
  });

  var staleReload = $("#pb-stale-reload");
  if (staleReload) {
    staleReload.addEventListener("click", function () {
      window.location.reload();
    });
  }

  // -------------------------------------------------------------------------
  // Ramen med sidan
  // -------------------------------------------------------------------------

  var frame = $("#pb-frame");
  var frameWrap = $("#pb-frame-wrap");
  var stage = $("#pb-stage");
  var overlay = $("#pb-overlay");
  var canvas = $("#pb-canvas");
  var body = $("#pb-body");
  var topbar = $(".pb-top", root);
  var fdoc = null;
  var fwin = null;
  var scale = 1;
  var device = "desktop";
  /* Den HTML servern senast ritade per block (före redigerarens ändringar
     i DOM:en), för att se vilka block som behöver bytas. */
  var rendered = {};
  var renderedChrome = {};
  var deferred = {};
  var forced = {};
  var hoverId = null;

  try {
    device = window.localStorage.getItem("pb-device") === "mobile" ? "mobile" : "desktop";
  } catch (error) {
    device = "desktop";
  }

  function blockNodes() {
    if (!fdoc) {
      return [];
    }
    return $all("main.rn-main > [data-pb-block]", fdoc);
  }

  function blockNode(id) {
    if (!fdoc || !id) {
      return null;
    }
    return fdoc.querySelector('main.rn-main > [data-pb-block="' + id + '"]');
  }

  function topbarHeight() {
    return topbar ? topbar.getBoundingClientRect().height : 0;
  }

  function supportsPlaintext() {
    var probe = document.createElement("div");
    try {
      probe.contentEditable = "plaintext-only";
    } catch (error) {
      return false;
    }
    return probe.contentEditable === "plaintext-only";
  }

  var PLAINTEXT = supportsPlaintext();

  /* Ett block som just ritats: texterna blir redigerbara, tomma fält och
     dolda block märks för redigerarens CSS, knappar och länkar gör inget. */
  function prepare(node) {
    var typeKey = node.getAttribute("data-pb-type");
    var type = TYPES[typeKey];
    if (node.hasAttribute("data-pb-empty")) {
      node.removeAttribute("data-pb-empty");
      node.setAttribute("data-pb-hidden", "");
    }
    $all("[data-pb-empty]", node).forEach(function (el) {
      el.removeAttribute("data-pb-empty");
      el.setAttribute("data-pb-blank", "");
    });
    $all("details", node).forEach(function (details) {
      details.open = true;
    });
    $all("button", node).forEach(function (button) {
      if (button.querySelector("[data-pb-field]")) {
        var span = fdoc.createElement("span");
        span.className = button.className;
        span.setAttribute("data-pb-was", "button");
        while (button.firstChild) {
          span.appendChild(button.firstChild);
        }
        button.parentNode.replaceChild(span, button);
      } else {
        button.tabIndex = -1;
        button.type = "button";
      }
    });
    $all("input, textarea, select", node).forEach(function (input) {
      input.tabIndex = -1;
      input.readOnly = true;
    });
    $all("a", node).forEach(function (link) {
      link.tabIndex = -1;
    });
    var editable = 0;
    $all("[data-pb-field]", node).forEach(function (el) {
      if (el.hasAttribute("data-pb-media")) {
        return;
      }
      var path = el.getAttribute("data-pb-field");
      var spec = fieldSpec(typeKey, path);
      if (!spec || spec.kind === "choice" || spec.kind === "key" || spec.kind === "media") {
        return;
      }
      if (spec.kind === "lines" || spec.kind === "items") {
        /* En tom lista: platshållaren öppnar listan. */
        el.setAttribute("data-pb-list", "");
        return;
      }
      if (readOnly) {
        return;
      }
      if (PLAINTEXT) {
        el.contentEditable = "plaintext-only";
      } else {
        el.contentEditable = "true";
      }
      el.setAttribute("role", "textbox");
      el.setAttribute("spellcheck", "true");
      el.setAttribute("aria-label", fieldLabel(spec) + (type ? " i " + type.name : ""));
      if (spec.multi) {
        el.setAttribute("aria-multiline", "true");
        el.setAttribute("data-pb-multi", "");
      }
      if (!el.hasAttribute("data-pb-placeholder")) {
        el.setAttribute("data-pb-placeholder", spec.line ? "Ny rad" : spec.label);
      }
      editable += 1;
    });
    if (!editable) {
      node.tabIndex = 0;
      node.setAttribute("aria-label", (type ? type.name : "Block") + ". Enter för blockets verktyg.");
    } else {
      node.tabIndex = -1;
    }
    if (node.getAttribute("data-pb-block") === state.selectedId) {
      node.classList.add("pb-is-sel");
    }
    markProblemFields(node);
  }

  function chromeHtml(doc, selector) {
    var el = doc.querySelector(selector);
    return el ? el.outerHTML : "";
  }

  function onFrameReady() {
    if (fdoc || !frame) {
      return;
    }
    var doc = frame.contentDocument;
    if (!doc || !doc.querySelector("main.rn-main")) {
      return;
    }
    fdoc = doc;
    fwin = frame.contentWindow;
    blockNodes().forEach(function (node) {
      rendered[node.getAttribute("data-pb-block")] = node.outerHTML;
      prepare(node);
    });
    renderedChrome.top = chromeHtml(fdoc, ".rn-top");
    renderedChrome.foot = chromeHtml(fdoc, ".rn-foot");
    bindFrame();
    if (window.ResizeObserver) {
      new window.ResizeObserver(function () {
        scheduleLayout();
      }).observe(fdoc.body);
    }
    if (fdoc.fonts && fdoc.fonts.ready) {
      fdoc.fonts.ready.then(scheduleLayout);
    }
    layout();
    root.classList.add("is-ready");
  }

  var layoutQueued = false;

  function scheduleLayout() {
    if (layoutQueued) {
      return;
    }
    layoutQueued = true;
    window.requestAnimationFrame(function () {
      layoutQueued = false;
      layout();
    });
  }

  /* Ramens bredd och höjd. Dator: hela dukens bredd, men aldrig smalare än
     Rens brytpunkt för två spalter (då skalas ramen ner). Mobil: en
     telefons bredd. I en riktig mobil: hela skärmen. */
  function layout() {
    root.style.setProperty("--pb-top-h", Math.round(topbarHeight()) + "px");
    root.setAttribute("data-device", isMobile() ? "phone" : device);
    $all("[data-pb-device]", root).forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.getAttribute("data-pb-device") === device));
    });
    if (!fdoc || !frame) {
      return;
    }
    var styles = window.getComputedStyle(canvas);
    var available = canvas.clientWidth - parseFloat(styles.paddingLeft) - parseFloat(styles.paddingRight);
    var border = frameWrap ? frameWrap.offsetWidth - frameWrap.clientWidth : 0;
    available = Math.max(200, available - border);
    var width;
    var nextScale = 1;
    if (isMobile()) {
      width = available;
    } else if (device === "mobile") {
      width = Math.min(PHONE_WIDTH, available);
    } else if (available >= DESKTOP_MIN) {
      width = available;
    } else {
      width = DESKTOP_MIN;
      nextScale = available / DESKTOP_MIN;
    }
    width = Math.floor(width);
    frame.style.width = width + "px";
    var height = Math.ceil(fdoc.body.getBoundingClientRect().height);
    frame.style.height = height + "px";
    frame.style.transform = nextScale === 1 ? "" : "scale(" + nextScale + ")";
    scale = nextScale;
    frameWrap.style.width = Math.round(width * nextScale) + border + "px";
    frameWrap.style.height = Math.round(height * nextScale) + border + "px";
    drawOverlay();
  }

  window.addEventListener("resize", scheduleLayout);
  if (MOBILE.addEventListener) {
    MOBILE.addEventListener("change", function () {
      closeSurface(false);
      scheduleLayout();
      syncPanelMode();
    });
  }

  $all("[data-pb-device]", root).forEach(function (button) {
    button.addEventListener("click", function () {
      device = button.getAttribute("data-pb-device") === "mobile" ? "mobile" : "desktop";
      try {
        window.localStorage.setItem("pb-device", device);
      } catch (error) {
        /* bara en bekvämlighet */
      }
      layout();
    });
  });

  // -------------------------------------------------------------------------
  // Rita om (POST rita/) och byt bara de block som ändrats
  // -------------------------------------------------------------------------

  var renderSeq = 0;
  var renderTimer = null;
  var renderWaiters = [];
  var focusAfter = null;

  function requestRender(delay) {
    window.clearTimeout(renderTimer);
    renderTimer = window.setTimeout(doRender, delay || 0);
  }

  function afterRender(fn) {
    renderWaiters.push(fn);
  }

  function doRender() {
    var seq = ++renderSeq;
    root.classList.add("is-rendering");
    post(urls.renderBlock, { blocks: state.blocks, palette: state.palette }).then(function (res) {
      if (seq !== renderSeq) {
        return;
      }
      root.classList.remove("is-rendering");
      if (!res.ok) {
        toast("Sidan gick inte att rita om. " + (res.data.error || ""));
        renderWaiters = [];
        return;
      }
      applyDocument(res.data.html);
      var waiters = renderWaiters;
      renderWaiters = [];
      waiters.forEach(function (fn) {
        try {
          fn();
        } catch (error) {
          if (window.console) {
            window.console.error(error);
          }
        }
      });
    });
  }

  function replaceChrome(doc, selector, key) {
    var html = chromeHtml(doc, selector);
    if (html === renderedChrome[key]) {
      return;
    }
    var current = fdoc.querySelector(selector);
    var next = doc.querySelector(selector);
    if (current && next) {
      current.parentNode.replaceChild(fdoc.importNode(next, true), current);
      renderedChrome[key] = html;
      var fresh = fdoc.querySelector(selector);
      $all("a, button", fresh).forEach(function (el) {
        el.tabIndex = -1;
      });
    }
  }

  function applyPaletteStyle(css) {
    if (!fdoc || !css) {
      return;
    }
    $all("head style", fdoc).some(function (style) {
      if (style.textContent.indexOf("--rn-primary") >= 0) {
        style.textContent = css;
        return true;
      }
      return false;
    });
  }

  function applyDocument(html) {
    if (!fdoc) {
      return;
    }
    var doc = new DOMParser().parseFromString(html, "text/html");
    var nextMain = doc.querySelector("main.rn-main");
    var main = fdoc.querySelector("main.rn-main");
    if (!nextMain || !main) {
      return;
    }
    replaceChrome(doc, ".rn-top", "top");
    replaceChrome(doc, ".rn-foot", "foot");
    $all("head style", doc).some(function (style) {
      if (style.textContent.indexOf("--rn-primary") >= 0) {
        applyPaletteStyle(style.textContent);
        return true;
      }
      return false;
    });
    fdoc.body.className = doc.body.className;
    var existing = {};
    blockNodes().forEach(function (node) {
      existing[node.getAttribute("data-pb-block")] = node;
    });
    var active = fdoc.hasFocus() ? fdoc.activeElement : null;
    var order = [];
    $all(":scope > [data-pb-block]", nextMain).forEach(function (section) {
      var id = section.getAttribute("data-pb-block");
      var html = section.outerHTML;
      var current = existing[id];
      var node;
      if (current && rendered[id] === html) {
        node = current;
        delete deferred[id];
      } else if (current && active && current.contains(active) && active !== fdoc.body && !forced[id]) {
        /* Kunden skriver i blocket: byt det när texten lämnas. */
        node = current;
        deferred[id] = section;
      } else {
        node = fdoc.importNode(section, true);
        rendered[id] = html;
        delete deferred[id];
        prepare(node);
      }
      order.push(node);
    });
    var keep = new Set(order);
    Object.keys(existing).forEach(function (id) {
      if (!keep.has(existing[id])) {
        existing[id].remove();
        delete rendered[id];
      }
    });
    order.forEach(function (node, i) {
      var at = $all(":scope > [data-pb-block]", main)[i];
      if (at !== node) {
        main.insertBefore(node, at || null);
      }
    });
    forced = {};
    if (hoverId && !blockNode(hoverId)) {
      hoverId = null;
    }
    $all(".pb-is-sel", fdoc).forEach(function (n) {
      n.classList.remove("pb-is-sel");
    });
    var selected = blockNode(state.selectedId);
    if (selected) {
      selected.classList.add("pb-is-sel");
    } else if (state.selectedId && !findBlock(state.selectedId)) {
      select(null);
    }
    removing = false;
    layout();
    if (focusAfter) {
      var target = focusAfter;
      focusAfter = null;
      focusField(target.blockId, target.path);
    }
  }

  function applyDeferred(id) {
    var section = deferred[id];
    var current = blockNode(id);
    if (!section || !current) {
      return;
    }
    delete deferred[id];
    var node = fdoc.importNode(section, true);
    rendered[id] = section.outerHTML;
    prepare(node);
    current.parentNode.replaceChild(node, current);
    if (id === state.selectedId) {
      node.classList.add("pb-is-sel");
    }
    layout();
  }

  function placeCaretAtEnd(el) {
    var range = fdoc.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    var selection = fwin.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function focusField(blockId, path, selectAll) {
    var node = blockNode(blockId);
    if (!node) {
      return false;
    }
    var el = path ? node.querySelector('[data-pb-field="' + path + '"][contenteditable]') : null;
    if (!el) {
      el = node.querySelector("[contenteditable]");
    }
    if (!el) {
      return false;
    }
    el.focus({ preventScroll: true });
    if (selectAll) {
      var range = fdoc.createRange();
      range.selectNodeContents(el);
      var selection = fwin.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    } else {
      placeCaretAtEnd(el);
    }
    return true;
  }

  // -------------------------------------------------------------------------
  // Ändringar
  // -------------------------------------------------------------------------

  function changed(reason, opts) {
    opts = opts || {};
    state.changes = true;
    markDirty();
    if (opts.render) {
      requestRender(opts.delay || 0);
    }
    updateLibrary();
    updatePublish();
    updateMbar();
    if (opts.render || reason !== "text") {
      drawOverlay();
    }
    emit("change", { reason: reason, blockId: opts.blockId || null, blocks: state.blocks });
  }

  // -------------------------------------------------------------------------
  // Text på sidan (contenteditable, bara vanlig text)
  // -------------------------------------------------------------------------

  var BLOCK_TAGS = /^(DIV|P|LI|H[1-6])$/;

  function readText(el, multi) {
    var out = "";
    (function walk(node) {
      Array.prototype.forEach.call(node.childNodes, function (child) {
        if (child.nodeType === 3) {
          out += child.data;
        } else if (child.nodeName === "BR") {
          out += "\n";
        } else if (child.nodeType === 1) {
          if (BLOCK_TAGS.test(child.nodeName) && out && out.charAt(out.length - 1) !== "\n") {
            out += "\n";
          }
          walk(child);
        }
      });
    })(el);
    out = plain(out);
    if (!multi) {
      return out.replace(/\s+/g, " ");
    }
    return out.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n");
  }

  function fieldContext(target) {
    if (!target || !target.closest) {
      return null;
    }
    var el = target.closest("[data-pb-field][contenteditable]");
    if (!el || el.getAttribute("contenteditable") === "false") {
      return null;
    }
    var blockEl = el.closest("[data-pb-block]");
    if (!blockEl) {
      return null;
    }
    var block = findBlock(blockEl.getAttribute("data-pb-block"));
    if (!block) {
      return null;
    }
    var path = el.getAttribute("data-pb-field");
    var spec = fieldSpec(block.type, path);
    if (!spec) {
      return null;
    }
    return { el: el, block: block, path: path, spec: spec, blockEl: blockEl };
  }

  function commitText(ctx, finalize) {
    var value = readText(ctx.el, ctx.spec.multi);
    if (finalize) {
      value = ctx.spec.multi ? value.replace(/^\s+|\s+$/g, "") : value.trim();
    }
    var current = getPath(activeFields(ctx.block), ctx.path);
    if ((current === undefined ? "" : String(current)) === value) {
      return false;
    }
    var version = ownVersion(ctx.block);
    setPath(version.fields, ctx.path, value);
    version.at = nowIso();
    if (!value && ctx.el.innerHTML !== "") {
      ctx.el.innerHTML = "";
    }
    changed("text", { blockId: ctx.block.id });
    return true;
  }

  function selectionLength() {
    var selection = fwin.getSelection();
    return selection && !selection.isCollapsed ? selection.toString().length : 0;
  }

  function insertPlain(text, multi) {
    if (!multi) {
      if (!fdoc.execCommand("insertText", false, text)) {
        insertNodeAtCaret(fdoc.createTextNode(text));
      }
      return;
    }
    var lines = text.split("\n");
    lines.forEach(function (line, i) {
      if (line && !fdoc.execCommand("insertText", false, line)) {
        insertNodeAtCaret(fdoc.createTextNode(line));
      }
      if (i < lines.length - 1) {
        insertBreak();
      }
    });
  }

  function insertNodeAtCaret(node) {
    var selection = fwin.getSelection();
    if (!selection || !selection.rangeCount) {
      return;
    }
    var range = selection.getRangeAt(0);
    range.deleteContents();
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    var target = node.parentNode && node.parentNode.closest ? node.parentNode.closest("[data-pb-field]") : null;
    if (target) {
      target.dispatchEvent(new fwin.Event("input", { bubbles: true }));
    }
  }

  function insertBreak() {
    if (!fdoc.execCommand("insertLineBreak")) {
      insertNodeAtCaret(fdoc.createElement("br"));
    }
  }

  var counter = null;

  function showCounter(ctx) {
    if (!ctx || !ctx.spec.max) {
      hideCounter();
      return;
    }
    var length = readText(ctx.el, ctx.spec.multi).length;
    if (length < ctx.spec.max * 0.8) {
      hideCounter();
      return;
    }
    if (!counter) {
      counter = h("div", { class: "pb-count", "aria-hidden": "true" });
      overlay.appendChild(counter);
    }
    var rect = rectOf(ctx.el);
    counter.hidden = false;
    counter.textContent = length + " av " + ctx.spec.max;
    counter.classList.toggle("is-full", length >= ctx.spec.max);
    counter.style.top = Math.round(rect.bottom + 6) + "px";
    counter.style.left = Math.round(rect.left + rect.width) + "px";
  }

  function hideCounter() {
    if (counter) {
      counter.hidden = true;
    }
  }

  /* Enter i en punkt: en ny punkt efter. Backspace i en tom punkt: bort. */
  var removing = false;
  function addLineAfter(ctx) {
    var key = ctx.path.split(".")[0];
    var index = ctx.spec.index;
    var max = ctx.spec.top.max_items;
    var version = ownVersion(ctx.block);
    var list = Array.isArray(version.fields[key]) ? version.fields[key] : (version.fields[key] = []);
    if (max && list.length >= max) {
      toast("Högst " + max + " rader här.");
      return;
    }
    list.splice(index + 1, 0, "");
    focusAfter = { blockId: ctx.block.id, path: key + "." + (index + 1) };
    forced[ctx.block.id] = true;
    changed("list", { blockId: ctx.block.id, render: true });
  }

  function removeLine(ctx) {
    var key = ctx.path.split(".")[0];
    var index = ctx.spec.index;
    var version = ownVersion(ctx.block);
    var list = Array.isArray(version.fields[key]) ? version.fields[key] : [];
    if (index < 0 || index >= list.length) {
      return;
    }
    list.splice(index, 1);
    if (index > 0) {
      focusAfter = { blockId: ctx.block.id, path: key + "." + (index - 1) };
    }
    forced[ctx.block.id] = true;
    removing = true;
    changed("list", { blockId: ctx.block.id, render: true });
  }

  function bindFrame() {
    fdoc.addEventListener(
      "click",
      function (event) {
        var target = event.target;
        if (target.closest("a") || target.closest("label") || target.closest("summary")) {
          event.preventDefault();
        }
        var blockEl = target.closest("[data-pb-block]");
        if (!blockEl) {
          if (!target.closest("[contenteditable]")) {
            select(null);
          }
          return;
        }
        var id = blockEl.getAttribute("data-pb-block");
        if (state.selectedId !== id) {
          select(id);
        }
        if (readOnly) {
          return;
        }
        var media = target.closest("[data-pb-media]");
        if (!media && !target.closest("[contenteditable]") && target.parentElement && target.parentElement.querySelector(":scope > [data-pb-media]")) {
          /* En etikett ovanpå bilderna ("Före", "Efter" i reglaget): bilden
             som syns där etiketten sitter. */
          media = mediaAt(event.clientX, event.clientY);
          media = media && blockEl.contains(media) ? media : null;
        }
        if (media) {
          openMedia(id, media.getAttribute("data-pb-field"));
          return;
        }
        var list = target.closest("[data-pb-list]");
        if (list) {
          openList(id, list.getAttribute("data-pb-field").split(".")[0], null);
        }
      },
      true
    );
    fdoc.addEventListener("submit", function (event) {
      event.preventDefault();
    }, true);
    fdoc.addEventListener("dragstart", function (event) {
      event.preventDefault();
    }, true);
    fdoc.addEventListener("drop", function (event) {
      event.preventDefault();
    }, true);
    fdoc.addEventListener("mouseover", function (event) {
      var blockEl = event.target.closest ? event.target.closest("[data-pb-block]") : null;
      var id = blockEl ? blockEl.getAttribute("data-pb-block") : null;
      if (id !== hoverId) {
        hoverId = id;
        drawHover();
      }
    });
    fdoc.documentElement.addEventListener("mouseleave", function () {
      hoverId = null;
      drawHover();
      leaveInsertSoon();
    });
    fdoc.addEventListener("mousemove", function (event) {
      nearInsert(event.clientY * scale + frameOrigin().y);
    });
    fdoc.addEventListener("focusin", function (event) {
      var ctx = fieldContext(event.target);
      if (ctx) {
        if (state.selectedId !== ctx.block.id) {
          select(ctx.block.id);
        }
        showCounter(ctx);
        return;
      }
      var blockEl = event.target.closest ? event.target.closest("[data-pb-block]") : null;
      if (blockEl && blockEl === event.target && state.selectedId !== blockEl.getAttribute("data-pb-block")) {
        select(blockEl.getAttribute("data-pb-block"));
      }
    });
    fdoc.addEventListener("focusout", function (event) {
      var ctx = fieldContext(event.target);
      if (ctx && (!ctx.el.isConnected || removing)) {
        removing = false;
        return;
      }
      if (ctx) {
        commitText(ctx, true);
        hideCounter();
        if (ctx.spec.line && !readText(ctx.el, false).trim()) {
          /* En tom punkt som lämnas tas bort. */
          var related = event.relatedTarget;
          var stays = related && related.closest && related.closest('[data-pb-field^="' + ctx.path.split(".")[0] + '."]');
          if (!stays) {
            removeLine(ctx);
          }
        }
      }
      var blockEl = event.target.closest ? event.target.closest("[data-pb-block]") : null;
      if (blockEl) {
        var id = blockEl.getAttribute("data-pb-block");
        window.setTimeout(function () {
          var node = blockNode(id);
          if (deferred[id] && (!node || !fdoc.hasFocus() || !node.contains(fdoc.activeElement))) {
            applyDeferred(id);
          }
        }, 0);
      }
    });
    fdoc.addEventListener("input", function (event) {
      var ctx = fieldContext(event.target);
      if (!ctx) {
        return;
      }
      commitText(ctx, false);
      if (ctx.el.hasAttribute("data-pb-blank") && readText(ctx.el, ctx.spec.multi)) {
        ctx.el.removeAttribute("data-pb-blank");
      }
      showCounter(ctx);
    });
    fdoc.addEventListener("beforeinput", function (event) {
      var ctx = fieldContext(event.target);
      if (!ctx) {
        return;
      }
      var kind = event.inputType || "";
      if (/^format/.test(kind) || kind === "insertFromDrop" || kind === "insertLink") {
        event.preventDefault();
        return;
      }
      if ((kind === "insertParagraph" || kind === "insertLineBreak") && !ctx.spec.multi) {
        event.preventDefault();
        return;
      }
      if (kind.indexOf("insert") === 0 && kind !== "insertFromPaste" && ctx.spec.max) {
        var length = readText(ctx.el, ctx.spec.multi).length;
        var adding = event.data ? event.data.length : 1;
        if (length - selectionLength() + adding > ctx.spec.max) {
          event.preventDefault();
          showCounter(ctx);
          if (counter) {
            counter.classList.remove("is-flash");
            void counter.offsetWidth;
            counter.classList.add("is-flash");
          }
        }
      }
    });
    fdoc.addEventListener("paste", function (event) {
      var ctx = fieldContext(event.target);
      if (!ctx) {
        return;
      }
      event.preventDefault();
      var data = event.clipboardData || fwin.clipboardData;
      var text = plain(data ? data.getData("text/plain") || data.getData("Text") || "" : "").replace(/\r\n?/g, "\n");
      if (!ctx.spec.multi) {
        text = text.replace(/\s+/g, " ");
      }
      if (ctx.spec.max) {
        var room = ctx.spec.max - (readText(ctx.el, ctx.spec.multi).length - selectionLength());
        if (room <= 0) {
          showCounter(ctx);
          return;
        }
        text = text.slice(0, room);
      }
      if (text) {
        insertPlain(text, ctx.spec.multi);
      }
    });
    fdoc.addEventListener("keydown", function (event) {
      var ctx = fieldContext(event.target);
      if (event.key === "Escape") {
        if (closeSurface(true)) {
          event.preventDefault();
          return;
        }
        event.preventDefault();
        if (ctx) {
          ctx.el.blur();
        }
        focusToolbar();
        return;
      }
      if (!ctx) {
        var blockEl = event.target.closest ? event.target.closest("[data-pb-block]") : null;
        if (blockEl && blockEl === event.target && event.key === "Enter") {
          event.preventDefault();
          select(blockEl.getAttribute("data-pb-block"));
          focusToolbar();
        }
        return;
      }
      if (event.key === "Enter") {
        if (ctx.spec.multi) {
          event.preventDefault();
          insertBreak();
          return;
        }
        event.preventDefault();
        if (ctx.spec.line && !event.shiftKey) {
          commitText(ctx, true);
          addLineAfter(ctx);
          return;
        }
        ctx.el.blur();
        return;
      }
      if (event.key === "Backspace" && ctx.spec.line && !readText(ctx.el, false)) {
        event.preventDefault();
        removeLine(ctx);
      }
    });
    fdoc.addEventListener(
      "keyup",
      function (event) {
        if (event.key === " " && event.target.closest && event.target.closest("summary")) {
          event.preventDefault();
        }
      },
      true
    );
  }

  // -------------------------------------------------------------------------
  // Lagret ovanpå ramen: markering, verktygsrad, "Lägg till block", linjen
  // -------------------------------------------------------------------------

  function frameOrigin() {
    var fr = frame.getBoundingClientRect();
    var sr = stage.getBoundingClientRect();
    return { x: fr.left - sr.left, y: fr.top - sr.top };
  }

  function rectOf(node) {
    var origin = frameOrigin();
    var r = node.getBoundingClientRect();
    var top = origin.y + r.top * scale;
    var left = origin.x + r.left * scale;
    return {
      top: top,
      left: left,
      width: r.width * scale,
      height: r.height * scale,
      bottom: top + r.height * scale,
      right: left + r.width * scale,
    };
  }

  var ov = {
    hover: h("div", { class: "pb-box pb-box--hover", hidden: true }, h("span", { class: "pb-box__tag" })),
    sel: h("div", { class: "pb-box pb-box--sel", hidden: true }, h("span", { class: "pb-box__tag" })),
    marks: [],
    inserts: [],
    tools: [],
    drop: h("div", { class: "pb-drop", hidden: true }),
  };
  if (overlay) {
    overlay.appendChild(ov.hover);
    overlay.appendChild(ov.sel);
    overlay.appendChild(ov.drop);
  }

  function placeBox(box, rect, tag) {
    if (!rect) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.style.top = Math.round(rect.top) + "px";
    box.style.left = Math.round(rect.left) + "px";
    box.style.width = Math.round(rect.width) + "px";
    box.style.height = Math.round(rect.height) + "px";
    var label = box.querySelector(".pb-box__tag");
    if (label) {
      label.textContent = tag || "";
      label.hidden = !tag;
    }
  }

  function problemsFor(id) {
    return state.problems.filter(function (p) {
      return p.block === id;
    });
  }

  var geometry = { order: [], rects: {} };

  function drawOverlay() {
    if (!fdoc || !overlay) {
      return;
    }
    var nodes = blockNodes();
    geometry = { order: [], rects: {} };
    nodes.forEach(function (node) {
      var id = node.getAttribute("data-pb-block");
      geometry.order.push(id);
      geometry.rects[id] = rectOf(node);
    });
    drawHover();
    var selected = findBlock(state.selectedId);
    placeBox(ov.sel, selected ? geometry.rects[state.selectedId] : null, selected ? typeOf(selected).name : "");
    drawMarks(nodes);
    drawInserts();
    placeToolbar();
    drawTools();
  }

  function drawHover() {
    var hoverBlock = hoverId && hoverId !== state.selectedId && !dragging ? findBlock(hoverId) : null;
    var rect = hoverBlock ? geometry.rects[hoverId] : null;
    if (hoverBlock && !rect) {
      var node = blockNode(hoverId);
      rect = node ? rectOf(node) : null;
    }
    placeBox(ov.hover, rect, hoverBlock ? typeOf(hoverBlock).name : "");
  }

  function drawMarks(nodes) {
    ov.marks.forEach(function (mark) {
      mark.remove();
    });
    ov.marks = [];
    nodes.forEach(function (node) {
      var id = node.getAttribute("data-pb-block");
      var count = problemsFor(id).length;
      var hidden = node.hasAttribute("data-pb-hidden");
      if (!count && !hidden) {
        return;
      }
      var text = [];
      if (hidden) {
        text.push(node.getAttribute("data-pb-type") === "before_after" ? "Syns när båda bilderna är valda" : "Syns inte för besökarna än");
      }
      if (count) {
        text.push(count === 1 ? "1 sak att rätta" : count + " saker att rätta");
      }
      var mark = h(
        "div",
        { class: "pb-box pb-box--mark" + (count ? " is-problem" : "") },
        h("span", { class: "pb-box__note", text: text.join(". ") })
      );
      placeBox(mark, geometry.rects[id], "");
      overlay.insertBefore(mark, ov.hover);
      ov.marks.push(mark);
    });
  }

  // "Lägg till block" mellan blocken.

  var insertNear = -1;
  var insertLeaveTimer = null;

  function gapLabel(gap) {
    if (!state.blocks.length) {
      return "Lägg till det första blocket";
    }
    if (gap === 0) {
      return "Lägg till block först på sidan";
    }
    var before = state.blocks[gap - 1];
    return "Lägg till block efter " + (before ? typeOf(before).name : "blocket");
  }

  function gapY(gap) {
    var order = geometry.order;
    if (!order.length) {
      var main = fdoc && fdoc.querySelector("main.rn-main");
      return main ? rectOf(main).top + 24 : 24;
    }
    if (gap < order.length) {
      return geometry.rects[order[gap]].top;
    }
    return geometry.rects[order[order.length - 1]].bottom;
  }

  function drawInserts() {
    var count = readOnly ? 0 : geometry.order.length + 1;
    while (ov.inserts.length > count) {
      ov.inserts.pop().remove();
    }
    while (ov.inserts.length < count) {
      (function (index) {
        var button = h(
          "button",
          { type: "button", class: "pb-ins__btn" },
          icon("pb-u-plus"),
          h("span", { text: "Lägg till block" })
        );
        var row = h("div", { class: "pb-ins" }, button);
        button.addEventListener("click", function () {
          openLibrary(index, button);
        });
        button.addEventListener("pointerenter", function () {
          window.clearTimeout(insertLeaveTimer);
        });
        button.addEventListener("pointerleave", leaveInsertSoon);
        overlay.appendChild(row);
        ov.inserts.push(row);
      })(ov.inserts.length);
    }
    var selectedGap = state.selectedId ? indexOf(state.selectedId) + 1 : -1;
    ov.inserts.forEach(function (row, gap) {
      row.style.top = Math.round(gapY(gap)) + "px";
      row.classList.toggle("is-near", gap === insertNear);
      row.classList.toggle("is-sel", gap === selectedGap);
      row.firstChild.setAttribute("aria-label", gapLabel(gap));
    });
  }

  function nearInsert(y) {
    var near = -1;
    for (var gap = 0; gap <= geometry.order.length; gap++) {
      if (Math.abs(gapY(gap) - y) < 26) {
        near = gap;
        break;
      }
    }
    window.clearTimeout(insertLeaveTimer);
    if (near !== insertNear) {
      insertNear = near;
      ov.inserts.forEach(function (row, gap) {
        row.classList.toggle("is-near", gap === insertNear);
      });
    }
  }

  function leaveInsertSoon() {
    window.clearTimeout(insertLeaveTimer);
    insertLeaveTimer = window.setTimeout(function () {
      insertNear = -1;
      ov.inserts.forEach(function (row) {
        row.classList.remove("is-near");
      });
    }, 500);
  }

  // Verktygsraden för det valda blocket (mockupen .blk-tb).

  var toolbar = h("div", { class: "pb-tb", role: "toolbar", hidden: true });
  if (overlay) {
    overlay.appendChild(toolbar);
  }
  var tb = {};

  function tbButton(name, iconId, label, opts) {
    opts = opts || {};
    var button = h(
      "button",
      {
        type: "button",
        class: "pb-tb__btn" + (opts.iconOnly ? " pb-tb__btn--icon" : "") + (opts.cls ? " " + opts.cls : ""),
        "aria-label": opts.aria || (opts.iconOnly ? label : null),
        title: opts.iconOnly ? label : null,
        "aria-haspopup": opts.popup ? "dialog" : null,
        "data-tb": name,
      },
      icon(iconId),
      opts.iconOnly ? null : h("span", { class: "pb-tb__label", text: label }),
      opts.chev ? icon("pb-u-chev", "pb-tb__chev") : null
    );
    tb[name] = button;
    return button;
  }

  function buildToolbar() {
    toolbar.appendChild(tbButton("handle", "pb-u-drag", "Block", { cls: "pb-tb__handle" }));
    toolbar.appendChild(h("span", { class: "pb-tb__sep", "aria-hidden": "true" }));
    toolbar.appendChild(tbButton("variant", "pb-u-variant", "Variant", { popup: true, chev: true, cls: "is-hl" }));
    toolbar.appendChild(tbButton("versions", "pb-u-history", "Versioner", { popup: true }));
    toolbar.appendChild(tbButton("list", "pb-u-list", "Lista", { popup: true }));
    toolbar.appendChild(tbButton("ai", "pb-u-sparkle", "Skriv om"));
    toolbar.appendChild(h("span", { class: "pb-tb__sep", "aria-hidden": "true" }));
    toolbar.appendChild(tbButton("up", "pb-u-up", "Flytta upp", { iconOnly: true }));
    toolbar.appendChild(tbButton("down", "pb-u-down", "Flytta ner", { iconOnly: true }));
    toolbar.appendChild(tbButton("copy", "pb-u-copy", "Kopiera blocket", { iconOnly: true }));
    toolbar.appendChild(tbButton("delete", "pb-u-trash", "Ta bort blocket", { iconOnly: true }));

    toolbar.addEventListener("click", function (event) {
      var button = event.target.closest("[data-tb]");
      var id = state.selectedId;
      if (!button || !id || button.disabled) {
        return;
      }
      var name = button.getAttribute("data-tb");
      if (name === "handle") {
        if (suppressClick) {
          suppressClick = false;
          return;
        }
        focusField(id) || button.focus();
      } else if (name === "variant") {
        openVariants(id, button);
      } else if (name === "versions") {
        openVersions(id, button);
      } else if (name === "list") {
        var listKey = listFieldOf(findBlock(id));
        if (listKey) {
          openList(id, listKey, button);
        }
      } else if (name === "ai") {
        openPanel("ai", { blockId: id, opener: button });
      } else if (name === "up") {
        moveBlock(id, -1, button);
      } else if (name === "down") {
        moveBlock(id, 1, button);
      } else if (name === "copy") {
        copyBlock(id);
      } else if (name === "delete") {
        deleteBlock(id);
      }
    });
    toolbar.addEventListener("keydown", function (event) {
      var buttons = $all(".pb-tb__btn", toolbar).filter(function (b) {
        return !b.disabled && b.offsetParent !== null;
      });
      var index = buttons.indexOf(document.activeElement);
      if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
        event.preventDefault();
        var next = buttons[(index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length];
        rove(next);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        rove(event.key === "Home" ? buttons[0] : buttons[buttons.length - 1]);
      } else if ((event.key === "ArrowUp" || event.key === "ArrowDown") && document.activeElement === tb.handle) {
        event.preventDefault();
        moveBlock(state.selectedId, event.key === "ArrowUp" ? -1 : 1, tb.handle);
      } else if (event.key === "Escape") {
        event.preventDefault();
        if (!closeSurface(true)) {
          if (!focusField(state.selectedId)) {
            select(null);
          }
        }
      }
    });
    tb.handle.addEventListener("pointerdown", function (event) {
      if (state.selectedId) {
        startDrag(event, { kind: "move", blockId: state.selectedId, source: tb.handle });
      }
    });
  }

  function rove(button) {
    if (!button) {
      return;
    }
    $all(".pb-tb__btn", toolbar).forEach(function (b) {
      b.tabIndex = b === button ? 0 : -1;
    });
    button.focus();
  }

  function focusToolbar() {
    if (!state.selectedId || isMobile() || toolbar.hidden) {
      return false;
    }
    var current = $all(".pb-tb__btn", toolbar).filter(function (b) {
      return b.tabIndex === 0 && !b.disabled;
    })[0];
    rove(current || tb.handle);
    return true;
  }

  function listFieldOf(block) {
    var type = typeOf(block);
    if (!type) {
      return null;
    }
    var key = null;
    type.fields.some(function (spec) {
      if ((spec.kind === "lines" || spec.kind === "items") && usedBy(spec, block.variant)) {
        key = spec.key;
        return true;
      }
      return false;
    });
    return key;
  }

  function updateToolbar(block) {
    var type = typeOf(block);
    var index = indexOf(block.id);
    var variant = variantOf(block);
    tb.handle.querySelector(".pb-tb__label").textContent = type.name;
    tb.handle.setAttribute("aria-label", "Flytta " + type.name + ". Dra, eller använd pil upp och pil ner.");
    tb.variant.querySelector(".pb-tb__label").textContent = variant ? variant.name : "Variant";
    tb.variant.setAttribute("aria-label", "Variant av " + type.name + ": " + (variant ? variant.name : ""));
    tb.variant.disabled = !type.variants || type.variants.length < 2;
    tb.versions.querySelector(".pb-tb__label").textContent = "Versioner " + block.versions.length;
    tb.versions.setAttribute("aria-label", "Versioner av " + type.name + ", " + block.versions.length + " st");
    var listKey = listFieldOf(block);
    tb.list.hidden = !listKey;
    if (listKey) {
      var spec = findField(type, listKey);
      var items = activeFields(block)[listKey] || [];
      tb.list.querySelector(".pb-tb__label").textContent = spec.label + " " + items.length;
      tb.list.setAttribute("aria-label", "Ändra " + lower(spec.label) + " i " + type.name);
    }
    tb.ai.setAttribute("aria-label", "Skriv om " + type.name + " med AI");
    tb.up.disabled = index <= 0;
    tb.down.disabled = index < 0 || index >= state.blocks.length - 1;
    tb.copy.disabled = !!type.single || state.blocks.length >= MAX_BLOCKS;
    tb.copy.title = type.single ? "Sidan kan bara ha ett block av sorten " + type.name + "." : "Kopiera blocket";
    toolbar.setAttribute("aria-label", "Verktyg för " + type.name);
    if (!$all(".pb-tb__btn", toolbar).some(function (b) {
      return b.tabIndex === 0 && !b.disabled && !b.hidden;
    })) {
      $all(".pb-tb__btn", toolbar).forEach(function (b) {
        b.tabIndex = b === tb.handle ? 0 : -1;
      });
    }
  }

  function placeToolbar() {
    var block = findBlock(state.selectedId);
    var rect = block ? geometry.rects[block.id] : null;
    if (!rect || readOnly || isMobile() || dragging) {
      toolbar.hidden = true;
      return;
    }
    var hadFocus = toolbar.contains(document.activeElement);
    toolbar.hidden = false;
    updateToolbar(block);
    var height = toolbar.offsetHeight || 42;
    var width = toolbar.offsetWidth || 400;
    var top = rect.top - height / 2;
    if (top < 4) {
      top = rect.top + 8;
    }
    var sr = stage.getBoundingClientRect();
    var limit = topbarHeight() + 8 - sr.top;
    if (top < limit && rect.bottom - height - 16 > limit) {
      top = limit;
    }
    var cr = canvas.getBoundingClientRect();
    var minLeft = cr.left - sr.left + 8;
    var maxLeft = cr.right - sr.left - width - 8;
    var left = Math.max(minLeft, Math.min(rect.left + 12, maxLeft));
    toolbar.style.top = Math.round(top) + "px";
    toolbar.style.left = Math.round(left) + "px";
    if (hadFocus && !toolbar.contains(document.activeElement)) {
      focusToolbar();
    }
  }

  window.addEventListener(
    "scroll",
    function () {
      if (!toolbar.hidden || surface) {
        window.requestAnimationFrame(function () {
          placeToolbar();
          positionSurface();
        });
      }
    },
    { passive: true }
  );

  // Knappar för bilderna och listan i det valda blocket.

  /* Bilden som syns i en punkt i ramen (ramens egna koordinater). I
     reglaget för före och efter ligger bilderna på varandra och den övre är
     beskuren; elementsFromPoint följer beskärningen, så det är bilden som
     syns i punkten som svarar. */
  function mediaAt(x, y) {
    if (!fdoc || !fdoc.elementsFromPoint) {
      return null;
    }
    var stack = fdoc.elementsFromPoint(x, y);
    for (var i = 0; i < stack.length; i++) {
      if (stack[i].hasAttribute && stack[i].hasAttribute("data-pb-media")) {
        return stack[i];
      }
    }
    return null;
  }

  /* Vilken sida av bilden som syns: "right" när bildens vänstra del inte
     syns (efter-bilden i reglaget är beskuren där, före-bilden syns i
     stället), annars "left". Knappen och klicket hamnar då på samma bild. */
  function mediaSide(el) {
    var r = el.getBoundingClientRect();
    var inset = Math.min(24, r.width / 4);
    var y = r.top + Math.min(24, r.height / 2);
    if (mediaAt(r.left + inset, y) !== el && mediaAt(r.right - inset, y) === el) {
      return "right";
    }
    return "left";
  }

  function drawTools() {
    ov.tools.forEach(function (tool) {
      tool.remove();
    });
    ov.tools = [];
    var block = findBlock(state.selectedId);
    var node = blockNode(state.selectedId);
    if (!block || !node || readOnly || dragging) {
      return;
    }
    var type = typeOf(block);
    var fields = activeFields(block);
    var media = type.fields.filter(function (spec) {
      return spec.kind === "media" && usedBy(spec, block.variant);
    });
    media.forEach(function (spec) {
      var el = node.querySelector('[data-pb-media][data-pb-field="' + spec.key + '"]');
      if (!el) {
        return;
      }
      var rect = rectOf(el);
      if (rect.width < 2) {
        return;
      }
      var has = fields[spec.key] !== null && fields[spec.key] !== undefined;
      var label = (has ? "Byt bild" : "Välj bild") + (media.length > 1 ? ": " + spec.label : "");
      var button = h(
        "button",
        { type: "button", class: "pb-chip", "aria-label": label + " i " + type.name },
        icon("pb-u-image"),
        h("span", { text: label })
      );
      button.style.top = Math.round(rect.top + 12) + "px";
      button.style.left = Math.round(rect.left + 12) + "px";
      button.addEventListener("click", function () {
        openMedia(block.id, spec.key, button);
      });
      overlay.appendChild(button);
      if (media.length > 1 && mediaSide(el) === "right") {
        button.style.left = Math.round(Math.max(rect.left + 12, rect.right - button.offsetWidth - 12)) + "px";
      }
      ov.tools.push(button);
    });
    if (isMobile()) {
      var listKey = listFieldOf(block);
      if (listKey) {
        var spec = findField(type, listKey);
        var rect = geometry.rects[block.id];
        var chip = h(
          "button",
          { type: "button", class: "pb-chip pb-chip--list" },
          icon("pb-u-list"),
          h("span", { text: "Ändra " + lower(spec.label) })
        );
        chip.style.top = Math.round(rect.bottom) + "px";
        chip.style.left = Math.round(rect.left + rect.width / 2) + "px";
        chip.addEventListener("click", function () {
          openList(block.id, listKey, chip);
        });
        overlay.appendChild(chip);
        ov.tools.push(chip);
      }
    }
  }

  // -------------------------------------------------------------------------
  // Välja ett block
  // -------------------------------------------------------------------------

  function select(id, opts) {
    opts = opts || {};
    if (id && !findBlock(id)) {
      id = null;
    }
    if (state.selectedId === id) {
      if (opts.scroll && id) {
        scrollToBlock(id);
      }
      return;
    }
    state.selectedId = id;
    if (fdoc) {
      $all(".pb-is-sel", fdoc).forEach(function (n) {
        n.classList.remove("pb-is-sel");
      });
      var node = blockNode(id);
      if (node) {
        node.classList.add("pb-is-sel");
      }
    }
    if (surface && surface.blockId && surface.blockId !== id) {
      closeSurface(false);
    }
    drawOverlay();
    updateMbar();
    if (id) {
      firstSelection();
    } else {
      updateMhint();
    }
    if (opts.scroll && id) {
      scrollToBlock(id);
    }
    emit("select", { blockId: id });
  }

  function scrollToBlock(id, opts) {
    var rect = geometry.rects[id];
    if (!rect) {
      return;
    }
    var sr = stage.getBoundingClientRect();
    var top = window.scrollY + sr.top + rect.top - topbarHeight() - 24;
    var visibleTop = window.scrollY + topbarHeight();
    var visibleBottom = window.scrollY + window.innerHeight - (isMobile() ? 90 : 0);
    var blockTop = window.scrollY + sr.top + rect.top;
    if (opts && opts.ifNeeded && blockTop > visibleTop && blockTop + Math.min(rect.height, 200) < visibleBottom) {
      return;
    }
    window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  }

  /* Håll blocket, eller ett fält i det (bilden som väljs), i bild när en
     panel tar plats: i datorn blir duken smalare och ramen ritas om, i
     mobilen täcker arket skärmens nedre del. Väntar tills ramen slutat
     ändra sig (högst tolv bildrutor) och rullar sedan sidan om det behövs. */
  function keepInView(blockId, path) {
    var last = "";
    var frames = 0;
    function target() {
      var node = blockNode(blockId);
      if (!node) {
        return null;
      }
      var field = path ? node.querySelector('[data-pb-field="' + path + '"]') : null;
      return field && field.getBoundingClientRect().height > 0 ? field : node;
    }
    function step() {
      var el = target();
      if (!el || !stage) {
        return;
      }
      var rect = rectOf(el);
      var key = [Math.round(rect.top), Math.round(rect.height), Math.round(rect.width)].join(":");
      if (key !== last && frames < 12) {
        last = key;
        frames += 1;
        window.requestAnimationFrame(step);
        return;
      }
      var top = stage.getBoundingClientRect().top + rect.top;
      /* Toppraden står kvar överst när sidan rullat (sticky), och i datorn
         blockets verktygsrad under den. */
      var visibleTop = topbarHeight() + 8;
      if (path && !isMobile() && !toolbar.hidden) {
        visibleTop += (toolbar.offsetHeight || 42) + 8;
      }
      var visibleBottom = window.innerHeight;
      if (isMobile() && panel && !panel.hidden) {
        visibleBottom = Math.min(visibleBottom, panel.getBoundingClientRect().top);
        /* Plats att rulla till även för ett block längst ner på sidan. */
        root.style.paddingBottom = Math.ceil(window.innerHeight - visibleBottom + 24) + "px";
      }
      /* Ett fält (bilden) ska synas helt om det ryms, ett block med sin
         början. */
      var need = Math.min(rect.height, path ? rect.height : 200, Math.max(40, visibleBottom - visibleTop - 16));
      if (top >= visibleTop && top + need <= visibleBottom - 8) {
        return;
      }
      window.scrollTo({ top: Math.max(0, window.scrollY + top - visibleTop - 8), behavior: "smooth" });
    }
    window.requestAnimationFrame(step);
  }

  // -------------------------------------------------------------------------
  // Lägga till, flytta, kopiera och ta bort block
  // -------------------------------------------------------------------------

  function addBlockAt(typeKey, gap, opts) {
    opts = opts || {};
    var check = canAdd(typeKey);
    if (!check.ok) {
      toast(check.reason || "Blocket går inte att lägga till.");
      return Promise.reject(new Error(check.reason || "Blocket går inte att lägga till."));
    }
    var payload = { type: typeKey, variant: opts.variant || null };
    if (opts.fields) {
      payload.fields = opts.fields;
      payload.source = opts.source || null;
    }
    root.classList.add("is-rendering");
    return post(urls.newBlock, payload).then(function (res) {
      if (!res.ok || !res.data.block) {
        root.classList.remove("is-rendering");
        var message = res.data.error || "Blocket gick inte att lägga till.";
        toast(message);
        throw new Error(message);
      }
      var block = res.data.block;
      var at = Math.max(0, Math.min(gap, state.blocks.length));
      state.blocks.splice(at, 0, block);
      afterRender(function () {
        select(block.id);
        scrollToBlock(block.id, { ifNeeded: true });
        announce(typeOf(block).name + " lades till som block " + (indexOf(block.id) + 1) + " av " + state.blocks.length + ".");
      });
      changed("add", { blockId: block.id, render: true });
      return block;
    });
  }

  function moveTo(id, gap) {
    var from = indexOf(id);
    if (from < 0 || gap === from || gap === from + 1) {
      return false;
    }
    var block = state.blocks.splice(from, 1)[0];
    var to = gap > from ? gap - 1 : gap;
    state.blocks.splice(to, 0, block);
    afterRender(function () {
      scrollToBlock(id, { ifNeeded: true });
    });
    changed("move", { blockId: id, render: true });
    announce(typeOf(block).name + " är nu block " + (to + 1) + " av " + state.blocks.length + ".");
    return true;
  }

  function moveBlock(id, delta, refocus) {
    var from = indexOf(id);
    var to = from + delta;
    if (from < 0 || to < 0 || to >= state.blocks.length) {
      return;
    }
    moveTo(id, delta > 0 ? to + 1 : to);
    if (refocus) {
      afterRender(function () {
        if (refocus.isConnected && !refocus.disabled) {
          refocus.focus();
        } else {
          focusToolbar();
        }
      });
    }
  }

  function copyBlock(id) {
    var index = indexOf(id);
    var source = findBlock(id);
    var type = typeOf(source);
    if (!source || type.single) {
      toast("Sidan kan bara ha ett block av sorten " + (type ? type.name : "") + ".");
      return;
    }
    if (state.blocks.length >= MAX_BLOCKS) {
      toast("Högst " + MAX_BLOCKS + " block på en sida.");
      return;
    }
    var copy = clone(source);
    copy.id = uid("b_");
    var ids = {};
    copy.versions.forEach(function (version) {
      var next = uid("v_");
      ids[version.id] = next;
      version.id = next;
    });
    copy.active = ids[copy.active] || copy.versions[copy.versions.length - 1].id;
    state.blocks.splice(index + 1, 0, copy);
    afterRender(function () {
      select(copy.id);
      scrollToBlock(copy.id, { ifNeeded: true });
    });
    changed("copy", { blockId: copy.id, render: true });
    toast(type.name + " kopierades.");
  }

  function deleteBlock(id) {
    var index = indexOf(id);
    if (index < 0) {
      return;
    }
    var block = state.blocks[index];
    var type = typeOf(block);
    state.blocks.splice(index, 1);
    var neighbour = state.blocks[index] || state.blocks[index - 1] || null;
    select(neighbour ? neighbour.id : null);
    changed("delete", { blockId: id, render: true });
    toast(type.name + " togs bort.", {
      label: "Ångra",
      run: function () {
        if (findBlock(block.id)) {
          return;
        }
        state.blocks.splice(Math.min(index, state.blocks.length), 0, block);
        afterRender(function () {
          select(block.id);
          scrollToBlock(block.id, { ifNeeded: true });
        });
        changed("undo", { blockId: block.id, render: true });
      },
    });
  }

  // -------------------------------------------------------------------------
  // Dra och släpp (pekare: mus, penna och finger; ingen HTML5-dragning)
  // -------------------------------------------------------------------------

  var dragging = null;
  var suppressClick = false;

  function gapAt(clientY) {
    var sr = stage.getBoundingClientRect();
    var y = clientY - sr.top;
    var order = geometry.order;
    for (var i = 0; i < order.length; i++) {
      var r = geometry.rects[order[i]];
      if (y < r.top + r.height / 2) {
        return i;
      }
    }
    return order.length;
  }

  function startDrag(event, opts) {
    if (readOnly || (event.button !== undefined && event.button !== 0)) {
      return;
    }
    var start = { x: event.clientX, y: event.clientY };
    var pointerId = event.pointerId;
    var source = opts.source;
    var touch = event.pointerType === "touch";
    var active = false;
    var longTimer = null;
    var scrollTimer = null;
    var lastY = event.clientY;
    var gap = -1;
    var ghost = null;

    function begin() {
      active = true;
      dragging = opts;
      closeSurface(false);
      try {
        source.setPointerCapture(pointerId);
      } catch (error) {
        /* pekaren finns inte längre */
      }
      var type = opts.kind === "new" ? TYPES[opts.type] : typeOf(findBlock(opts.blockId));
      ghost = h(
        "div",
        { class: "pb-ghost", "aria-hidden": "true" },
        h("span", { class: "pb-ghost__icon" }, icon("pb-i-" + (type ? type.icon : "hero"))),
        h("span", { text: type ? type.name : "" })
      );
      document.body.appendChild(ghost);
      root.classList.add("is-dragging");
      toolbar.hidden = true;
      drawTools();
      move(start.x, start.y);
      scrollTimer = window.setInterval(autoScroll, 30);
    }

    function move(x, y) {
      lastY = y;
      if (ghost) {
        ghost.style.transform = "translate(" + Math.round(x + 12) + "px," + Math.round(y + 12) + "px) rotate(-2deg)";
      }
      gap = gapAt(y);
      var from = opts.kind === "move" ? indexOf(opts.blockId) : -1;
      var noop = from >= 0 && (gap === from || gap === from + 1);
      if (noop || !geometry.order.length && opts.kind === "move") {
        ov.drop.hidden = true;
      } else {
        ov.drop.hidden = false;
        ov.drop.style.top = Math.round(gapY(gap) - 2) + "px";
      }
    }

    function autoScroll() {
      var edge = 80;
      var top = topbarHeight() + edge;
      if (lastY < top) {
        window.scrollBy(0, -Math.ceil((top - lastY) / 4));
      } else if (lastY > window.innerHeight - edge) {
        window.scrollBy(0, Math.ceil((lastY - (window.innerHeight - edge)) / 4));
      }
    }

    function onMove(e) {
      if (e.pointerId !== pointerId) {
        return;
      }
      if (!active) {
        var distance = Math.abs(e.clientX - start.x) + Math.abs(e.clientY - start.y);
        if (touch) {
          if (distance > 10) {
            cleanup();
          }
          return;
        }
        if (distance > 6) {
          begin();
          move(e.clientX, e.clientY);
        }
        return;
      }
      e.preventDefault();
      move(e.clientX, e.clientY);
    }

    function onTouchMove(e) {
      if (active) {
        e.preventDefault();
      }
    }

    function onUp(e) {
      if (e.pointerId !== pointerId) {
        return;
      }
      if (active) {
        suppressClick = true;
        window.setTimeout(function () {
          suppressClick = false;
        }, 400);
        var target = gap;
        finish();
        if (opts.kind === "new") {
          addBlockAt(opts.type, target).catch(function () {});
        } else {
          moveTo(opts.blockId, target);
        }
        return;
      }
      cleanup();
    }

    function onCancel(e) {
      if (e.pointerId === pointerId) {
        finish();
      }
    }

    function finish() {
      if (ghost) {
        ghost.remove();
      }
      ov.drop.hidden = true;
      root.classList.remove("is-dragging");
      dragging = null;
      cleanup();
      drawOverlay();
    }

    function cleanup() {
      window.clearTimeout(longTimer);
      window.clearInterval(scrollTimer);
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onCancel);
      document.removeEventListener("touchmove", onTouchMove);
    }

    document.addEventListener("pointermove", onMove, { passive: false });
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onCancel);
    document.addEventListener("touchmove", onTouchMove, { passive: false });
    if (touch) {
      longTimer = window.setTimeout(function () {
        if (navigator.vibrate) {
          try {
            navigator.vibrate(10);
          } catch (error) {
            /* inte alla telefoner */
          }
        }
        begin();
      }, opts.kind === "new" ? 380 : 0);
    }
  }

  // -------------------------------------------------------------------------
  // Popover i datorn, ark nedifrån i mobilen
  // -------------------------------------------------------------------------

  var surface = null;
  var surfaceSeq = 0;

  function openSurface(opts) {
    closeSurface(false);
    var mobile = isMobile();
    var titleId = "pb-surface-title-" + ++surfaceSeq;
    var content = h("div", { class: "pb-surface__body" });
    var closeButton = h(
      "button",
      { type: "button", class: "pb-iconbtn", "aria-label": "Stäng" },
      icon("pb-u-close")
    );
    var el = h(
      "div",
      {
        class: (mobile ? "pb-sheet" : "pb-pop") + (opts.wide ? " is-wide" : "") + (opts.cls ? " " + opts.cls : ""),
        role: "dialog",
        "aria-modal": mobile ? "true" : null,
        "aria-labelledby": titleId,
      },
      h("div", { class: "pb-surface__head" }, h("h2", { class: "pb-surface__title", id: titleId, tabindex: "-1", text: opts.title }), closeButton),
      content
    );
    var back = mobile ? h("div", { class: "pb-sheet-back" }) : null;
    surface = {
      el: el,
      back: back,
      anchor: opts.anchor || null,
      blockId: opts.blockId || null,
      onClose: opts.onClose || null,
      name: opts.name || "",
      body: content,
    };
    closeButton.addEventListener("click", function () {
      closeSurface(true);
    });
    if (back) {
      back.addEventListener("click", function () {
        closeSurface(true);
      });
      document.body.appendChild(back);
    }
    document.body.appendChild(el);
    root.classList.add("has-surface");
    opts.build(content, surface);
    if (opts.anchor && opts.anchor.setAttribute) {
      opts.anchor.setAttribute("aria-expanded", "true");
    }
    positionSurface();
    var first = el.querySelector("[data-pb-autofocus]") || el.querySelector(".pb-surface__title");
    if (first) {
      first.focus({ preventScroll: true });
    }
    return surface;
  }

  function positionSurface() {
    if (!surface || !surface.el.classList.contains("pb-pop")) {
      return;
    }
    var el = surface.el;
    var anchor = surface.anchor;
    var rect = anchor && anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : null;
    if (!rect || (!rect.width && !rect.height)) {
      var sel = geometry.rects[state.selectedId];
      var sr = stage.getBoundingClientRect();
      rect = sel
        ? { left: sr.left + sel.left + 12, right: sr.left + sel.left + 12, top: sr.top + sel.top, bottom: sr.top + sel.top + 40, width: 1, height: 40 }
        : { left: window.innerWidth / 2 - 170, right: window.innerWidth / 2, top: 120, bottom: 160, width: 1, height: 40 };
    }
    var width = el.offsetWidth;
    var height = el.offsetHeight;
    var left = Math.max(16, Math.min(rect.left, window.innerWidth - width - 16));
    var top = rect.bottom + 8;
    if (top + height > window.innerHeight - 12 && rect.top - height - 8 > topbarHeight()) {
      top = rect.top - height - 8;
    }
    top = Math.max(topbarHeight() + 8, Math.min(top, window.innerHeight - height - 12));
    el.style.left = Math.round(left) + "px";
    el.style.top = Math.round(top) + "px";
  }

  /* Stänger arket eller popovern. true om något var öppet. */
  function closeSurface(restoreFocus) {
    if (!surface) {
      return false;
    }
    var current = surface;
    surface = null;
    current.el.remove();
    if (current.back) {
      current.back.remove();
    }
    root.classList.remove("has-surface");
    if (current.anchor && current.anchor.setAttribute) {
      current.anchor.setAttribute("aria-expanded", "false");
    }
    if (current.onClose) {
      current.onClose();
    }
    if (restoreFocus && current.anchor && current.anchor.isConnected && current.anchor.focus) {
      current.anchor.focus({ preventScroll: true });
    }
    return true;
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    if (closeSurface(true)) {
      event.preventDefault();
      return;
    }
    if (state.panel) {
      event.preventDefault();
      closePanel();
      return;
    }
    if (toolbar.contains(document.activeElement)) {
      return;
    }
    if (state.selectedId && !(document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName))) {
      select(null);
    }
  });

  document.addEventListener(
    "pointerdown",
    function (event) {
      if (!surface || !surface.el.classList.contains("pb-pop")) {
        return;
      }
      if (surface.el.contains(event.target)) {
        return;
      }
      if (surface.anchor && surface.anchor.contains && surface.anchor.contains(event.target)) {
        return;
      }
      closeSurface(false);
    },
    true
  );

  // Variant (mockupen .var-grid med skisser).

  function wireframe(typeKey, variantKey) {
    var spec = (WIREFRAMES[typeKey] || {})[variantKey] || "h l l";
    var wf = h("span", { class: "pb-wf", "aria-hidden": "true" });
    function piece(token) {
      var match = /^(nums|rows)(\d)$/.exec(token);
      if (match) {
        var group = h("span", { class: "pb-wf__" + match[1] });
        for (var i = 0; i < +match[2]; i++) {
          group.appendChild(h("i"));
        }
        return group;
      }
      if (token === "cards" || token === "chips" || token === "pair") {
        return h("span", { class: "pb-wf__" + token }, h("i"), h("i"), token === "pair" ? null : h("i"));
      }
      if (token === "stars") {
        return h("span", { class: "pb-wf__stars" }, h("i"), h("i"), h("i"), h("i"), h("i"));
      }
      if (token === "checks") {
        return h("span", { class: "pb-wf__checks" }, h("i"), h("i"));
      }
      return h("i", { class: "pb-wf__" + token });
    }
    if (spec.indexOf("row:") === 0) {
      wf.classList.add("pb-wf--row");
      spec.slice(4).split("|").forEach(function (column) {
        var col = h("span", { class: "pb-wf__col" });
        column.split(" ").forEach(function (token) {
          col.appendChild(piece(token));
        });
        wf.appendChild(col);
      });
    } else if (spec.indexOf("bar:") === 0) {
      wf.classList.add("pb-wf--bar");
      var bar = h("span", { class: "pb-wf__bar" });
      spec.slice(4).split(" ").forEach(function (token) {
        bar.appendChild(piece(token));
      });
      wf.appendChild(bar);
    } else {
      spec.split(" ").forEach(function (token) {
        wf.appendChild(piece(token));
      });
    }
    return wf;
  }

  function openVariants(id, anchor) {
    var block = findBlock(id);
    var type = typeOf(block);
    if (!block || !type) {
      return;
    }
    openSurface({
      title: "Variant av " + type.name,
      anchor: anchor,
      blockId: id,
      name: "variant",
      build: function (content) {
        var grid = h("div", { class: "pb-vars" });
        type.variants.forEach(function (variant) {
          var current = variant.key === block.variant;
          var button = h(
            "button",
            {
              type: "button",
              class: "pb-var",
              "aria-pressed": String(current),
              "data-pb-autofocus": current ? true : null,
            },
            wireframe(type.key, variant.key),
            h("span", { class: "pb-var__name" }, current ? icon("pb-u-check", "pb-var__check") : null, variant.name)
          );
          button.addEventListener("click", function () {
            if (variant.key !== block.variant) {
              block.variant = variant.key;
              changed("variant", { blockId: id, render: true });
              announce(type.name + ": " + variant.name + ".");
            }
            closeSurface(true);
          });
          grid.appendChild(button);
        });
        content.appendChild(grid);
        content.appendChild(h("p", { class: "pb-surface__note", text: "Texten följer med när du byter variant." }));
      },
    });
  }

  // Versioner (mockupen skärm 04).

  function versionPreview(fields) {
    var text = fields.title || fields.name || fields.text || fields.lead || "";
    if (!text) {
      Object.keys(fields).some(function (key) {
        var value = fields[key];
        if (Array.isArray(value) && value.length) {
          var first = value[0];
          text = typeof first === "string" ? first : first && (first.title || first.q || first.label || first.name) || "";
        }
        return !!text;
      });
    }
    text = String(text || "").replace(/\s+/g, " ").trim();
    return text.length > 90 ? text.slice(0, 87).trim() + "..." : text;
  }

  function versionWho(version) {
    if (mine(version)) {
      return "Du";
    }
    return SOURCE_LABELS[version.source] || "";
  }

  function versionIcon(version, active) {
    if (active) {
      return "pb-u-check";
    }
    if (version.source === "ai") {
      return "pb-u-sparkle";
    }
    if (version.source === "template") {
      return "pb-u-pages";
    }
    return "pb-u-history";
  }

  function openVersions(id, anchor) {
    var block = findBlock(id);
    var type = typeOf(block);
    if (!block || !type) {
      return;
    }
    openSurface({
      title: "Versioner av " + type.name,
      anchor: anchor,
      blockId: id,
      name: "versions",
      wide: true,
      build: function (content) {
        function render() {
          content.innerHTML = "";
          var add = h(
            "button",
            { type: "button", class: "pb-btn pb-btn--ghost pb-btn--sm", "data-pb-autofocus": true },
            icon("pb-u-plus"),
            h("span", { text: "Ny version" })
          );
          add.addEventListener("click", function () {
            var current = activeVersion(block);
            newVersion(block, clone((current && current.fields) || {}), me.source);
            afterRender(function () {
              focusField(id, null, true);
            });
            changed("version", { blockId: id, render: true });
            closeSurface(false);
            toast("En ny version är skapad och vald. Skriv ditt alternativ direkt på sidan.");
          });
          content.appendChild(
            h(
              "div",
              { class: "pb-vers__head" },
              h("p", { class: "pb-surface__note", text: "En version är blockets text. Den aktiva syns på sidan." }),
              add
            )
          );
          var list = h("ol", { class: "pb-vers" });
          var versions = block.versions.slice();
          for (var n = versions.length - 1; n >= 0; n--) {
            (function (version, number) {
              var active = version.id === block.active;
              var preview = versionPreview(version.fields || {});
              var row = h(
                "li",
                { class: "pb-ver" + (active ? " is-on" : "") },
                h("span", { class: "pb-ver__icon" }, icon(versionIcon(version, active))),
                h(
                  "div",
                  { class: "pb-ver__body" },
                  h("p", { class: "pb-ver__title", text: "Version " + number + (active ? ", aktiv" : "") }),
                  preview ? h("p", { class: "pb-ver__quote", text: '"' + preview + '"' }) : null,
                  h(
                    "p",
                    { class: "pb-ver__meta" },
                    h("span", { text: [versionWho(version), whenText(version.at)].filter(Boolean).join(", ") })
                  )
                )
              );
              if (!active) {
                var use = h("button", { type: "button", class: "pb-btn pb-btn--ghost pb-btn--sm", text: "Använd" });
                use.setAttribute("aria-label", "Använd version " + number);
                use.addEventListener("click", function () {
                  block.active = version.id;
                  changed("version", { blockId: id, render: true });
                  announce("Version " + number + " av " + type.name + " används nu.");
                  render();
                  var again = content.querySelector(".pb-ver.is-on");
                  if (again) {
                    again.setAttribute("tabindex", "-1");
                    again.focus({ preventScroll: true });
                  }
                });
                row.querySelector(".pb-ver__body").appendChild(use);
              }
              list.appendChild(row);
            })(versions[n], n + 1);
          }
          content.appendChild(list);
          content.appendChild(
            h("p", { class: "pb-surface__note", text: "Högst " + MAX_VERSIONS + " versioner per block. När det blir fler tas de äldsta bort, aldrig den aktiva." })
          );
        }
        render();
      },
    });
  }

  // Listor: punkter, steg, frågor, orter, certifikat, prisexempel, villkor.

  function pruneList(block, key) {
    var type = typeOf(block);
    var spec = findField(type, key);
    var version = activeVersion(block);
    if (!version || !spec) {
      return false;
    }
    var list = version.fields[key];
    if (!Array.isArray(list)) {
      return false;
    }
    var kept = list.filter(function (item) {
      if (spec.kind === "lines") {
        return String(item || "").trim() !== "";
      }
      return (spec.items || []).some(function (sub) {
        return (sub.kind === "text" || sub.kind === "textarea") && String((item || {})[sub.key] || "").trim() !== "";
      });
    });
    if (kept.length === list.length) {
      return false;
    }
    version.fields[key] = kept;
    return true;
  }

  function openList(id, key, anchor) {
    var block = findBlock(id);
    var type = typeOf(block);
    var spec = type ? findField(type, key) : null;
    if (!block || !spec || readOnly) {
      return;
    }
    var word = ADD_WORDS[type.key + "." + key] || lower(itemLabel(spec)) || "rad";
    var max = spec.max_items || 0;
    var variant = variantOf(block);
    var limit = variant && variant.limits ? variant.limits[key] : undefined;
    var one = itemLabel(spec);
    openSurface({
      title: spec.label + " i " + type.name,
      anchor: anchor,
      blockId: id,
      name: "list",
      wide: true,
      cls: "pb-pop--list",
      onClose: function () {
        if (pruneList(block, key)) {
          changed("list", { blockId: id, render: true });
        }
      },
      build: function (content) {
        function items() {
          var value = activeFields(block)[key];
          return Array.isArray(value) ? value : [];
        }
        function write(fn) {
          var version = ownVersion(block);
          if (!Array.isArray(version.fields[key])) {
            version.fields[key] = [];
          }
          fn(version.fields[key]);
          version.at = nowIso();
        }
        function render(focusIndex) {
          content.innerHTML = "";
          var list = h("ol", { class: "pb-rows" });
          items().forEach(function (item, i) {
            var row = h("li", { class: "pb-row" + (spec.kind === "lines" ? " pb-row--line" : "") });
            var inputs = h("div", { class: "pb-row__fields" });
            if (spec.kind === "lines") {
              var input = h("input", {
                type: "text",
                class: "pb-input",
                value: item || "",
                maxlength: spec.max_length || null,
                "aria-label": one + " " + (i + 1),
              });
              input.addEventListener("input", function () {
                var value = plain(input.value);
                write(function (arr) {
                  arr[i] = value;
                });
                changed("list", { blockId: id, render: true, delay: 450 });
              });
              inputs.appendChild(input);
            } else {
              (spec.items || []).forEach(function (sub) {
                if (sub.kind === "key") {
                  return;
                }
                var fieldId = "pb-row-" + i + "-" + sub.key;
                var value = (item || {})[sub.key];
                var control;
                if (sub.kind === "choice") {
                  control = h("select", { class: "pb-input", id: fieldId });
                  (sub.choices || []).forEach(function (choice) {
                    control.appendChild(h("option", { value: choice[0], text: choice[1], selected: value === choice[0] ? true : null }));
                  });
                } else if (sub.kind === "textarea") {
                  control = h("textarea", { class: "pb-input", id: fieldId, rows: "3", maxlength: sub.max_length || null });
                  control.value = value || "";
                } else {
                  control = h("input", { type: "text", class: "pb-input", id: fieldId, value: value || "", maxlength: sub.max_length || null });
                }
                control.addEventListener(sub.kind === "choice" ? "change" : "input", function () {
                  var next = sub.kind === "choice" ? control.value : plain(control.value);
                  write(function (arr) {
                    if (!arr[i] || typeof arr[i] !== "object") {
                      arr[i] = {};
                    }
                    arr[i][sub.key] = next;
                  });
                  changed("list", { blockId: id, render: true, delay: 450 });
                });
                /* Synligt: underfältets etikett ("Svar"); numret står i
                   radens ring. Uppläst: "Svar, fråga 2". */
                inputs.appendChild(
                  h(
                    "div",
                    { class: "pb-row__field" },
                    h(
                      "label",
                      { class: "pb-row__label", for: fieldId },
                      sub.label,
                      h("span", { class: "fl-sr", text: (lower(sub.label) === lower(one) ? " " : ", " + lower(one) + " ") + (i + 1) })
                    ),
                    control
                  )
                );
              });
            }
            var tools = h("div", { class: "pb-row__tools" });
            function tool(iconId, label, disabled, fn) {
              var button = h("button", { type: "button", class: "pb-iconbtn pb-iconbtn--sm", "aria-label": label, title: label, disabled: disabled ? true : null }, icon(iconId));
              button.addEventListener("click", fn);
              tools.appendChild(button);
            }
            tool("pb-u-up", "Flytta upp " + word + " " + (i + 1), i === 0, function () {
              write(function (arr) {
                arr.splice(i - 1, 0, arr.splice(i, 1)[0]);
              });
              changed("list", { blockId: id, render: true });
              render(i - 1);
            });
            tool("pb-u-down", "Flytta ner " + word + " " + (i + 1), i === items().length - 1, function () {
              write(function (arr) {
                arr.splice(i + 1, 0, arr.splice(i, 1)[0]);
              });
              changed("list", { blockId: id, render: true });
              render(i + 1);
            });
            tool("pb-u-trash", "Ta bort " + word + " " + (i + 1), false, function () {
              write(function (arr) {
                arr.splice(i, 1);
              });
              changed("list", { blockId: id, render: true });
              render(Math.max(0, i - 1));
            });
            row.appendChild(h("span", { class: "pb-row__n", "aria-hidden": "true", text: String(i + 1) }));
            row.appendChild(inputs);
            row.appendChild(tools);
            list.appendChild(row);
          });
          if (!items().length) {
            content.appendChild(h("p", { class: "pb-surface__note", text: "Inget här än." }));
          }
          content.appendChild(list);
          var full = max && items().length >= max;
          var add = h(
            "button",
            { type: "button", class: "pb-btn pb-btn--ghost pb-btn--sm", disabled: full ? true : null },
            icon("pb-u-plus"),
            h("span", { text: "Lägg till " + word })
          );
          add.addEventListener("click", function () {
            write(function (arr) {
              arr.push(spec.kind === "lines" ? "" : emptyItem(spec));
            });
            changed("list", { blockId: id, render: true, delay: 450 });
            render(items().length - 1);
          });
          var foot = h("div", { class: "pb-rows__foot" }, add);
          if (max) {
            foot.appendChild(h("span", { class: "pb-surface__note", text: "Högst " + max + "." }));
          }
          content.appendChild(foot);
          if (limit !== undefined && items().length > limit) {
            content.appendChild(
              h("p", { class: "pb-surface__note", text: "Varianten " + variant.name + " visar de " + limit + " första. Byt variant för att visa fler." })
            );
          }
          if (focusIndex !== undefined) {
            var rows = $all(".pb-row", list);
            var target = rows[focusIndex] ? rows[focusIndex].querySelector("input, textarea, select") : null;
            if (target) {
              target.focus();
            }
          }
          positionSurface();
        }
        render();
        var first = content.querySelector(".pb-input");
        if (first) {
          first.setAttribute("data-pb-autofocus", "");
        }
      },
    });
  }

  /* En ny post. En fråga i formuläret får en egen nyckel direkt (fältet
     q_<nyckel>), så att den syns i formuläret medan den skrivs. */
  function emptyItem(spec) {
    var item = {};
    (spec.items || []).forEach(function (sub) {
      if (sub.kind === "key") {
        item[sub.key] = "fraga-" + uid("").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 6);
      } else {
        item[sub.key] = sub.kind === "choice" && sub.choices && sub.choices.length ? sub.choices[0][0] : "";
      }
    });
    return item;
  }

  // "Mer" i mobilen: flytta, kopiera och ta bort det valda blocket.

  function openMove(id, anchor) {
    var block = findBlock(id);
    var type = typeOf(block);
    if (!block) {
      return;
    }
    openSurface({
      title: "Mer: " + type.name,
      anchor: anchor,
      blockId: id,
      name: "move",
      build: function (content) {
        var where = h("p", { class: "pb-surface__note", "aria-live": "polite" });
        var up = h("button", { type: "button", class: "pb-btn pb-btn--ghost pb-btn--lg", "data-pb-autofocus": true }, icon("pb-u-up"), h("span", { text: "Flytta upp" }));
        var down = h("button", { type: "button", class: "pb-btn pb-btn--ghost pb-btn--lg" }, icon("pb-u-down"), h("span", { text: "Flytta ner" }));
        var copy = h("button", { type: "button", class: "pb-btn pb-btn--ghost" }, icon("pb-u-copy"), h("span", { text: "Kopiera" }));
        var remove = h("button", { type: "button", class: "pb-btn pb-btn--ghost pb-btn--danger" }, icon("pb-u-trash"), h("span", { text: "Ta bort" }));
        var done = h("button", { type: "button", class: "pb-btn pb-btn--primary", text: "Klar" });
        function refresh() {
          var index = indexOf(id);
          where.textContent = "Block " + (index + 1) + " av " + state.blocks.length + ".";
          up.disabled = index <= 0;
          down.disabled = index >= state.blocks.length - 1;
          copy.disabled = !!type.single;
        }
        up.addEventListener("click", function () {
          moveBlock(id, -1);
          refresh();
        });
        down.addEventListener("click", function () {
          moveBlock(id, 1);
          refresh();
        });
        copy.addEventListener("click", function () {
          closeSurface(false);
          copyBlock(id);
        });
        remove.addEventListener("click", function () {
          closeSurface(false);
          deleteBlock(id);
        });
        done.addEventListener("click", function () {
          closeSurface(true);
        });
        content.appendChild(where);
        content.appendChild(h("div", { class: "pb-move" }, up, down));
        content.appendChild(h("div", { class: "pb-move pb-move--more" }, copy, remove));
        content.appendChild(h("div", { class: "pb-surface__actions" }, done));
        refresh();
      },
    });
  }

  // Biblioteket som rutor (mobilen och "Lägg till block").

  function openLibrary(gap, anchor) {
    var before = gap > 0 ? state.blocks[gap - 1] : null;
    var title = !state.blocks.length ? "Lägg till block" : before ? "Lägg till efter " + typeOf(before).name : "Lägg till först";
    openSurface({
      title: title,
      anchor: anchor,
      name: "library",
      wide: true,
      build: function (content) {
        var tiles = h("div", { class: "pb-tiles" });
        (config.groups || []).forEach(function (group) {
          Object.keys(TYPES).forEach(function (key) {
            var type = TYPES[key];
            if (type.group !== group.key) {
              return;
            }
            var check = canAdd(key);
            var tile = h(
              "button",
              { type: "button", class: "pb-tile", disabled: check.ok ? null : true, title: check.ok ? type.why : check.reason },
              h("span", { class: "pb-tile__icon", "aria-hidden": "true" }, icon("pb-i-" + type.icon)),
              h("span", { class: "pb-tile__name", text: type.name }),
              check.ok ? null : h("span", { class: "pb-tile__reason", text: check.reason })
            );
            tile.addEventListener("click", function () {
              if (suppressClick) {
                return;
              }
              closeSurface(false);
              addBlockAt(key, gap).catch(function () {});
            });
            tile.addEventListener("pointerdown", function (event) {
              if (check.ok && event.pointerType === "touch") {
                startDrag(event, { kind: "new", type: key, source: tile });
              }
            });
            tiles.appendChild(tile);
          });
        });
        content.appendChild(tiles);
        content.appendChild(
          h("p", { class: "pb-surface__note", text: isMobile() ? "Tryck för att lägga till, eller håll och dra till rätt plats." : "Klicka för att lägga till." })
        );
        /* Ett omdömesblock utan profil: länken till Omdömen, där profilerna
           kopplas (config.available[typ].link). */
        var profileLink = "";
        Object.keys(AVAILABLE).forEach(function (key) {
          var base = AVAILABLE[key];
          if (!profileLink && base && !base.ok && base.link) {
            profileLink = base.link.split("#")[0];
          }
        });
        if (profileLink && !readOnly) {
          content.appendChild(
            h("p", { class: "pb-surface__note" }, "Profilerna för omdömena kopplas under ", h("a", { href: profileLink, text: "Omdömen" }), ".")
          );
        }
        var first = tiles.querySelector(".pb-tile:not([disabled])");
        if (first) {
          first.setAttribute("data-pb-autofocus", "");
        }
      },
    });
  }

  // -------------------------------------------------------------------------
  // Biblioteket till vänster
  // -------------------------------------------------------------------------

  var library = $("#pb-lib");
  var libraryQuery = $("#pb-lib-q");

  function updateLibrary() {
    if (!library) {
      return;
    }
    $all("[data-pb-add]", library).forEach(function (button) {
      var check = canAdd(button.getAttribute("data-pb-add"));
      button.disabled = !check.ok;
      var reason = button.querySelector("[data-pb-reason]");
      if (reason) {
        reason.textContent = check.ok ? "" : check.reason;
      }
    });
  }

  function filterLibrary() {
    var query = fold(libraryQuery ? libraryQuery.value.trim() : "");
    var any = false;
    $all(".pb-lib__group", library).forEach(function (group) {
      var shown = 0;
      $all(".pb-lib__li", group).forEach(function (li) {
        var button = li.querySelector("[data-pb-add]");
        var match = !query || fold(button.getAttribute("data-pb-search")).indexOf(query) >= 0;
        li.hidden = !match;
        if (match) {
          shown += 1;
        }
      });
      group.hidden = !shown;
      any = any || shown > 0;
    });
    var none = $("#pb-lib-none");
    if (none) {
      none.hidden = any;
    }
  }

  if (library) {
    if (libraryQuery) {
      libraryQuery.addEventListener("input", filterLibrary);
    }
    $all("[data-pb-add]", library).forEach(function (button) {
      var key = button.getAttribute("data-pb-add");
      button.addEventListener("click", function () {
        if (suppressClick) {
          return;
        }
        addBlockAt(key, defaultGap(key)).catch(function () {});
      });
      button.addEventListener("pointerdown", function (event) {
        if (!button.disabled) {
          startDrag(event, { kind: "new", type: key, source: button });
        }
      });
    });
    updateLibrary();
  }

  // -------------------------------------------------------------------------
  // Raden längst ner i mobilen
  // -------------------------------------------------------------------------

  var mbar = $("#pb-mbar");
  var mhint = $("#pb-mhint");
  var mhintDone = false;
  var mhintNudge = false;
  var mhintTimer = null;
  try {
    mhintDone = window.sessionStorage.getItem("pb-mhint") === "1";
  } catch (error) {
    mhintDone = false;
  }

  /* Raden ovanför verktygen i mobilen: hur man börjar (tills det första
     blocket väljs, sedan inte mer under sessionen), och varför Variant,
     Versioner och Mer inte gör något utan ett valt block. Knapparna är
     aria-disabled, inte disabled, så att ett tryck kan förklara det. */
  function updateMhint() {
    if (!mhint) {
      return;
    }
    var text = "";
    if (mhintNudge) {
      text = readOnly ? "Sidan går inte att ändra här." : "Välj ett block först: tryck på det på sidan.";
    } else if (!mhintDone && !state.selectedId && !readOnly) {
      text = "Tryck på en text för att ändra den.";
    }
    mhint.textContent = text;
    mhint.hidden = !text;
    mhint.classList.toggle("is-nudge", mhintNudge);
  }

  function nudgeMhint() {
    mhintNudge = true;
    updateMhint();
    window.clearTimeout(mhintTimer);
    mhintTimer = window.setTimeout(function () {
      mhintNudge = false;
      updateMhint();
    }, 6000);
  }

  function firstSelection() {
    window.clearTimeout(mhintTimer);
    mhintNudge = false;
    if (!mhintDone) {
      mhintDone = true;
      try {
        window.sessionStorage.setItem("pb-mhint", "1");
      } catch (error) {
        /* bara en bekvämlighet */
      }
    }
    updateMhint();
  }

  function updateMbar() {
    if (!mbar) {
      return;
    }
    var block = findBlock(state.selectedId);
    $all("[data-pb-m]", mbar).forEach(function (button) {
      var name = button.getAttribute("data-pb-m");
      if (name === "variant" || name === "versions" || name === "move") {
        button.disabled = false;
        button.setAttribute("aria-disabled", String(!block || readOnly));
      }
    });
    var more = mbar.querySelector('[data-pb-m="move"]');
    if (more) {
      more.setAttribute("aria-label", block ? "Mer för " + typeOf(block).name + ": flytta, kopiera eller ta bort" : "Mer för det valda blocket");
    }
    mbar.setAttribute("aria-label", block ? "Verktyg för " + typeOf(block).name : "Verktyg för det valda blocket");
  }

  if (mbar) {
    mbar.addEventListener("click", function (event) {
      var button = event.target.closest("[data-pb-m]");
      if (!button || button.disabled) {
        return;
      }
      if (button.getAttribute("aria-disabled") === "true") {
        nudgeMhint();
        return;
      }
      var name = button.getAttribute("data-pb-m");
      var id = state.selectedId;
      if (name === "variant" && id) {
        openVariants(id, button);
      } else if (name === "versions" && id) {
        openVersions(id, button);
      } else if (name === "ai") {
        openPanel("ai", { blockId: id, opener: button });
      } else if (name === "move" && id) {
        openMove(id, button);
      } else if (name === "add") {
        openLibrary(defaultGap(""), button);
      }
    });
    updateMbar();
    updateMhint();
  }

  // -------------------------------------------------------------------------
  // Panelerna: AI, konverteringskollen och bildväljaren
  // -------------------------------------------------------------------------

  var panel = $("#pb-panel");
  var panelTitle = $("#pb-panel-title");
  var panelOpener = null;
  var panelBack = null;

  function panelEl(name) {
    return document.getElementById("pb-panel-" + name);
  }

  function syncPanelMode() {
    if (!panel) {
      return;
    }
    var sheet = isMobile() && !panel.hidden;
    panel.classList.toggle("is-sheet", isMobile());
    if (sheet && !panelBack) {
      panelBack = h("div", { class: "pb-sheet-back" });
      panelBack.addEventListener("click", closePanel);
      document.body.appendChild(panelBack);
    } else if (!sheet && panelBack) {
      panelBack.remove();
      panelBack = null;
    }
  }

  function openPanel(name, opts) {
    opts = opts || {};
    if (!panel) {
      return;
    }
    var wrap = panel.querySelector('[data-pb-panel="' + name + '"]');
    if (!wrap) {
      return;
    }
    closeSurface(false);
    var wasOpen = state.panel;
    if (wasOpen && wasOpen !== name) {
      emit("panel", { name: wasOpen, open: false, blockId: state.panelBlock });
    }
    $all("[data-pb-panel]", panel).forEach(function (w) {
      w.hidden = w !== wrap;
    });
    panel.hidden = false;
    state.panel = name;
    state.panelBlock = opts.blockId !== undefined ? opts.blockId : state.selectedId;
    panelOpener = opts.opener || document.activeElement;
    var title = wrap.getAttribute("data-pb-title") || "";
    if (name === "ai" && state.panelBlock && findBlock(state.panelBlock)) {
      title = "AI: " + typeOf(findBlock(state.panelBlock)).name;
    }
    panelTitle.textContent = opts.title || title;
    if (body) {
      body.classList.add("has-panel");
    }
    $all("[data-pb-open]", root).forEach(function (button) {
      button.setAttribute("aria-expanded", String(button.getAttribute("data-pb-open") === name));
    });
    panel.setAttribute("data-pb-current", name);
    syncPanelMode();
    scheduleLayout();
    if (state.panelBlock) {
      keepInView(state.panelBlock, opts.path || null);
    }
    emit("panel", { name: name, open: true, blockId: state.panelBlock });
    panelTitle.focus({ preventScroll: true });
  }

  function closePanel() {
    if (!panel || panel.hidden) {
      return;
    }
    var name = state.panel;
    panel.hidden = true;
    panel.removeAttribute("data-pb-current");
    root.style.paddingBottom = "";
    state.panel = null;
    if (body) {
      body.classList.remove("has-panel");
    }
    $all("[data-pb-open]", root).forEach(function (button) {
      button.setAttribute("aria-expanded", "false");
    });
    syncPanelMode();
    scheduleLayout();
    emit("panel", { name: name, open: false, blockId: state.panelBlock });
    if (panelOpener && panelOpener.isConnected && panelOpener.focus) {
      panelOpener.focus({ preventScroll: true });
    }
    panelOpener = null;
  }

  if (panel) {
    $all("[data-pb-close-panel]", panel).forEach(function (button) {
      button.addEventListener("click", closePanel);
    });
    $all("[data-pb-open]", root).forEach(function (button) {
      button.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-controls", "pb-panel");
      button.addEventListener("click", function () {
        var name = button.getAttribute("data-pb-open");
        if (state.panel === name) {
          closePanel();
        } else {
          openPanel(name, { opener: button });
        }
      });
    });
  }

  function badge(name, text) {
    var el = document.getElementById("pb-" + name + "-badge");
    if (!el) {
      return;
    }
    el.textContent = text || "";
    el.hidden = !text;
  }

  // Bildväljaren (mediaarkivets adresser).

  var media = { target: null, assets: null, info: null, loading: false, error: "" };

  function openMedia(blockId, key, opener) {
    var block = findBlock(blockId);
    var type = typeOf(block);
    var spec = type ? findField(type, key) : null;
    if (!spec || spec.kind !== "media" || readOnly) {
      return;
    }
    media.target = { blockId: blockId, key: key };
    /* "Bild: före", "Bild: efter"; ett fält som redan heter Bild: "Bild". */
    var title = lower(spec.label) === "bild" ? "Bild" : "Bild: " + lower(spec.label);
    openPanel("media", { blockId: blockId, opener: opener, title: title, path: key });
    renderMedia();
    loadMedia();
  }

  function loadMedia() {
    if (!urls.media_json || media.loading) {
      return;
    }
    media.loading = true;
    fetch(urls.media_json, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.json();
      })
      .then(function (data) {
        media.assets = data.assets || [];
        media.info = data;
        media.error = "";
      })
      .catch(function () {
        media.error = "Bilderna gick inte att hämta. Försök igen om en stund.";
      })
      .then(function () {
        media.loading = false;
        renderMedia();
      });
  }

  function currentMediaId() {
    if (!media.target) {
      return null;
    }
    var block = findBlock(media.target.blockId);
    var value = block ? activeFields(block)[media.target.key] : null;
    return value === undefined ? null : value;
  }

  function chooseMedia(assetId) {
    var target = media.target;
    var block = target ? findBlock(target.blockId) : null;
    if (!block) {
      return;
    }
    var version = ownVersion(block);
    version.fields[target.key] = assetId;
    version.at = nowIso();
    changed("media", { blockId: block.id, render: true });
    renderMedia();
    if (assetId === null) {
      toast("Bilden är borttagen från blocket. Den finns kvar i mediaarkivet.");
    } else {
      announce("Bilden är vald.");
      if (isMobile()) {
        closePanel();
      }
    }
  }

  function uploadMedia(files) {
    if (!urls.media_upload || !files || !files.length) {
      return;
    }
    var form = new FormData();
    Array.prototype.forEach.call(files, function (file) {
      form.append("file", file);
    });
    media.error = "";
    media.uploading = true;
    renderMedia();
    fetch(urls.media_upload, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrf, Accept: "application/json" },
      body: form,
    })
      .then(function (response) {
        return response.json().catch(function () {
          return {};
        }).then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (res) {
        media.uploading = false;
        if (!res.ok) {
          media.error = res.data.error || "Bilden gick inte att ladda upp.";
          renderMedia();
          return;
        }
        var added = res.data.assets || [];
        media.assets = added.concat(media.assets || []);
        if (media.info) {
          media.info.count = (media.info.count || 0) + added.length;
        }
        if (res.data.errors && res.data.errors.length) {
          media.error = res.data.errors.join(" ");
        }
        if (added.length === 1) {
          chooseMedia(added[0].id);
        } else {
          renderMedia();
        }
      })
      .catch(function () {
        media.uploading = false;
        media.error = "Ingen kontakt med servern. Försök igen.";
        renderMedia();
      });
  }

  function renderMedia() {
    var el = panelEl("media");
    if (!el || !media.target) {
      return;
    }
    el.innerHTML = "";
    var current = currentMediaId();
    var info = media.info || {};
    if (!urls.media_json) {
      el.appendChild(h("p", { class: "pb-panel__text", text: "Mediaarkivet är inte på plats än." }));
    } else {
      var canUpload = !!urls.media_upload && info.can_upload !== false;
      if (canUpload) {
        var input = h("input", { type: "file", class: "fl-sr", id: "pb-media-file", accept: "image/jpeg,image/png,image/webp,image/gif", multiple: true });
        input.addEventListener("change", function () {
          uploadMedia(input.files);
          input.value = "";
        });
        var drop = h(
          "label",
          { class: "pb-drop-zone" + (media.uploading ? " is-busy" : ""), for: "pb-media-file" },
          icon("pb-u-upload"),
          h("span", { class: "pb-drop-zone__title", text: media.uploading ? "Laddar upp" : "Ladda upp bilder" }),
          h("span", { class: "pb-drop-zone__text", text: isMobile() ? "JPG, PNG, WebP eller GIF." : "Klicka eller släpp bilder här. JPG, PNG, WebP eller GIF." })
        );
        drop.addEventListener("dragover", function (event) {
          event.preventDefault();
          drop.classList.add("is-over");
        });
        drop.addEventListener("dragleave", function () {
          drop.classList.remove("is-over");
        });
        drop.addEventListener("drop", function (event) {
          event.preventDefault();
          drop.classList.remove("is-over");
          uploadMedia(event.dataTransfer && event.dataTransfer.files);
        });
        el.appendChild(input);
        el.appendChild(drop);
      } else if (info.limit) {
        el.appendChild(h("p", { class: "pb-panel__text", text: "Arkivet är fullt (" + info.limit + " bilder). Ta bort bilder i mediaarkivet för att ladda upp fler." }));
      }
      if (media.error) {
        el.appendChild(h("p", { class: "pb-panel__error", role: "alert", text: media.error }));
      }
      if (media.assets === null) {
        el.appendChild(h("p", { class: "pb-panel__text", text: "Hämtar bilderna" }));
      } else if (!media.assets.length) {
        el.appendChild(h("p", { class: "pb-panel__text", text: "Inga bilder i arkivet än." }));
      } else {
        var grid = h("ul", { class: "pb-media" });
        media.assets.forEach(function (asset) {
          var chosen = asset.id === current;
          var button = h(
            "button",
            { type: "button", class: "pb-media__item" + (chosen ? " is-on" : "") + (asset.is_logo ? " is-logo" : ""), "aria-pressed": String(chosen) },
            h("img", { src: asset.thumb || asset.url, alt: "", loading: "lazy", width: "160", height: "120" }),
            h("span", { class: "pb-media__alt", text: asset.alt || (asset.is_logo ? "Logotyp" : "Bild utan beskrivning") }),
            chosen ? h("span", { class: "pb-media__check" }, icon("pb-u-check")) : null
          );
          button.setAttribute("aria-label", (chosen ? "Vald: " : "Välj ") + (asset.alt || (asset.is_logo ? "logotypen" : "bilden")));
          button.addEventListener("click", function () {
            chooseMedia(asset.id);
          });
          grid.appendChild(h("li", null, button));
        });
        el.appendChild(grid);
        if (info.limit) {
          el.appendChild(h("p", { class: "pb-panel__note", text: (info.count || media.assets.length) + " av " + info.limit + " bilder i arkivet." }));
        }
      }
    }
    var actions = h("div", { class: "pb-panel__actions" });
    if (current !== null) {
      var clear = h("button", { type: "button", class: "pb-btn pb-btn--ghost pb-btn--sm" }, icon("pb-u-trash"), h("span", { text: "Ta bort bild" }));
      clear.addEventListener("click", function () {
        chooseMedia(null);
      });
      actions.appendChild(clear);
    }
    if (urls.media) {
      actions.appendChild(h("a", { class: "pb-link", href: urls.media, text: "Öppna mediaarkivet" }));
    }
    if (actions.childNodes.length) {
      el.appendChild(actions);
    }
  }

  // -------------------------------------------------------------------------
  // Kontrollerna och publiceringen
  // -------------------------------------------------------------------------

  function markProblemFields(scope) {
    if (!fdoc) {
      return;
    }
    $all(".pb-has-problem", scope || fdoc).forEach(function (el) {
      el.classList.remove("pb-has-problem");
      el.removeAttribute("title");
    });
    state.problems.forEach(function (problem) {
      if (!problem.block || !problem.part) {
        return;
      }
      var node = scope && scope.getAttribute && scope.getAttribute("data-pb-block") ? (scope.getAttribute("data-pb-block") === problem.block ? scope : null) : blockNode(problem.block);
      if (!node) {
        return;
      }
      problemFields(node, problem).forEach(function (el) {
        el.classList.add("pb-has-problem");
        el.setAttribute("title", problem.message);
      });
    });
  }

  function problemFields(node, problem) {
    var path = problem.part + (problem.index !== null && problem.index !== undefined ? "." + problem.index : "");
    var exact = $all('[data-pb-field="' + path + '"]', node);
    if (exact.length) {
      return exact;
    }
    return $all('[data-pb-field^="' + path + '."]', node);
  }

  function setProblems(list) {
    state.problems = Array.isArray(list) ? list : [];
    var count = state.problems.length;
    if (problemsBtn) {
      problemsBtn.hidden = !count;
      if (problemsCount) {
        problemsCount.textContent = count === 1 ? "1 att rätta" : count + " att rätta";
      }
      problemsBtn.setAttribute("aria-label", count === 1 ? "1 sak att rätta innan sidan kan publiceras" : count + " saker att rätta innan sidan kan publiceras");
    }
    var dot = $("#pb-more-dot");
    if (dot) {
      dot.hidden = !count;
    }
    markProblemFields();
    drawOverlay();
    if (surface && surface.name === "more") {
      renderMore(surface.body);
    }
    if (surface && surface.name === "problems") {
      if (!count) {
        closeSurface(false);
        toast("Allt är rättat. Sidan går att publicera.");
      } else {
        renderProblems(surface.body);
      }
    }
  }

  function renderProblems(content) {
    content.innerHTML = "";
    var list = h("ul", { class: "pb-problems" });
    state.problems.forEach(function (problem) {
      var button = h(
        "button",
        { type: "button", class: "pb-problem" },
        icon("pb-u-warn"),
        h("span", { class: "pb-problem__text" }, problem.where ? h("b", { text: problem.where + ": " }) : null, problem.message)
      );
      button.addEventListener("click", function () {
        goToProblem(problem);
      });
      list.appendChild(h("li", null, button));
    });
    content.appendChild(list);
    content.appendChild(h("p", { class: "pb-surface__note", text: "Kontrollerna är samma som för annonserna: bara det du bekräftat under Företaget, inga löften om tider." }));
    var first = list.querySelector("button");
    if (first) {
      first.setAttribute("data-pb-autofocus", "");
    }
  }

  function openProblems(anchor) {
    if (!state.problems.length) {
      return;
    }
    openSurface({
      title: "Rätta det här innan du publicerar",
      anchor: anchor,
      name: "problems",
      wide: true,
      build: function (content) {
        renderProblems(content);
      },
    });
  }

  function goToProblem(problem) {
    closeSurface(false);
    if (!problem.block || !findBlock(problem.block)) {
      return;
    }
    var block = findBlock(problem.block);
    var spec = problem.part ? findField(typeOf(block), problem.part) : null;
    if (spec && spec.kind === "media") {
      select(problem.block);
      openMedia(problem.block, spec.key);
      return;
    }
    select(problem.block, { scroll: true });
    window.setTimeout(function () {
      var node = blockNode(problem.block);
      if (!node || !spec) {
        focusToolbar();
        return;
      }
      if (spec.kind === "media") {
        openMedia(problem.block, spec.key);
        return;
      }
      var fields = problemFields(node, problem).filter(function (el) {
        return el.isContentEditable;
      });
      if (fields.length) {
        fields[0].focus({ preventScroll: true });
        placeCaretAtEnd(fields[0]);
      } else if (spec.kind === "lines" || spec.kind === "items") {
        openList(problem.block, spec.key, null);
      } else {
        focusToolbar();
      }
    }, 350);
  }

  if (problemsBtn) {
    problemsBtn.addEventListener("click", function () {
      if (surface && surface.name === "problems") {
        closeSurface(true);
      } else {
        openProblems(problemsBtn);
      }
    });
  }

  function publish() {
    if (publishing || stale || !publishBtn) {
      return;
    }
    publishing = true;
    updatePublish();
    flush()
      .then(function () {
        // rev: servern publicerar bara det utkast som den här fliken sparade.
        return post(urls.publish, { rev: state.rev });
      })
      .then(function (res) {
        publishing = false;
        if (res.status === 409) {
          showStale();
          return;
        }
        if (res.ok) {
          state.published = true;
          state.changes = dirty;
          if (res.data.state) {
            state.live = res.data.state.live || state.live;
          }
          setProblems([]);
          updatePublish();
          toast(res.data.message || "Publicerad.");
          return;
        }
        updatePublish();
        if (res.data.problems && res.data.problems.length) {
          setProblems(res.data.problems);
          openProblems(problemsBtn && !problemsBtn.hidden ? problemsBtn : publishBtn);
          toast("Inget publicerades. Rätta det som står i listan först.");
        } else {
          toast(res.data.error || "Inget publicerades. Försök igen.");
        }
      })
      .catch(function () {
        publishing = false;
        updatePublish();
        if (!stale) {
          toast("Sidan gick inte att spara, så inget publicerades. Försök igen.");
        }
      });
  }

  if (publishBtn) {
    publishBtn.addEventListener("click", publish);
  }

  // -------------------------------------------------------------------------
  // "Mer" i mobilen: färgen, konverteringskollen, kontrollerna och notiserna
  // -------------------------------------------------------------------------

  function renderMore(content) {
    content.innerHTML = "";
    var info = stateText();
    var usage = $("#pb-usage");
    content.appendChild(
      h("p", { class: "pb-surface__note", text: info.label + ". " + (usage ? usage.textContent : "") + "." })
    );
    var notes = $all(".pb-note", root);
    if (notes.length) {
      var box = h("div", { class: "pb-more__notes" });
      notes.forEach(function (note) {
        box.appendChild(note.cloneNode(true));
      });
      content.appendChild(box);
    }
    var swatches = h("div", { class: "pb-more__swatches", role: "group", "aria-label": "Färg" });
    $all('input[name="pb-palette"]', root).forEach(function (input) {
      var palette = (config.palettes || []).filter(function (p) {
        return p.key === input.value;
      })[0] || { label: input.value, color: "" };
      var button = h(
        "button",
        {
          type: "button",
          class: "pb-more__swatch",
          "aria-pressed": String(input.checked),
          disabled: input.disabled ? true : null,
          title: palette.reason || null,
        },
        h("i", { "aria-hidden": "true" }),
        h("span", { text: palette.label })
      );
      var dot = button.querySelector("i");
      var source = input.parentNode.querySelector(".pb-swatch__dot");
      if (source) {
        var computed = window.getComputedStyle(source);
        dot.style.backgroundColor = computed.backgroundColor;
        dot.style.backgroundImage = computed.backgroundImage;
      } else {
        dot.style.backgroundColor = palette.color;
      }
      button.addEventListener("click", function () {
        if (input.disabled || input.checked) {
          return;
        }
        input.checked = true;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        $all(".pb-more__swatch", swatches).forEach(function (b) {
          b.setAttribute("aria-pressed", String(b === button));
        });
      });
      swatches.appendChild(button);
    });
    content.appendChild(h("div", null, h("p", { class: "pb-more__label", text: "Färg på sidan" }), swatches));
    var buttons = h("div", { class: "pb-more__buttons" });
    var kollBadge = $("#pb-koll-badge");
    var koll = h(
      "button",
      { type: "button", class: "pb-btn pb-btn--ghost" },
      icon("pb-u-gauge"),
      h("span", { text: "Konverteringskoll" + (kollBadge && !kollBadge.hidden && kollBadge.textContent ? ", " + kollBadge.textContent : "") })
    );
    koll.addEventListener("click", function () {
      openPanel("koll", { opener: $("#pb-more-btn") });
    });
    buttons.appendChild(koll);
    if (state.problems.length) {
      var count = state.problems.length;
      var fix = h(
        "button",
        { type: "button", class: "pb-btn pb-btn--warn" },
        icon("pb-u-warn"),
        h("span", { text: count === 1 ? "1 sak att rätta innan publicering" : count + " saker att rätta innan publicering" })
      );
      fix.addEventListener("click", function () {
        openProblems($("#pb-more-btn"));
      });
      buttons.appendChild(fix);
    }
    content.appendChild(buttons);
  }

  var moreBtn = $("#pb-more-btn");
  if (moreBtn) {
    moreBtn.addEventListener("click", function () {
      openSurface({
        title: state.name,
        anchor: moreBtn,
        name: "more",
        wide: true,
        build: function (content) {
          renderMore(content);
        },
      });
    });
  }

  // -------------------------------------------------------------------------
  // Namnet och färgen
  // -------------------------------------------------------------------------

  var nameInput = $("#pb-name");
  var nameError = $("#pb-name-error");

  function saveName() {
    if (!nameInput || readOnly) {
      return;
    }
    var value = plain(nameInput.value).replace(/\s+/g, " ").trim();
    if (value === state.name) {
      nameInput.value = state.name;
      return;
    }
    if (!value) {
      showNameError("Skriv ett namn på sidan.");
      nameInput.value = state.name;
      return;
    }
    post(urls.settings, { name: value }).then(function (res) {
      if (res.ok) {
        state.name = res.data.name;
        nameInput.value = state.name;
        showNameError("");
        document.title = state.name + " - ADX Flamingo";
        setSaved("saved", "Sparat " + (res.data.saved_text || clock(new Date())));
      } else {
        showNameError(res.data.error || "Namnet gick inte att spara.");
        nameInput.value = state.name;
      }
    });
  }

  function showNameError(text) {
    if (!nameError) {
      return;
    }
    nameError.textContent = text;
    nameError.hidden = !text;
  }

  if (nameInput) {
    nameInput.addEventListener("change", saveName);
    nameInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        nameInput.blur();
      } else if (event.key === "Escape") {
        nameInput.value = state.name;
        nameInput.blur();
      }
    });
  }

  (config.palettes || []).forEach(function (palette) {
    if (palette.key === "logo" && palette.color) {
      var swatch = $(".pb-swatch--logo", root);
      if (swatch) {
        swatch.style.setProperty("--sw", palette.color);
      }
    }
  });

  $all('input[name="pb-palette"]', root).forEach(function (input) {
    input.addEventListener("change", function () {
      if (!input.checked) {
        return;
      }
      var previous = state.palette;
      state.palette = input.value;
      post(urls.settings, { palette: input.value }).then(function (res) {
        if (res.ok) {
          applyPaletteStyle(res.data.palette_style);
          setSaved("saved", "Sparat " + (res.data.saved_text || clock(new Date())));
          announce("Färgen är " + lower(res.data.palette_label) + ".");
          emit("change", { reason: "palette", blockId: null, blocks: state.blocks });
        } else {
          state.palette = previous;
          var back = $('input[name="pb-palette"][value="' + previous + '"]', root);
          if (back) {
            back.checked = true;
          }
          toast(res.data.error || "Färgen gick inte att byta.");
        }
      });
    });
  });

  // -------------------------------------------------------------------------
  // Meddelanden
  // -------------------------------------------------------------------------

  var toastEl = $("#pb-toast");
  var toastTimer = null;

  function toast(text, action) {
    if (!toastEl) {
      return;
    }
    window.clearTimeout(toastTimer);
    toastEl.innerHTML = "";
    toastEl.appendChild(h("span", { class: "pb-toast__text", text: text }));
    if (action) {
      var button = h("button", { type: "button", class: "pb-toast__btn", text: action.label });
      button.addEventListener("click", function () {
        hideToast();
        action.run();
      });
      toastEl.appendChild(button);
    }
    toastEl.classList.add("is-on");
    toastTimer = window.setTimeout(hideToast, action ? 10000 : 5000);
  }

  function hideToast() {
    if (toastEl) {
      toastEl.classList.remove("is-on");
      toastEl.innerHTML = "";
    }
  }

  // -------------------------------------------------------------------------
  // Start
  // -------------------------------------------------------------------------

  buildToolbar();
  updatePublish();
  setProblems(state.problems);
  if (frame) {
    frame.addEventListener("load", onFrameReady);
    onFrameReady();
  }
  layout();

  // -------------------------------------------------------------------------
  // Det publika gränssnittet
  // -------------------------------------------------------------------------

  function apiAddBlock(typeKey, variant, opts) {
    opts = opts || {};
    var gap = opts.afterId && indexOf(opts.afterId) >= 0 ? gapAfter(indexOf(opts.afterId)) : defaultGap(typeKey);
    return addBlockAt(typeKey, gap, { variant: variant, fields: opts.fields, source: opts.source }).then(function (block) {
      return clone(block);
    });
  }

  /* En ändring från ett annat skript: sparas direkt, och tas tillbaka om
     servern säger nej. */
  function commitFromApi(snapshot, reason, blockId) {
    changed(reason, { blockId: blockId, render: true });
    return flush().then(
      function (data) {
        return data;
      },
      function (res) {
        if (res && res.status === 400) {
          state.blocks = snapshot;
          changed("undo", { blockId: blockId, render: true });
        }
        throw new Error((res && res.data && res.data.error) || "Ändringen gick inte att spara.");
      }
    );
  }

  function apiAddVersion(blockId, fields, source) {
    var block = findBlock(blockId);
    if (!block) {
      return Promise.reject(new Error("Blocket finns inte på sidan."));
    }
    var snapshot = clone(state.blocks);
    var base = clone(activeFields(block));
    var merged = Object.assign(base, clone(fields || {}));
    var version = newVersion(block, merged, source);
    return commitFromApi(snapshot, "version", blockId).then(function () {
      return clone(version);
    });
  }

  function apiSetBlocks(blocks, opts) {
    if (!Array.isArray(blocks)) {
      return Promise.reject(new Error("Blocken ska vara en lista."));
    }
    var snapshot = clone(state.blocks);
    state.blocks = clone(blocks);
    select(null);
    return commitFromApi(snapshot, "replace", null).then(function (data) {
      toast("Sidan är utbytt" + (opts && opts.source === "ai" ? " mot AI:s förslag" : "") + ".", {
        label: "Ångra",
        run: function () {
          state.blocks = snapshot;
          changed("undo", { render: true });
        },
      });
      return data;
    });
  }

  window.FlamingoPB = {
    version: 1,
    state: function () {
      return {
        pageId: state.pageId,
        rev: state.rev,
        blocks: clone(state.blocks),
        palette: state.palette,
        selectedId: state.selectedId,
      };
    },
    on: on,
    select: function (blockId) {
      select(blockId || null, { scroll: true });
    },
    addBlock: apiAddBlock,
    addVersion: apiAddVersion,
    setBlocks: apiSetBlocks,
    openPanel: openPanel,
    closePanel: closePanel,
    panelEl: panelEl,
    badge: badge,
    schema: function () {
      return clone(config.schema || []);
    },
    urls: urls,
  };
})();
