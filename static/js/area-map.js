/**
 * Service-area map, loaded only on click.
 *
 * Nothing reaches Google until the visitor asks for it: no script tag, no
 * cookie, no IP. That covers the consent question, and it also keeps the bill
 * down - Google charges per map load, and most visitors never open the map.
 *
 * The custom palette is passed as a `styles` array, which means the map must
 * NOT be given a `mapId`. Cloud-based styling and the legacy `styles` option
 * are mutually exclusive; set both and Google silently ignores `styles`.
 */
(function () {
    "use strict";

    var MAP_STYLE = [
        {
            featureType: "all",
            elementType: "labels.text",
            stylers: [{ color: "#878787" }]
        },
        {
            featureType: "all",
            elementType: "labels.text.stroke",
            stylers: [{ visibility: "off" }]
        },
        {
            featureType: "landscape",
            elementType: "all",
            stylers: [{ color: "#f9f5ed" }]
        },
        {
            featureType: "road.highway",
            elementType: "all",
            stylers: [{ color: "#f5f5f5" }]
        },
        {
            featureType: "road.highway",
            elementType: "geometry.stroke",
            stylers: [{ color: "#c9c9c9" }]
        },
        {
            featureType: "water",
            elementType: "all",
            stylers: [{ color: "#aee0f4" }]
        }
    ];

    var scriptPromise = null;

    function loadMapsApi(key) {
        if (scriptPromise) return scriptPromise;
        scriptPromise = new Promise(function (resolve, reject) {
            var script = document.createElement("script");
            script.src =
                "https://maps.googleapis.com/maps/api/js?key=" +
                encodeURIComponent(key) +
                "&libraries=maps&loading=async&callback=__areaMapReady";
            script.async = true;
            script.onerror = function () {
                reject(new Error("Kunde inte ladda Google Maps."));
            };
            window.__areaMapReady = resolve;
            document.head.appendChild(script);
        });
        return scriptPromise;
    }

    function render(container) {
        var lat = parseFloat(container.dataset.lat);
        var lng = parseFloat(container.dataset.lng);
        var zoom = parseInt(container.dataset.zoom, 10) || 11;
        var center = { lat: lat, lng: lng };

        var map = new google.maps.Map(container, {
            center: center,
            zoom: zoom,
            styles: MAP_STYLE,
            disableDefaultUI: true,
            zoomControl: true,
            gestureHandling: "cooperative",
            keyboardShortcuts: false
        });

        // A soft circle reads as "we cover this area"; a pin would suggest an
        // address we do not have and do not want to claim.
        new google.maps.Circle({
            map: map,
            center: center,
            radius: parseInt(container.dataset.radius, 10) || 4000,
            strokeColor: "#1a1a1a",
            strokeOpacity: 0.35,
            strokeWeight: 1,
            fillColor: "#1a1a1a",
            fillOpacity: 0.06,
            clickable: false
        });
    }

    document.addEventListener("click", function (event) {
        var button = event.target.closest("[data-map-load]");
        if (!button) return;

        var wrapper = button.closest("[data-area-map]");
        if (!wrapper) return;
        var container = wrapper.querySelector("[data-map-canvas]");
        var key = wrapper.dataset.mapsKey;
        if (!container || !key) return;

        button.disabled = true;
        button.textContent = "Laddar karta...";

        loadMapsApi(key)
            .then(function () {
                wrapper.classList.add("is-loaded");
                render(container);
            })
            .catch(function () {
                button.disabled = false;
                button.textContent = "Kartan kunde inte laddas";
            });
    });
})();
