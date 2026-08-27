/**
 * First-party analytics beacon.
 *
 * Sends only the client-side slice of data the server can't see:
 *   - screen resolution + viewport size (once per page load)
 *   - tel: / mailto: / outbound link clicks, plus where on the page they sit
 *   - engaged time (seconds the tab was actually visible and the user active)
 *   - inquiry form abandonment (left a wizard step without submitting)
 *
 * Uses navigator.sendBeacon so it never blocks navigation. The server resolves
 * the session from the httponly cookie - no IDs are read here.
 *
 * Everything is wrapped so an analytics failure can never break a page.
 */
(function () {
    "use strict";

    var ENDPOINT = "/analytics/beacon/";

    /* ------------------------------------------------------------------ *
     * Transport
     * ------------------------------------------------------------------ */

    function send(payload) {
        try {
            var body = JSON.stringify(payload);
            if (navigator.sendBeacon) {
                var blob = new Blob([body], { type: "application/json" });
                navigator.sendBeacon(ENDPOINT, blob);
            } else {
                fetch(ENDPOINT, {
                    method: "POST",
                    body: body,
                    headers: { "Content-Type": "application/json" },
                    keepalive: true,
                    credentials: "same-origin",
                });
            }
        } catch (e) {
            /* analytics must never throw */
        }
    }

    /* ------------------------------------------------------------------ *
     * 1. Screen + viewport, reported once on load
     * ------------------------------------------------------------------ */

    function reportScreen() {
        send({
            type: "screen",
            w: window.screen ? window.screen.width : 0,
            h: window.screen ? window.screen.height : 0,
            // Viewport is what CSS breakpoints respond to, so it is the number
            // worth having when deciding layout.
            vw: window.innerWidth || 0,
            vh: window.innerHeight || 0,
        });
    }

    /* ------------------------------------------------------------------ *
     * 2. Engaged time
     *
     * Counts a second only while the tab is visible. Idle time is excluded
     * after IDLE_LIMIT seconds without interaction, so a forgotten open tab
     * doesn't inflate the numbers. Deltas are flushed periodically and on
     * pagehide, which is the last moment a beacon is guaranteed to go out.
     * ------------------------------------------------------------------ */

    var TICK_MS = 1000;
    var FLUSH_SECONDS = 15;
    var IDLE_LIMIT = 30;

    var pending = 0;          // engaged seconds not yet sent
    var idleSeconds = 0;      // seconds since last real interaction
    var flushed = false;

    function onTick() {
        if (document.hidden) return;
        if (idleSeconds >= IDLE_LIMIT) return;
        idleSeconds += 1;
        pending += 1;
        if (pending >= FLUSH_SECONDS) flushEngagement();
    }

    function flushEngagement() {
        if (pending <= 0) return;
        send({
            type: "engagement",
            seconds: pending,
            path: window.location.pathname,
        });
        pending = 0;
    }

    function markActive() {
        idleSeconds = 0;
    }

    function startEngagementTracking() {
        setInterval(onTick, TICK_MS);

        ["mousemove", "keydown", "scroll", "click", "touchstart"].forEach(
            function (evt) {
                document.addEventListener(evt, markActive, { passive: true });
            }
        );

        // Flush when the tab is backgrounded - on mobile, pagehide may never
        // fire, so this is the reliable checkpoint.
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) flushEngagement();
            else markActive();
        });

        window.addEventListener("pagehide", function () {
            if (flushed) return;
            flushed = true;
            flushEngagement();
            reportAbandonment();
        });
    }

    /* ------------------------------------------------------------------ *
     * 3. Where on the page an interaction happened
     *
     * Most contact links are CMS-managed menu items, so hardcoding an
     * attribute on every one isn't practical. Walk up the DOM and match known
     * containers instead; an explicit data-analytics-position always wins.
     * ------------------------------------------------------------------ */

    var PLACEMENT_RULES = [
        [".c-nav__mobile", "mobile_nav"],
        [".c-nav", "header"],
        [".c-footer", "footer"],
        [".c-hero", "hero"],
        [".c-quote-cta", "quote_cta"],
        [".c-cta-block", "cta"],
        [".c-cta", "cta"],
        ["[data-sticky-cta]", "sticky"],
        ["article", "content"],
        ["main", "content"],
    ];

    function detectPlacement(el) {
        if (!el || !el.closest) return "other";

        var explicit = el.closest("[data-analytics-position]");
        if (explicit) {
            return explicit.getAttribute("data-analytics-position") || "other";
        }

        for (var i = 0; i < PLACEMENT_RULES.length; i++) {
            if (el.closest(PLACEMENT_RULES[i][0])) {
                return PLACEMENT_RULES[i][1];
            }
        }
        return "other";
    }

    /* ------------------------------------------------------------------ *
     * 4. Click tracking for tel:, mailto: and outbound links
     * ------------------------------------------------------------------ */

    function classifyLink(href) {
        if (!href) return null;
        if (href.indexOf("tel:") === 0) return { event: "tel", label: href.slice(4) };
        if (href.indexOf("mailto:") === 0) return { event: "email", label: href.slice(7) };
        if (/^https?:\/\//i.test(href)) {
            try {
                var url = new URL(href);
                if (url.hostname !== window.location.hostname) {
                    return { event: "outbound", label: url.hostname };
                }
            } catch (e) {
                return null;
            }
        }
        return null;
    }

    function onClick(e) {
        var el = e.target.closest ? e.target.closest("a") : null;
        if (!el) return;
        var info = classifyLink(el.getAttribute("href"));
        if (!info) return;
        send({
            type: "event",
            event: info.event,
            label: info.label,
            path: window.location.pathname,
            placement: detectPlacement(el),
        });
    }

    /* ------------------------------------------------------------------ *
     * 5. Inquiry form abandonment
     *
     * The form carries data-analytics-form="<step label>". If the visitor
     * leaves the page without submitting, report it. Validation errors are
     * recorded server-side, where the failing field is actually known.
     * ------------------------------------------------------------------ */

    var trackedForm = null;
    var submitted = false;

    function setupFormTracking() {
        trackedForm = document.querySelector("[data-analytics-form]");
        if (!trackedForm) return;
        trackedForm.addEventListener("submit", function () {
            submitted = true;
        });
    }

    function reportAbandonment() {
        if (!trackedForm || submitted) return;
        send({
            type: "form",
            event: "abandon",
            label: trackedForm.getAttribute("data-analytics-form") || "",
            path: window.location.pathname,
        });
    }

    /* ------------------------------------------------------------------ *
     * Init
     * ------------------------------------------------------------------ */

    function init() {
        reportScreen();
        setupFormTracking();
        startEngagementTracking();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    document.addEventListener("click", onClick, true);
})();
