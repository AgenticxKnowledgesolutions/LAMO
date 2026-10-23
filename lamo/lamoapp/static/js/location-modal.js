/**
 * LAMO Location Selection Popup
 * Professional, modern location selection modal for the 14 Kerala districts.
 * Integrates directly with existing session-based location mechanism and AJAX service filtering.
 */

(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    const overlay = document.getElementById("locationModalOverlay");
    const card = document.getElementById("locationModalCard");
    const closeBtn = document.getElementById("locModalCloseBtn");
    const continueBtn = document.getElementById("btnContinueLocation");
    const continueBtnText = document.getElementById("btnContinueLocationText");
    const continueBtnIcon = document.getElementById("btnContinueLocationIcon");
    const validationAlert = document.getElementById("locValidationAlert");
    const searchInput = document.getElementById("locDistrictFilterInput");
    const clearFilterBtn = document.getElementById("locClearFilterBtn");
    const districtGrid = document.getElementById("locDistrictGrid");
    const noMatchState = document.getElementById("locNoMatchState");
    const searchTermSpan = document.getElementById("locSearchTerm");

    const navLocationBtn = document.getElementById("navLocationBtn");
    const mobileNavLocationBtn = document.getElementById("mobileNavLocationBtn");
    const navCurrentDistrict = document.getElementById("navCurrentDistrict");
    const mobileNavCurrentDistrict = document.getElementById("mobileNavCurrentDistrict");
    const heroDistrictSelect = document.getElementById("heroDistrictSelect");
    const btnChangeDistrict = document.getElementById("btnChangeDistrict");

    if (!overlay || !card) return;

    let isMandatory = false;
    let selectedDistrict = overlay.getAttribute("data-initial-district") || "";

    // Sync initial selected chip
    function syncSelectedChip(dist) {
      if (!districtGrid) return;
      const chips = districtGrid.querySelectorAll(".loc-district-chip");
      chips.forEach(function (chip) {
        const val = chip.getAttribute("data-district");
        if (dist && val && val.toLowerCase() === dist.toLowerCase()) {
          chip.classList.add("selected");
          chip.setAttribute("aria-pressed", "true");
        } else {
          chip.classList.remove("selected");
          chip.setAttribute("aria-pressed", "false");
        }
      });
    }

    if (selectedDistrict) {
      syncSelectedChip(selectedDistrict);
    }

    // -------------------------------------------------------------
    // MODAL OPEN & CLOSE FUNCTIONS
    // -------------------------------------------------------------
    function openLocationModal(options) {
      const opts = options || {};
      isMandatory = !!opts.mandatory;

      // Close button visibility: hide if mandatory first-time prompt
      if (closeBtn) {
        closeBtn.style.display = isMandatory ? "none" : "flex";
      }

      // Hide validation alert
      if (validationAlert) {
        validationAlert.style.display = "none";
      }

      // Reset filter search input
      if (searchInput) {
        searchInput.value = "";
        filterDistricts("");
      }

      // Sync active chip
      syncSelectedChip(selectedDistrict);

      // Show overlay and lock body scroll
      overlay.style.display = "flex";
      requestAnimationFrame(function () {
        overlay.classList.add("show");
        document.body.classList.add("location-modal-open");
      });
    }

    function closeLocationModal() {
      overlay.classList.remove("show");
      document.body.classList.remove("location-modal-open");
      setTimeout(function () {
        overlay.style.display = "none";
      }, 300);
    }

    function triggerValidationShake(msg) {
      if (validationAlert) {
        if (msg) {
          const span = validationAlert.querySelector("span");
          if (span) span.textContent = msg;
        }
        validationAlert.style.display = "flex";
      }
      card.classList.remove("shake");
      void card.offsetWidth; // trigger reflow
      card.classList.add("shake");
      setTimeout(function () {
        card.classList.remove("shake");
      }, 480);
    }

    // -------------------------------------------------------------
    // DISTRICT CHIP SELECTION
    // -------------------------------------------------------------
    if (districtGrid) {
      districtGrid.addEventListener("click", function (e) {
        const chip = e.target.closest(".loc-district-chip");
        if (!chip) return;

        const dist = chip.getAttribute("data-district");
        if (dist) {
          selectedDistrict = dist;
          syncSelectedChip(dist);
          if (validationAlert) {
            validationAlert.style.display = "none";
          }
        }
      });
    }

    // -------------------------------------------------------------
    // REAL-TIME DISTRICT SEARCH FILTER
    // -------------------------------------------------------------
    function filterDistricts(query) {
      const q = (query || "").trim().toLowerCase();
      let matchCount = 0;

      if (clearFilterBtn) {
        clearFilterBtn.style.display = q ? "block" : "none";
      }

      if (districtGrid) {
        const chips = districtGrid.querySelectorAll(".loc-district-chip");
        chips.forEach(function (chip) {
          const name = (chip.getAttribute("data-district") || "").toLowerCase();
          if (!q || name.includes(q)) {
            chip.style.display = "flex";
            matchCount++;
          } else {
            chip.style.display = "none";
          }
        });
      }

      if (noMatchState) {
        if (matchCount === 0 && q) {
          noMatchState.style.display = "block";
          if (searchTermSpan) searchTermSpan.textContent = query;
        } else {
          noMatchState.style.display = "none";
        }
      }
    }

    if (searchInput) {
      searchInput.addEventListener("input", function () {
        filterDistricts(searchInput.value);
      });
    }

    if (clearFilterBtn) {
      clearFilterBtn.addEventListener("click", function () {
        if (searchInput) {
          searchInput.value = "";
          searchInput.focus();
        }
        filterDistricts("");
      });
    }

    // -------------------------------------------------------------
    // CONTINUE BUTTON HANDLER
    // -------------------------------------------------------------
    if (continueBtn) {
      continueBtn.addEventListener("click", function () {
        if (!selectedDistrict) {
          triggerValidationShake("Please select your district to continue.");
          return;
        }

        // Show loading state on button
        continueBtn.disabled = true;
        if (continueBtnText) continueBtnText.textContent = "Setting Location...";
        if (continueBtnIcon) continueBtnIcon.className = "bi bi-arrow-repeat spin";

        // Update UI labels immediately
        if (navCurrentDistrict) navCurrentDistrict.textContent = selectedDistrict;
        if (mobileNavCurrentDistrict) mobileNavCurrentDistrict.textContent = selectedDistrict;
        if (heroDistrictSelect) heroDistrictSelect.value = selectedDistrict;

        // Save selected district in session via existing backend AJAX endpoint
        const isHomePage = window.location.pathname === "/" || window.location.pathname === "";

        fetch("/?district=" + encodeURIComponent(selectedDistrict) + "&ajax=1", {
          headers: {
            "X-Requested-With": "XMLHttpRequest",
            Accept: "application/json",
          },
        })
          .then(function (res) {
            if (!res.ok) throw new Error("HTTP error " + res.status);
            return res.json();
          })
          .then(function (data) {
            // Restore button state
            continueBtn.disabled = false;
            if (continueBtnText) continueBtnText.textContent = "Continue";
            if (continueBtnIcon) continueBtnIcon.className = "bi bi-arrow-right";

            // Update overlay data attribute
            overlay.setAttribute("data-initial-district", selectedDistrict);

            // Close modal
            closeLocationModal();

            if (isHomePage) {
              // On home page: invoke existing filter function to refresh services with real backend data
              if (typeof window.fetchFilteredServices === "function") {
                window.fetchFilteredServices({ district: selectedDistrict });
              } else {
                // Fallback: update URL
                window.location.href = "/?district=" + encodeURIComponent(selectedDistrict) + "#services";
              }
            } else {
              // On non-home pages: redirect to home page with district applied
              window.location.href = "/?district=" + encodeURIComponent(selectedDistrict) + "#services";
            }
          })
          .catch(function (err) {
            console.error("Error saving district location:", err);
            // Even if network glitches, apply to URL as fallback
            window.location.href = "/?district=" + encodeURIComponent(selectedDistrict) + "#services";
          });
      });
    }

    // -------------------------------------------------------------
    // CLOSE ACTIONS
    // -------------------------------------------------------------
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        if (!isMandatory || selectedDistrict) {
          closeLocationModal();
        } else {
          triggerValidationShake("Please select your district to continue.");
        }
      });
    }

    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) {
        if (!isMandatory || selectedDistrict) {
          closeLocationModal();
        } else {
          triggerValidationShake("Please select your district to continue.");
        }
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && overlay.classList.contains("show")) {
        if (!isMandatory || selectedDistrict) {
          closeLocationModal();
        } else {
          triggerValidationShake("Please select your district to continue.");
        }
      }
    });

    // -------------------------------------------------------------
    // NAVBAR & TRIGGER BUTTON LISTENERS
    // -------------------------------------------------------------
    if (navLocationBtn) {
      navLocationBtn.addEventListener("click", function (e) {
        e.preventDefault();
        openLocationModal({ mandatory: false });
      });
    }

    if (mobileNavLocationBtn) {
      mobileNavLocationBtn.addEventListener("click", function (e) {
        e.preventDefault();
        // Close bootstrap collapse if open
        const mainNav = document.getElementById("mainNav");
        if (mainNav && mainNav.classList.contains("show")) {
          const bsCollapse = bootstrap.Collapse.getInstance(mainNav);
          if (bsCollapse) bsCollapse.hide();
        }
        openLocationModal({ mandatory: false });
      });
    }

    if (btnChangeDistrict) {
      btnChangeDistrict.addEventListener("click", function (e) {
        e.preventDefault();
        openLocationModal({ mandatory: false });
      });
    }

    // Attach to any other "change district" links on the page
    document.querySelectorAll('a[href*="change_district=1"]').forEach(function (link) {
      link.addEventListener("click", function (e) {
        e.preventDefault();
        openLocationModal({ mandatory: false });
      });
    });

    // -------------------------------------------------------------
    // AUTOMATIC POPUP DISPLAY LOGIC
    // -------------------------------------------------------------
    const isHomePage = window.location.pathname === "/" || window.location.pathname === "";
    const hasInitialDistrict = !!selectedDistrict;

    // Requirement:
    // If on home page and NO location is selected yet, display the popup automatically.
    // If location is already selected, do not show popup again unnecessarily.
    if (isHomePage && !hasInitialDistrict) {
      // Delay slightly (180ms) for smooth visual entrance
      setTimeout(function () {
        openLocationModal({ mandatory: true });
      }, 180);
    }

    // Expose open function globally for other components if needed
    window.openLocationModal = openLocationModal;
  });
})();
