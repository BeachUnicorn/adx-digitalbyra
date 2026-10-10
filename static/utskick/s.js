/* ADX Flamingo (README E.6): besöket från en spårad länk. Gör något bara
   när adressen bär adx=, läser den en gång och tar bort den. Sparar inget
   i webbläsaren. */
(function (w, d) {
  "use strict";
  var me = d.currentScript;
  var api = w.adxFlamingo = w.adxFlamingo || {};
  if (!api.track) api.track = function () {};
  var q, t;
  try {
    q = new URLSearchParams(w.location.search);
  } catch (e) {
    return;
  }
  if (!q.has("adx")) return;
  t = q.get("adx") || "";
  q.delete("adx");
  var rest = q.toString();
  try {
    w.history.replaceState(w.history.state, "", w.location.pathname + (rest ? "?" + rest : "") + w.location.hash);
  } catch (e) {}
  var k = me && me.getAttribute("data-k");
  var nav = w.navigator;
  if (!k || !/^[A-Za-z0-9]{1,12}\.[A-Za-z0-9]{10}$/.test(t) || !nav.sendBeacon || !me.src) return;
  var url = me.src.split("/").slice(0, 3).join("/") + "/v";
  var shown = 0;
  var since = d.visibilityState === "visible" ? Date.now() : 0;
  var sent = 0;
  function secs() {
    var ms = shown + (since ? Date.now() - since : 0);
    return Math.min(Math.round(ms / 1000), 1800);
  }
  function send(more) {
    var body = { k: k, t: t, p: w.location.pathname.slice(0, 200), s: secs() };
    for (var key in more) body[key] = more[key];
    try {
      nav.sendBeacon(url, JSON.stringify(body));
    } catch (e) {}
  }
  function flush() {
    if (since) {
      shown += Date.now() - since;
      since = 0;
    }
    if (sent < 5) {
      sent += 1;
      send({});
    }
  }
  send({ v: 1 });
  d.addEventListener("visibilitychange", function () {
    if (d.visibilityState === "hidden") flush();
    else if (!since) since = Date.now();
  });
  w.addEventListener("pagehide", flush);
  api.track = function (name) {
    name = String(name || "").toLowerCase();
    if (/^[a-z0-9_-]{1,40}$/.test(name)) send({ e: name });
  };
})(window, document);
