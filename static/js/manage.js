// /manage/ - top-nav hamburger toggle (mobile).
(function () {
    const hamburger = document.querySelector(".m-nav__hamburger");
    const mobileNav = document.getElementById("m-mobile-nav");
    if (!hamburger || !mobileNav) return;

    hamburger.addEventListener("click", () => {
        const expanded = hamburger.getAttribute("aria-expanded") === "true";
        hamburger.setAttribute("aria-expanded", String(!expanded));
        mobileNav.classList.toggle("is-open");
        mobileNav.setAttribute("aria-hidden", String(expanded));
    });

    // Reset the panel when the viewport grows past the hamburger breakpoint.
    const NAV_BREAKPOINT = 680;
    let resizeRaf = null;
    window.addEventListener("resize", () => {
        if (resizeRaf) return;
        resizeRaf = requestAnimationFrame(() => {
            resizeRaf = null;
            if (window.innerWidth > NAV_BREAKPOINT && mobileNav.classList.contains("is-open")) {
                mobileNav.classList.remove("is-open");
                mobileNav.setAttribute("aria-hidden", "true");
                hamburger.setAttribute("aria-expanded", "false");
            }
        });
    });
})();

// Tabs component - works on any [data-tabs] container.
(function () {
    document.querySelectorAll("[data-tabs]").forEach(function (container) {
        const buttons = container.querySelectorAll("[data-tab]");
        const panels = container.querySelectorAll("[data-tab-panel]");

        buttons.forEach(function (btn) {
            btn.addEventListener("click", function () {
                var target = btn.dataset.tab;
                buttons.forEach(function (b) { b.classList.remove("is-active"); });
                panels.forEach(function (p) { p.classList.remove("is-active"); });
                btn.classList.add("is-active");
                var panel = container.querySelector('[data-tab-panel="' + target + '"]');
                if (panel) panel.classList.add("is-active");
            });
        });
    });
})();

// Optimize popover toggle (media library)
(function () {
    document.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-opt-toggle]");
        if (btn) {
            var wrap = btn.closest(".m-media-card__opt-wrap");
            var pop = wrap && wrap.querySelector("[data-opt-popover]");
            if (pop) {
                var isHidden = pop.hasAttribute("hidden");
                // Close all other popovers first
                document.querySelectorAll("[data-opt-popover]").forEach(function (p) { p.setAttribute("hidden", ""); });
                if (isHidden) pop.removeAttribute("hidden");
            }
            return;
        }
        // Click outside closes all
        if (!e.target.closest(".m-media-card__opt-popover")) {
            document.querySelectorAll("[data-opt-popover]").forEach(function (p) { p.setAttribute("hidden", ""); });
        }
    });
})();

