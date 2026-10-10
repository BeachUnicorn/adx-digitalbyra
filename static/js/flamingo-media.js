/* ==========================================================================
   ADX Flamingo - mediaarkivet och omdömena från Google (verktyget)

   Bara förbättringar: utan skript fungerar varje formulär som det är.

   form[data-fl-media-upload]   släpp-ytan och filfältet laddar upp direkt
                                med förlopp (POST till adressen i
                                attributet, fältet "file", CSRF med
                                X-CSRFToken) och laddar om sidan efteråt
   form[data-fl-media-import]   knappen säger hur många bilder som hämtas
   form[data-fl-once]           skickas en gång: varje sökning och hämtning
                                hos Google kostar, så ett dubbelklick eller
                                en otålig Enter blir inte två anrop
   ========================================================================== */
(function () {
  "use strict";

  function csrf(form) {
    var input = form.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  /* ---------- Uppladdningen ---------- */
  function setupUpload(form) {
    var url = form.getAttribute("data-fl-media-upload");
    var input = form.querySelector("input[type=file]");
    var zone = form.querySelector(".fl-md-drop__zone");
    var progress = form.querySelector("[data-fl-media-progress]");
    var bar = progress ? progress.querySelector("progress") : null;
    var status = form.querySelector("[data-fl-media-status]");
    var maxFiles = parseInt(form.getAttribute("data-max-files") || "20", 10);
    var busy = false;
    if (!url || !input || !window.FormData || !window.XMLHttpRequest) return;
    form.classList.add("is-js");

    function say(text) {
      if (progress) progress.hidden = false;
      if (status) status.textContent = text;
    }

    // En fil per anrop: servern tar högst 85 MB per anrop, och 20 bilder på
    // 15 MB är mer än så. Förloppet gäller alla filerna tillsammans.
    function upload(fileList) {
      if (busy || !fileList || !fileList.length) return;
      var files = Array.prototype.slice.call(fileList);
      if (files.length > maxFiles) {
        say("Högst " + maxFiles + " bilder åt gången.");
        return;
      }
      busy = true;
      var total = files.reduce(function (sum, f) {
        return sum + (f.size || 1);
      }, 0);
      var sent = 0;
      var saved = 0;
      var errors = [];
      if (bar) bar.value = 0;
      say("Laddar upp " + plural(files.length, "bild", "bilder") + ".");

      function finish() {
        busy = false;
        input.value = "";
        if (bar) bar.value = 100;
        var done = saved ? plural(saved, "bild", "bilder") + " uppladdade." : "";
        if (!errors.length) {
          say(done);
          window.location.reload();
          return;
        }
        say(
          [done, errors.join(" "), saved ? "Ladda om sidan för att se bilderna." : ""]
            .filter(Boolean)
            .join(" ")
        );
      }

      function next(index) {
        if (index >= files.length) {
          finish();
          return;
        }
        var file = files[index];
        var data = new FormData();
        data.append("file", file);
        var xhr = new XMLHttpRequest();
        xhr.open("POST", url);
        xhr.setRequestHeader("X-CSRFToken", csrf(form));
        xhr.setRequestHeader("Accept", "application/json");
        xhr.upload.addEventListener("progress", function (event) {
          if (event.lengthComputable && bar) {
            var part = (event.loaded / event.total) * (file.size || 1);
            bar.value = Math.min(99, Math.round(((sent + part) / total) * 100));
          }
        });
        xhr.addEventListener("load", function () {
          var body = {};
          try {
            body = JSON.parse(xhr.responseText || "{}");
          } catch (error) {
            body = {};
          }
          if (xhr.status >= 200 && xhr.status < 300 && body.assets) {
            saved += body.assets.length;
            errors = errors.concat(body.errors || []);
          } else if (xhr.status === 413) {
            errors.push(file.name + ": filen är för stor.");
          } else {
            errors.push(body.error || file.name + ": uppladdningen gick inte.");
          }
          sent += file.size || 1;
          say("Uppladdat " + (index + 1) + " av " + files.length + ".");
          next(index + 1);
        });
        xhr.addEventListener("error", function () {
          errors.push(file.name + ": uppladdningen gick inte. Kontrollera anslutningen.");
          sent += file.size || 1;
          next(index + 1);
        });
        xhr.send(data);
      }

      next(0);
    }

    input.addEventListener("change", function () {
      upload(input.files);
    });
    if (zone) {
      ["dragenter", "dragover"].forEach(function (name) {
        zone.addEventListener(name, function (event) {
          event.preventDefault();
          form.classList.add("is-over");
        });
      });
      ["dragleave", "drop"].forEach(function (name) {
        zone.addEventListener(name, function (event) {
          event.preventDefault();
          form.classList.remove("is-over");
        });
      });
      zone.addEventListener("drop", function (event) {
        if (event.dataTransfer) upload(event.dataTransfer.files);
      });
    }
  }

  /* ---------- Bilderna från hemsidan ---------- */
  function setupImport(form) {
    var button = form.querySelector("[data-fl-media-import-btn]");
    if (!button) return;
    function update() {
      var n = form.querySelectorAll("input[name=candidate]:checked").length;
      button.textContent = n
        ? "Hämta " + plural(n, "bild", "bilder") + " till arkivet"
        : "Välj bilder att hämta";
    }
    form.addEventListener("change", update);
    update();
  }

  /* ---------- En gång ---------- */
  function setupOnce(form) {
    form.addEventListener("submit", function (event) {
      if (form.getAttribute("data-fl-sent")) {
        event.preventDefault();
        return;
      }
      form.setAttribute("data-fl-sent", "1");
      form.setAttribute("aria-busy", "true");
      var buttons = Array.prototype.filter.call(
        form.querySelectorAll("button[type=submit]"),
        function (b) {
          return !b.disabled;
        }
      );
      // Efter att formuläret skickats (knappens värde följer med), och
      // tillbaka efter en stund om sidan visas igen från webbläsarens minne.
      window.setTimeout(function () {
        buttons.forEach(function (b) {
          b.disabled = true;
        });
      }, 0);
      window.setTimeout(function () {
        form.removeAttribute("data-fl-sent");
        form.removeAttribute("aria-busy");
        buttons.forEach(function (b) {
          b.disabled = false;
        });
      }, 4000);
    });
  }

  document.querySelectorAll("form[data-fl-media-upload]").forEach(setupUpload);
  document.querySelectorAll("form[data-fl-media-import]").forEach(setupImport);
  document.querySelectorAll("form[data-fl-once]").forEach(setupOnce);
})();
