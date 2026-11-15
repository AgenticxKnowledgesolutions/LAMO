document.addEventListener("DOMContentLoaded", function () {

    const setLocationBtn =
        document.getElementById("setWorkerLocationBtn");

    const statusEl =
        document.getElementById("workerLocationStatus");

    const latInput =
        document.querySelector('input[name="latitude"]');

    const lngInput =
        document.querySelector('input[name="longitude"]');


    if (!setLocationBtn || !latInput || !lngInput) {
        return;
    }


    setLocationBtn.addEventListener("click", function () {

        /* ==========================================
           Browser Support
           ========================================== */

        if (!navigator.geolocation) {

            showStatus(
                "Geolocation is not supported by your browser.",
                "error"
            );

            return;
        }


        /* ==========================================
           Loading State
           ========================================== */

        setLocationBtn.disabled = true;

        setLocationBtn.classList.add("loading");

        setLocationBtn.innerHTML = `
            <span class="location-spinner"></span>
            <span>Getting Location...</span>
        `;

        showStatus(
            "Please allow location access...",
            "loading"
        );


        /* ==========================================
           Get Location
           ========================================== */

        navigator.geolocation.getCurrentPosition(

            function (position) {

                const latitude =
                    position.coords.latitude;

                const longitude =
                    position.coords.longitude;


                /* Set form values */

                latInput.value =
                    latitude.toFixed(6);

                lngInput.value =
                    longitude.toFixed(6);


                /* Trigger input event */

                latInput.dispatchEvent(
                    new Event("input", {
                        bubbles: true
                    })
                );

                lngInput.dispatchEvent(
                    new Event("input", {
                        bubbles: true
                    })
                );


                /* Success */

                showStatus(
                    "✓ Location set successfully.",
                    "success"
                );


                setLocationBtn.innerHTML = `
                    <span>✓</span>
                    <span>Location Updated</span>
                `;

                setLocationBtn.classList.remove("loading");

                setLocationBtn.classList.add("success");


                /* Reset button after 3 seconds */

                setTimeout(function () {

                    setLocationBtn.disabled = false;

                    setLocationBtn.classList.remove("success");

                    setLocationBtn.innerHTML = `
                        <span>📍</span>
                        <span>Use My Current Location</span>
                    `;

                }, 3000);

            },


            function (error) {

                let message =
                    "Unable to get your location.";


                switch (error.code) {

                    case error.PERMISSION_DENIED:

                        message =
                            "Location permission denied. Please allow location access.";

                        break;


                    case error.POSITION_UNAVAILABLE:

                        message =
                            "Location information is unavailable.";

                        break;


                    case error.TIMEOUT:

                        message =
                            "Location request timed out. Please try again.";

                        break;
                }


                showStatus(message, "error");


                setLocationBtn.disabled = false;

                setLocationBtn.classList.remove("loading");

                setLocationBtn.innerHTML = `
                    <span>📍</span>
                    <span>Try Again</span>
                `;

            },


            {
                enableHighAccuracy: true,

                timeout: 10000,

                maximumAge: 300000
            }
        );
    });


    /* ==========================================
       STATUS FUNCTION
       ========================================== */

    function showStatus(message, type) {

        if (!statusEl) {
            return;
        }

        statusEl.textContent = message;

        statusEl.className =
            "ep-location-status " + type;
    }

    /* ==========================================
       WORKING SHIFT HOURS (12-HOUR CLOCK CONTROLLER)
       ========================================== */
    const startSelect = document.getElementById("id_work_start_time");
    const endSelect = document.getElementById("id_work_end_time");
    const previewText = document.getElementById("shiftHoursPreviewText");
    const alertBox = document.getElementById("shiftHoursValidationAlert");
    const presetBtns = document.querySelectorAll(".shift-preset-btn");

    function formatTimeDisplay(val) {
        if (!val) return "";
        const parts = val.split(":").map(Number);
        let h = parts[0];
        const m = parts[1] || 0;
        const ampm = h >= 12 ? "PM" : "AM";
        h = h % 12;
        if (h === 0) h = 12;
        const mStr = m > 0 ? (":" + String(m).padStart(2, "0")) : ":00";
        return `${h}${mStr} ${ampm}`;
    }

    function updateShiftPreview() {
        if (!startSelect || !endSelect || !previewText) return;
        const startVal = startSelect.value;
        const endVal = endSelect.value;
        if (!startVal || !endVal) return;

        const sParts = startVal.split(":").map(Number);
        const eParts = endVal.split(":").map(Number);
        const sMins = sParts[0] * 60 + (sParts[1] || 0);
        const eMins = eParts[0] * 60 + (eParts[1] || 0);

        if (eMins <= sMins) {
            if (alertBox) alertBox.style.display = "block";
            previewText.textContent = `${formatTimeDisplay(startVal)} - Invalid End Time`;
            previewText.style.color = "#dc2626";
        } else {
            if (alertBox) alertBox.style.display = "none";
            const diffHours = ((eMins - sMins) / 60).toFixed(1).replace(".0", "");
            previewText.textContent = `${formatTimeDisplay(startVal)} - ${formatTimeDisplay(endVal)} (${diffHours}h Shift)`;
            previewText.style.color = "#166534";
        }

        // Highlight matching preset button
        presetBtns.forEach(function(btn) {
            if (btn.dataset.start === startVal && btn.dataset.end === endVal) {
                btn.style.background = "#dcfce7";
                btn.style.borderColor = "#16a34a";
                btn.style.color = "#15803d";
                btn.style.fontWeight = "700";
            } else {
                btn.style.background = "#ffffff";
                btn.style.borderColor = "#cbd5e1";
                btn.style.color = "#334155";
                btn.style.fontWeight = "600";
            }
        });
    }

    if (startSelect && endSelect) {
        startSelect.addEventListener("change", updateShiftPreview);
        endSelect.addEventListener("change", updateShiftPreview);
        updateShiftPreview();
    }

    presetBtns.forEach(function(btn) {
        btn.addEventListener("click", function() {
            if (startSelect && endSelect) {
                startSelect.value = this.dataset.start;
                endSelect.value = this.dataset.end;
                updateShiftPreview();
            }
        });
    });

});