// Focal point picker - any [data-focal-picker] wrapping an <img>.
// Click = "this is the subject": the point is stored on the MediaFile and
// every cover-cropped rendering (hero, cards) keeps it in view via --focal.
(function () {
    const pickers = document.querySelectorAll("[data-focal-picker]");
    if (!pickers.length) return;
    const csrf = document.querySelector("input[name=csrfmiddlewaretoken]");
    const placers = [];

    // The <img> element box and the painted image inside it differ under
    // object-fit (contain letterboxes, cover crops). All math must run in
    // the painted rect, or clicks in the letterbox skew the point.
    function paintedRect(img) {
        const box = img.getBoundingClientRect();
        const nw = img.naturalWidth;
        const nh = img.naturalHeight;
        if (!nw || !nh || !box.width || !box.height) return box;
        const fit = getComputedStyle(img).objectFit;
        let scale;
        if (fit === "contain") scale = Math.min(box.width / nw, box.height / nh);
        else if (fit === "cover") scale = Math.max(box.width / nw, box.height / nh);
        else return box;
        const width = nw * scale;
        const height = nh * scale;
        return {
            left: box.left + (box.width - width) / 2,
            top: box.top + (box.height - height) / 2,
            width: width,
            height: height,
        };
    }

    function clampPercent(value) {
        return Math.min(100, Math.max(0, Math.round(value)));
    }

    pickers.forEach(function (picker) {
        const img = picker.querySelector("img");
        const dot = picker.querySelector(".m-focal__dot");
        const flash = picker.querySelector(".m-focal__flash");
        if (!img || !dot) return;

        function placeDot() {
            const rect = paintedRect(img);
            const host = picker.getBoundingClientRect();
            dot.style.left = rect.left - host.left + (picker.dataset.x / 100) * rect.width + "px";
            dot.style.top = rect.top - host.top + (picker.dataset.y / 100) * rect.height + "px";
            dot.hidden = false;
        }
        placers.push(placeDot);

        if (img.complete && img.naturalWidth) placeDot();
        else img.addEventListener("load", placeDot);

        function say(text, isError) {
            if (!flash) return;
            flash.textContent = text;
            flash.classList.toggle("is-error", Boolean(isError));
            flash.hidden = false;
            clearTimeout(flash._timer);
            flash._timer = setTimeout(function () { flash.hidden = true; }, 1400);
        }

        picker.addEventListener("click", function (event) {
            const rect = paintedRect(img);
            if (!rect.width || !rect.height) return;
            picker.dataset.x = clampPercent(((event.clientX - rect.left) / rect.width) * 100);
            picker.dataset.y = clampPercent(((event.clientY - rect.top) / rect.height) * 100);
            placeDot();

            const body = new URLSearchParams();
            body.set("focal_x", picker.dataset.x);
            body.set("focal_y", picker.dataset.y);
            if (csrf) body.set("csrfmiddlewaretoken", csrf.value);
            fetch(picker.dataset.url, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                body: body.toString(),
                credentials: "same-origin",
            }).then(function (response) {
                if (!response.ok) throw new Error(String(response.status));
                say("Sparad");
            }).catch(function () {
                say("Kunde inte spara", true);
            });
        });
    });

    let focalRaf = null;
    window.addEventListener("resize", function () {
        if (focalRaf) return;
        focalRaf = requestAnimationFrame(function () {
            focalRaf = null;
            placers.forEach(function (place) { place(); });
        });
    });
})();

// "Optimera alla bilder" - runs the per-image optimize endpoint once per
// image, sequentially. One small request per image means no single request
// can hit a proxy timeout, no matter how large the library is.
(function () {
    const btn = document.querySelector("[data-optimize-all]");
    if (!btn) return;
    const progress = document.querySelector("[data-optimize-progress]");
    const csrf = document.querySelector("input[name=csrfmiddlewaretoken]");
    const ids = (btn.dataset.ids || "").split(",").filter(Boolean);
    let running = false;

    function report(text) {
        if (!progress) return;
        progress.hidden = false;
        progress.textContent = text;
    }

    btn.addEventListener("click", async function () {
        if (running || !ids.length) return;
        running = true;
        btn.disabled = true;
        let done = 0;
        let failed = 0;

        for (const id of ids) {
            report("Optimerar " + (done + failed + 1) + " av " + ids.length + "…");
            try {
                const body = new URLSearchParams();
                if (csrf) body.set("csrfmiddlewaretoken", csrf.value);
                const response = await fetch(btn.dataset.urlTemplate.replace("/0/", "/" + id + "/"), {
                    method: "POST",
                    headers: {
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    body: body.toString(),
                    credentials: "same-origin",
                });
                const data = response.ok ? await response.json() : { ok: false };
                if (data.ok) done += 1; else failed += 1;
            } catch (error) {
                failed += 1;
            }
        }

        if (failed) {
            // Stay on the page so the outcome is readable; the button
            // re-enables and a new run retries only what is still left.
            report(done + " optimerade, " + failed + " gick inte att optimera.");
            btn.disabled = false;
            running = false;
        } else {
            report("Klart: " + done + " optimerade.");
            setTimeout(function () { window.location.reload(); }, 900);
        }
    });
})();
