/**
 * DEN ENDA länkväljaren för /manage/ - porterad från Atlas Holly (adx).
 *
 * `ADXLinkModal.open({ onSelect, current })` öppnar modalen med två flikar:
 * välj något på sajten (sökbart på namn), eller skriv en webbadress
 * (intern rutt eller extern URL, kontrollerad server-side med
 * uppgraderingsförslag). `onSelect` får { link, label, href, status }.
 *
 * Den kopplar också varje `[data-lnkf]`-fält (renderat av `{% link_field %}`):
 * den dolda inputen bär beskrivaren som JSON, pillen visar vart länken går.
 */
(function () {
  if (window.ADXLinkModal) return;

  var modal = null;
  var state = null;
  var optionsCache = null;

  function $(sel) { return modal.querySelector(sel); }
  function $$(sel) { return modal.querySelectorAll(sel); }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    var inp = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return inp ? inp.value : '';
  }

  function resetState() {
    state = {
      onSelect: null,
      chosen: null,   // { link, label, href, status }
      search: '',
      checkTimer: null,
      expanded: {},   // gruppnamn -> användaren har själv fällt ut/ihop
    };
  }

  function showPane(name) {
    $$('[data-lnkm-pane]').forEach(function (p) {
      p.hidden = p.getAttribute('data-lnkm-pane') !== name;
    });
    $$('[data-lnkm-tab]').forEach(function (t) {
      t.classList.toggle('is-active', t.getAttribute('data-lnkm-tab') === name);
    });
    setError('');
  }

  function setError(msg) { $('[data-lnkm-error]').textContent = msg || ''; }
  function setUseEnabled() { $('[data-lnkm-use]').disabled = !state.chosen; }

  // --- Fliken "På sajten" ---------------------------------------------------

  function loadOptions() {
    if (optionsCache) { renderList(); return; }
    fetch(modal.dataset.optionsUrl, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) { optionsCache = d.options || []; renderList(); })
      .catch(function () { setError(modal.dataset.i18nLoadFailed); });
  }

  function canon(link) {
    // Nyckelordningen i lagrade beskrivare varierar ({"id":11,"kind":"page"}
    // vs {"kind":"page","id":11}) - jämför sorterat, inte stringify rakt av.
    if (!link || typeof link !== 'object') return JSON.stringify(link);
    var out = {};
    Object.keys(link).sort().forEach(function (k) { out[k] = link[k]; });
    return JSON.stringify(out);
  }

  function isChosen(opt) {
    return state.chosen && canon(state.chosen.link) === canon(opt.link);
  }

  function renderList() {
    var list = $('[data-lnkm-list]');
    var q = state.search.toLowerCase();
    list.innerHTML = '';
    var shown = 0;

    // Sortera alternativen per grupp, i först-sedd-ordning.
    var groups = [];
    var byName = {};
    (optionsCache || []).forEach(function (opt) {
      var hay = (opt.label + ' ' + opt.group + ' ' + (opt.note || '')).toLowerCase();
      if (q && hay.indexOf(q) === -1) return;
      shown++;
      if (!byName[opt.group]) {
        byName[opt.group] = [];
        groups.push(opt.group);
      }
      byName[opt.group].push(opt);
    });

    groups.forEach(function (name) {
      var opts = byName[name];
      // Stora grupper startar ihopfällda så listan går att skanna; sökning
      // eller ett val inuti fäller ut dem. Användarens egna klick vinner.
      var open = opts.length <= 3 || opts.some(isChosen);
      if (name in state.expanded) open = state.expanded[name];
      if (q) open = true; // vid sökning: visa alltid träffarna

      var head = document.createElement('button');
      head.type = 'button';
      head.className = 'lnkm__group lnkm__group--toggle';
      head.textContent = (open ? '▾ ' : '▸ ') + name + ' (' + opts.length + ')';
      head.addEventListener('click', function () {
        state.expanded[name] = !open;
        renderList();
      });
      list.appendChild(head);
      if (!open) return;

      opts.forEach(function (opt) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'lnkm__opt';
        if (isChosen(opt)) btn.classList.add('is-selected');
        var label = document.createElement('span');
        label.textContent = opt.label;
        btn.appendChild(label);
        var note = document.createElement('span');
        note.className = 'lnkm__opt-note' + (opt.status !== 'ok' && opt.status !== 'external' ? ' lnkm__opt-note--warn' : '');
        note.textContent = opt.note || opt.href;
        btn.appendChild(note);
        btn.addEventListener('click', function () {
          state.chosen = { link: opt.link, label: opt.label, href: opt.href, status: opt.status };
          setUseEnabled();
          renderList();
        });
        list.appendChild(btn);
      });
    });
    $('[data-lnkm-empty]').hidden = shown > 0;
  }

  // --- Fliken "Webbadress" --------------------------------------------------

  function checkAddress() {
    var input = $('[data-lnkm-addr]');
    var out = $('[data-lnkm-check]');
    var raw = input.value.trim();
    state.chosen = null;
    setUseEnabled();
    if (!raw) { out.textContent = ''; return; }
    out.className = 'lnkm__check';
    out.textContent = modal.dataset.i18nChecking;
    fetch(modal.dataset.checkUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify({ href: raw }),
    })
      .then(function (r) { return r.json(); })
      .then(function (d) { renderCheck(raw, d, out); })
      .catch(function () { out.textContent = ''; });
  }

  function renderCheck(raw, d, out) {
    out.innerHTML = '';
    var msg = document.createElement('span');

    if (d.suggestion) {
      // Servern kände igen inmatningen som något bättre: en adress som
      // matchar en sak på sajten - direktlänken överlever adressbyten.
      out.className = 'lnkm__check lnkm__check--warn';
      var isExternal = d.suggestion.kind === 'external';
      msg.textContent = isExternal
        ? modal.dataset.i18nDidYouMean + ' ' + d.suggestion.url + '?'
        : modal.dataset.i18nLinkDirectly.replace('%s', d.label || d.href);
      out.appendChild(msg);
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'lnkm__suggest';
      btn.textContent = isExternal ? d.suggestion.url : (d.label || d.href);
      btn.addEventListener('click', function () {
        state.chosen = { link: d.suggestion, label: d.label || d.suggestion.url || d.href, href: d.href, status: d.status };
        out.className = 'lnkm__check lnkm__check--ok';
        out.textContent = '✓ ' + (state.chosen.label);
        setUseEnabled();
      });
      out.appendChild(btn);
      // Den ordagranna inmatningen funkar också om den resolvade - tillåt den.
      if (d.ok) {
        state.chosen = { link: d.link, label: raw, href: d.href, status: d.status };
        setUseEnabled();
      }
      return;
    }

    if (d.ok) {
      out.className = 'lnkm__check lnkm__check--ok';
      msg.textContent = '✓ ' + (d.label || d.href);
      state.chosen = { link: d.link, label: d.label || raw, href: d.href, status: d.status };
    } else {
      out.className = 'lnkm__check lnkm__check--bad';
      msg.textContent = d.note || '';
    }
    out.appendChild(msg);
    setUseEnabled();
  }

  // --- Modalens rörmokeri ---------------------------------------------------

  function open(opts) {
    modal = document.getElementById('adx-link-modal');
    if (!modal) return;
    resetState();
    state.onSelect = (opts && opts.onSelect) || null;
    if (opts && opts.current && opts.current.link) {
      state.chosen = opts.current;
    }
    $('[data-lnkm-search]').value = '';
    $('[data-lnkm-addr]').value = '';
    $('[data-lnkm-check]').textContent = '';
    state.search = '';
    modal.classList.add('is-open');
    showPane('site');
    loadOptions();
    setUseEnabled();
    $('[data-lnkm-search]').focus();
  }

  function close() {
    if (modal) modal.classList.remove('is-open');
    optionsCache = null; // sajten kan ha ändrats; hämta om nästa gång
  }

  function wire() {
    modal = document.getElementById('adx-link-modal');
    if (!modal) return;

    $('[data-lnkm-close]').addEventListener('click', close);
    modal.addEventListener('click', function (e) { if (e.target === modal) close(); });

    $$('[data-lnkm-tab]').forEach(function (tab) {
      tab.addEventListener('click', function () {
        showPane(tab.getAttribute('data-lnkm-tab'));
        if (tab.getAttribute('data-lnkm-tab') === 'address') $('[data-lnkm-addr]').focus();
      });
    });

    $('[data-lnkm-search]').addEventListener('input', function (e) {
      state.search = e.target.value;
      renderList();
    });

    $('[data-lnkm-addr]').addEventListener('input', function () {
      clearTimeout(state.checkTimer);
      state.checkTimer = setTimeout(checkAddress, 350);
    });

    $('[data-lnkm-use]').addEventListener('click', function () {
      if (state.chosen && state.onSelect) state.onSelect(state.chosen);
      close();
    });
  }

  // --- Fältkomponenterna ----------------------------------------------------

  var idSeq = 0;

  function wireField(field) {
    if (field.dataset.lnkfWired) return;
    field.dataset.lnkfWired = '1';
    var input = field.querySelector('[data-lnkf-input]');
    // Rader klonas ur <template> - klonen ärver mallens input-id. Ge varje
    // kopia ett eget så dokumentet aldrig bär dubbla id:n.
    if (input.id && document.querySelectorAll('[id="' + input.id + '"]').length > 1) {
      input.id = input.id + '_c' + (++idSeq);
    }
    var pill = field.querySelector('[data-lnkf-label]');
    var openBtn = field.querySelector('[data-lnkf-open]');
    var clearBtn = field.querySelector('[data-lnkf-clear]');

    function update(chosen) {
      if (chosen && chosen.link && Object.keys(chosen.link).length) {
        input.value = JSON.stringify(chosen.link);
        pill.textContent = chosen.label || chosen.href || '';
        pill.classList.remove('lnkf__label--empty');
        openBtn.textContent = field.dataset.labelChange;
        clearBtn.hidden = false;
      } else {
        input.value = '';
        pill.textContent = field.dataset.labelNone;
        pill.classList.add('lnkf__label--empty');
        openBtn.textContent = field.dataset.labelSet;
        clearBtn.hidden = true;
      }
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    openBtn.addEventListener('click', function () {
      var current = null;
      try {
        var parsed = JSON.parse(input.value || 'null');
        if (parsed) current = { link: parsed, label: pill.textContent };
      } catch (e) { /* legacy-sträng; modalen startar tom */ }
      open({ current: current, onSelect: update });
    });
    clearBtn.addEventListener('click', function () { update(null); });
  }

  function wireAll() {
    document.querySelectorAll('[data-lnkf]').forEach(wireField);
  }

  function init() {
    wire();
    wireAll();
    // Nya listrader klonas ur <template> av manage-block-rows.js - en
    // MutationObserver fångar dem (motsvarar adx:s htmx:afterSwap-hook).
    new MutationObserver(wireAll).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  function resetField(field) {
    if (!field) return;
    var input = field.querySelector('[data-lnkf-input]');
    var pill = field.querySelector('[data-lnkf-label]');
    input.value = '';
    pill.textContent = field.dataset.labelNone;
    pill.classList.add('lnkf__label--empty');
    field.querySelector('[data-lnkf-open]').textContent = field.dataset.labelSet;
    field.querySelector('[data-lnkf-clear]').hidden = true;
  }

  window.ADXLinkModal = {
    open: open,
    close: close,
    wireField: wireField,
    resetField: resetField,
  };
})();
