/**
 * LAMO Location Selection Popup & GPS Auto-Detection
 * Professional 2-step location selector for all 14 Kerala districts and all localities.
 * Integrates directly with session-based location mechanism and AJAX service filtering.
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
    const validationText = document.getElementById("locValidationText");

    // Views
    const stepDistrictsView = document.getElementById("locStepDistrictsView");
    const stepLocalitiesView = document.getElementById("locStepLocalitiesView");

    // GPS elements
    const btnGpsAutoDetect = document.getElementById("btnGpsAutoDetect");
    const gpsStatusText = document.getElementById("gpsStatusText");
    const gpsBadgeIcon = document.getElementById("gpsBadgeIcon");

    // District filter elements
    const districtSearchInput = document.getElementById("locDistrictFilterInput");
    const clearDistrictFilterBtn = document.getElementById("locClearFilterBtn");
    const districtGrid = document.getElementById("locDistrictGrid");
    const districtNoMatchState = document.getElementById("locNoMatchState");
    const districtSearchTermSpan = document.getElementById("locSearchTerm");

    // Locality elements
    const btnBackToDistricts = document.getElementById("btnBackToDistricts");
    const activeDistrictNameSpan = document.getElementById("locActiveDistrictName");
    const popularDistrictNameSpan = document.getElementById("locPopularDistrictName");
    const listDistrictNameSpan = document.getElementById("locListDistrictName");
    const popularPillsContainer = document.getElementById("locPopularPillsContainer");
    const localitiesGrid = document.getElementById("locLocalitiesGrid");
    const localitySearchInput = document.getElementById("locLocalityFilterInput");
    const clearLocalityFilterBtn = document.getElementById("locClearLocalityFilterBtn");
    const localityNoMatchState = document.getElementById("locNoLocalityMatchState");
    const localitySearchTermSpan = document.getElementById("locLocalitySearchTerm");
    const totalCountDisplay = document.getElementById("locTotalCountDisplay");
    const btnTopSaveLocation = document.getElementById("btnTopSaveLocation");

    // Summary element
    const summaryContainer = document.getElementById("locCurrentSelectionSummary");
    const summaryText = document.getElementById("locSummaryText");

    // Navbar elements
    const navLocationBtn = document.getElementById("navLocationBtn");
    const mobileNavLocationBtn = document.getElementById("mobileNavLocationBtn");
    const navCurrentDistrict = document.getElementById("navCurrentDistrict");
    const mobileNavCurrentDistrict = document.getElementById("mobileNavCurrentDistrict");
    const heroDistrictSelect = document.getElementById("heroDistrictSelect");
    const btnChangeDistrict = document.getElementById("btnChangeDistrict");

    if (!overlay || !card) return;

    // Load comprehensive Kerala locations tree from embedded JSON
    let locationData = {};
    const treeScript = document.getElementById("keralaLocationTreeData");
    if (treeScript && treeScript.textContent) {
      try {
        locationData = JSON.parse(treeScript.textContent);
      } catch (e) {
        console.warn("Could not parse keralaLocationTreeData:", e);
      }
    }

    let isMandatory = false;
    let selectedDistrict = overlay.getAttribute("data-initial-district") || "";
    let selectedLocality = overlay.getAttribute("data-initial-locality") || "";
    let selectedLat = overlay.getAttribute("data-initial-lat") || "";
    let selectedLng = overlay.getAttribute("data-initial-lng") || "";

    // -------------------------------------------------------------
    // SYNC ACTIVE CHIPS & SUMMARY
    // -------------------------------------------------------------
    function updateSummaryDisplay() {
      if (!summaryContainer || !summaryText) return;
      if (selectedDistrict) {
        summaryContainer.style.display = "flex";
        if (selectedLocality) {
          summaryText.textContent = selectedLocality + ", " + selectedDistrict;
        } else {
          summaryText.textContent = selectedDistrict + " (All Places)";
        }
      } else {
        summaryContainer.style.display = "none";
      }
    }

    function syncDistrictChips() {
      if (!districtGrid) return;
      const chips = districtGrid.querySelectorAll(".loc-district-chip");
      chips.forEach(function (chip) {
        const val = chip.getAttribute("data-district");
        if (selectedDistrict && val && val.toLowerCase() === selectedDistrict.toLowerCase()) {
          chip.classList.add("selected");
          chip.setAttribute("aria-pressed", "true");
        } else {
          chip.classList.remove("selected");
          chip.setAttribute("aria-pressed", "false");
        }
      });
    }

    // -------------------------------------------------------------
    // SWITCH BETWEEN DISTRICT & LOCALITY VIEWS
    // -------------------------------------------------------------
    function updateSidebarVisual(step, districtName) {
      const sideTitle = document.getElementById("locSidebarTitle");
      const sideTechnician = document.getElementById("locSidebarArtTechnician");
      const sideHouseboat = document.getElementById("locSidebarArtHouseboat");
      const sideBadges = document.getElementById("locTrustBadgesList");

      if (step === "districts") {
        if (sideTitle) sideTitle.innerHTML = "Find Services<br>in Your Area";
        if (sideTechnician) sideTechnician.style.display = "block";
        if (sideHouseboat) sideHouseboat.style.display = "none";
        if (sideBadges) {
          sideBadges.innerHTML =
            '<li class="loc-trust-badge-item">' +
            '  <span class="loc-trust-badge-icon"><i class="bi bi-geo-alt-fill"></i></span>' +
            '  <span class="loc-trust-badge-text">Local &amp; Verified Workers</span>' +
            '</li>' +
            '<li class="loc-trust-badge-item">' +
            '  <span class="loc-trust-badge-icon"><i class="bi bi-lightning-charge-fill"></i></span>' +
            '  <span class="loc-trust-badge-text">Faster Booking</span>' +
            '</li>' +
            '<li class="loc-trust-badge-item">' +
            '  <span class="loc-trust-badge-icon"><i class="bi bi-shield-check"></i></span>' +
            '  <span class="loc-trust-badge-text">Reliable &amp; Safe Service</span>' +
            '</li>';
        }
      } else if (step === "localities") {
        const dist = districtName || selectedDistrict || "Your Area";
        if (sideTitle) sideTitle.innerHTML = "Book Trusted<br>Home Services<br>in " + dist;
        if (sideTechnician) sideTechnician.style.display = "none";
        if (sideHouseboat) sideHouseboat.style.display = "block";
        if (sideBadges) {
          sideBadges.innerHTML =
            '<li class="loc-trust-badge-item">' +
            '  <span class="loc-trust-badge-icon"><i class="bi bi-shield-check"></i></span>' +
            '  <span class="loc-trust-badge-text">Verified Professionals</span>' +
            '</li>' +
            '<li class="loc-trust-badge-item">' +
            '  <span class="loc-trust-badge-icon"><i class="bi bi-lightning-charge-fill"></i></span>' +
            '  <span class="loc-trust-badge-text">Quick Booking</span>' +
            '</li>' +
            '<li class="loc-trust-badge-item">' +
            '  <span class="loc-trust-badge-icon"><i class="bi bi-headset"></i></span>' +
            '  <span class="loc-trust-badge-text">Reliable Support</span>' +
            '</li>';
        }
      }
    }

    function formatSaveButtonText(dist, loc) {
      const isMl = (window.LAMO_I18N && window.LAMO_I18N.getLanguage && window.LAMO_I18N.getLanguage() === 'ml');
      if (!dist) return isMl ? "സ്ഥലം സേവ് ചെയ്യുക" : "Save Location";
      if (loc) return isMl ? loc + ", " + dist + " സേവ് ചെയ്യുക" : "Save " + loc + ", " + dist;
      return isMl ? dist + " മുഴുവൻ സേവ് ചെയ്യുക" : "Save Entire " + dist;
    }

    function showDistrictsStep() {
      if (stepDistrictsView) stepDistrictsView.style.display = "block";
      if (stepLocalitiesView) stepLocalitiesView.style.display = "none";
      syncDistrictChips();
      updateSummaryDisplay();
      updateSidebarVisual("districts");
      if (continueBtnText) {
        continueBtnText.textContent = formatSaveButtonText(selectedDistrict, "");
      }
      if (continueBtnIcon) {
        continueBtnIcon.className = "bi bi-check-circle-fill";
      }
    }

    function showLocalitiesStep(districtName) {
      if (!districtName) return;
      selectedDistrict = districtName;
      syncDistrictChips();

      if (activeDistrictNameSpan) activeDistrictNameSpan.textContent = districtName;
      if (popularDistrictNameSpan) popularDistrictNameSpan.textContent = districtName;
      if (listDistrictNameSpan) listDistrictNameSpan.textContent = districtName;

      renderLocalitiesForDistrict(districtName);

      if (stepDistrictsView) stepDistrictsView.style.display = "none";
      if (stepLocalitiesView) stepLocalitiesView.style.display = "block";

      if (localitySearchInput) {
        localitySearchInput.value = "";
        localitySearchInput.placeholder = "Search town in " + districtName + "...";
      }

      updateSummaryDisplay();
      updateSidebarVisual("localities", districtName);
      if (continueBtnText) {
        continueBtnText.textContent = formatSaveButtonText(districtName, selectedLocality);
      }
      if (continueBtnIcon) {
        continueBtnIcon.className = "bi bi-check-circle-fill";
      }
    }

    // -------------------------------------------------------------
    // RENDER LOCALITIES FOR SELECTED DISTRICT
    // -------------------------------------------------------------
    function renderLocalitiesForDistrict(dist) {
      const distInfo = locationData[dist] || { popular: [], localities: [] };
      const populars = distInfo.popular || [];
      const localities = distInfo.localities || [];

      // 1. Render popular quick pills
      if (popularPillsContainer) {
        popularPillsContainer.innerHTML = "";

        // Add "All [District]" pill first
        const allPill = document.createElement("button");
        allPill.type = "button";
        allPill.className = "loc-popular-pill" + (!selectedLocality ? " selected" : "");
        allPill.textContent = "Entire " + dist;
        allPill.addEventListener("click", function () {
          selectLocality("", null, null, true);
        });
        popularPillsContainer.appendChild(allPill);

        populars.forEach(function (popName) {
          const pill = document.createElement("button");
          pill.type = "button";
          const isSelected = selectedLocality && selectedLocality.toLowerCase() === popName.toLowerCase();
          pill.className = "loc-popular-pill" + (isSelected ? " selected" : "");
          pill.textContent = popName;

          // Find coords
          const matchLoc = localities.find(function (l) { return l.name.toLowerCase() === popName.toLowerCase(); });
          const lat = matchLoc ? matchLoc.lat : null;
          const lng = matchLoc ? matchLoc.lng : null;

          pill.addEventListener("click", function () {
            selectLocality(popName, lat, lng, true);
          });
          popularPillsContainer.appendChild(pill);
        });
      }

      // 2. Render all localities
      if (localitiesGrid) {
        localitiesGrid.innerHTML = "";
        if (totalCountDisplay) {
          totalCountDisplay.textContent = localities.length + " places";
        }

        localities.forEach(function (loc) {
          const chip = document.createElement("button");
          chip.type = "button";
          const isSelected = selectedLocality && selectedLocality.toLowerCase() === loc.name.toLowerCase();
          chip.className = "loc-locality-chip" + (isSelected ? " selected" : "");
          chip.setAttribute("data-locality", loc.name);
          chip.setAttribute("data-lat", loc.lat || "");
          chip.setAttribute("data-lng", loc.lng || "");

          chip.innerHTML = 
            '<span class="loc-chip-name"><i class="bi bi-geo-alt me-1 text-muted"></i> ' + loc.name + '</span>' +
            '<span class="loc-chip-check"><i class="bi bi-check-circle-fill"></i></span>';

          chip.addEventListener("click", function () {
            selectLocality(loc.name, loc.lat, loc.lng, true);
          });
          localitiesGrid.appendChild(chip);
        });
      }
    }

    function selectLocality(locName, lat, lng, autoSave) {
      selectedLocality = locName || "";
      selectedLat = lat || "";
      selectedLng = lng || "";

      // Sync active state in popular pills
      if (popularPillsContainer) {
        popularPillsContainer.querySelectorAll(".loc-popular-pill").forEach(function (p) {
          if (!selectedLocality && p.textContent.startsWith("Entire")) {
            p.classList.add("selected");
          } else if (selectedLocality && p.textContent.toLowerCase() === selectedLocality.toLowerCase()) {
            p.classList.add("selected");
          } else {
            p.classList.remove("selected");
          }
        });
      }

      // Sync active state in all localities grid
      if (localitiesGrid) {
        localitiesGrid.querySelectorAll(".loc-locality-chip").forEach(function (c) {
          const name = c.getAttribute("data-locality");
          if (selectedLocality && name && name.toLowerCase() === selectedLocality.toLowerCase()) {
            c.classList.add("selected");
          } else {
            c.classList.remove("selected");
          }
        });
      }

      if (validationAlert) validationAlert.style.display = "none";
      updateSummaryDisplay();

      if (continueBtnText) {
        continueBtnText.textContent = formatSaveButtonText(selectedDistrict, selectedLocality);
      }
      if (continueBtnIcon) {
        continueBtnIcon.className = "bi bi-check-circle-fill";
      }

      if (autoSave) {
        applyLocation();
      }
    }

    // -------------------------------------------------------------
    // LOCALITY SEARCH FILTER
    // -------------------------------------------------------------
    function filterLocalities(query) {
      const q = (query || "").trim().toLowerCase();
      let matchCount = 0;

      if (clearLocalityFilterBtn) {
        clearLocalityFilterBtn.style.display = q ? "block" : "none";
      }

      if (localitiesGrid) {
        const chips = localitiesGrid.querySelectorAll(".loc-locality-chip");
        chips.forEach(function (chip) {
          const name = (chip.getAttribute("data-locality") || "").toLowerCase();
          if (!q || name.includes(q)) {
            chip.style.display = "flex";
            matchCount++;
          } else {
            chip.style.display = "none";
          }
        });
      }

      if (localityNoMatchState) {
        if (matchCount === 0 && q) {
          localityNoMatchState.style.display = "block";
          if (localitySearchTermSpan) localitySearchTermSpan.textContent = query;
        } else {
          localityNoMatchState.style.display = "none";
        }
      }
    }

    if (localitySearchInput) {
      localitySearchInput.addEventListener("input", function () {
        filterLocalities(localitySearchInput.value);
      });
    }

    if (clearLocalityFilterBtn) {
      clearLocalityFilterBtn.addEventListener("click", function () {
        if (localitySearchInput) {
          localitySearchInput.value = "";
          localitySearchInput.focus();
        }
        filterLocalities("");
      });
    }

    if (btnBackToDistricts) {
      btnBackToDistricts.addEventListener("click", function (e) {
        e.preventDefault();
        showDistrictsStep();
      });
    }

    // -------------------------------------------------------------
    // GPS AUTO-DETECTION HANDLER
    // -------------------------------------------------------------
    if (btnGpsAutoDetect) {
      btnGpsAutoDetect.addEventListener("click", function () {
        if (!navigator.geolocation) {
          triggerValidationShake("Geolocation is not supported by your browser.");
          return;
        }

        btnGpsAutoDetect.classList.add("locating");
        if (gpsStatusText) gpsStatusText.textContent = "Detecting your location via GPS...";
        if (gpsBadgeIcon) gpsBadgeIcon.innerHTML = '<i class="bi bi-arrow-repeat spin"></i>';

        navigator.geolocation.getCurrentPosition(
          function (pos) {
            const lat = pos.coords.latitude;
            const lng = pos.coords.longitude;

            // Send coords to backend for instant nearest Kerala district & locality resolution
            fetch("/api/location/gps/?lat=" + lat + "&lng=" + lng)
              .then(function (res) { return res.json(); })
              .then(function (data) {
                btnGpsAutoDetect.classList.remove("locating");
                if (gpsBadgeIcon) gpsBadgeIcon.innerHTML = '<i class="bi bi-check-circle-fill text-success"></i>';

                if (data.success && data.district) {
                  selectedDistrict = data.district;
                  selectedLocality = data.locality || "";
                  selectedLat = lat;
                  selectedLng = lng;

                  if (gpsStatusText) {
                    gpsStatusText.textContent = "Found: " + (selectedLocality ? selectedLocality + ", " : "") + selectedDistrict;
                  }

                  // Auto-switch to localities view with this detected place pre-selected
                  showLocalitiesStep(selectedDistrict);
                  selectLocality(selectedLocality, selectedLat, selectedLng);
                } else {
                  if (gpsStatusText) gpsStatusText.textContent = "Location found outside Kerala. Select district below.";
                }
              })
              .catch(function (err) {
                btnGpsAutoDetect.classList.remove("locating");
                if (gpsBadgeIcon) gpsBadgeIcon.innerHTML = '<i class="bi bi-exclamation-circle text-danger"></i>';
                if (gpsStatusText) gpsStatusText.textContent = "Could not resolve GPS location. Please select below.";
              });
          },
          function (err) {
            btnGpsAutoDetect.classList.remove("locating");
            if (gpsBadgeIcon) gpsBadgeIcon.innerHTML = '<i class="bi bi-exclamation-circle text-danger"></i>';
            if (gpsStatusText) gpsStatusText.textContent = "GPS permission denied or timed out. Please select below.";
            triggerValidationShake("Please select your district and locality manually.");
          },
          { timeout: 8000, enableHighAccuracy: true }
        );
      });
    }

    // -------------------------------------------------------------
    // MODAL OPEN & CLOSE FUNCTIONS
    // -------------------------------------------------------------
    function openLocationModal(options) {
      const opts = options || {};
      isMandatory = !!opts.mandatory;

      if (closeBtn) {
        closeBtn.style.display = isMandatory ? "none" : "flex";
      }

      if (validationAlert) {
        validationAlert.style.display = "none";
      }

      if (districtSearchInput) {
        districtSearchInput.value = "";
        filterDistricts("");
      }

      if (selectedDistrict) {
        showLocalitiesStep(selectedDistrict);
      } else {
        showDistrictsStep();
      }

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
        if (msg && validationText) {
          validationText.textContent = msg;
        }
        validationAlert.style.display = "flex";
      }
      card.classList.remove("shake");
      void card.offsetWidth;
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
          showLocalitiesStep(dist);
        }
      });
    }

    // -------------------------------------------------------------
    // REAL-TIME DISTRICT SEARCH FILTER
    // -------------------------------------------------------------
    function filterDistricts(query) {
      const q = (query || "").trim().toLowerCase();
      let matchCount = 0;

      if (clearDistrictFilterBtn) {
        clearDistrictFilterBtn.style.display = q ? "block" : "none";
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

      if (districtNoMatchState) {
        if (matchCount === 0 && q) {
          districtNoMatchState.style.display = "block";
          if (districtSearchTermSpan) districtSearchTermSpan.textContent = query;
        } else {
          districtNoMatchState.style.display = "none";
        }
      }
    }

    if (districtSearchInput) {
      districtSearchInput.addEventListener("input", function () {
        filterDistricts(districtSearchInput.value);
      });
    }

    if (clearDistrictFilterBtn) {
      clearDistrictFilterBtn.addEventListener("click", function () {
        if (districtSearchInput) {
          districtSearchInput.value = "";
          districtSearchInput.focus();
        }
        filterDistricts("");
      });
    }

    // -------------------------------------------------------------
    // CONTINUE / APPLY LOCATION
    // -------------------------------------------------------------
    function applyLocation() {
      if (!selectedDistrict) {
        triggerValidationShake("Please select your district to continue.");
        return;
      }

      continueBtn.disabled = true;
      if (continueBtnText) continueBtnText.textContent = "Saving Location...";
      if (continueBtnIcon) continueBtnIcon.className = "bi bi-arrow-repeat spin";
      if (btnTopSaveLocation) {
        btnTopSaveLocation.disabled = true;
        btnTopSaveLocation.innerHTML = '<i class="bi bi-arrow-repeat spin"></i> Saving...';
      }

      // Display text in navbar
      const displayLocation = selectedLocality 
        ? selectedLocality + ", " + selectedDistrict 
        : selectedDistrict;

      if (navCurrentDistrict) navCurrentDistrict.textContent = displayLocation;
      if (mobileNavCurrentDistrict) mobileNavCurrentDistrict.textContent = displayLocation;
      if (heroDistrictSelect) heroDistrictSelect.value = selectedDistrict;

      // Update overlay data attributes
      overlay.setAttribute("data-initial-district", selectedDistrict);
      overlay.setAttribute("data-initial-locality", selectedLocality);
      if (selectedLat) overlay.setAttribute("data-initial-lat", selectedLat);
      if (selectedLng) overlay.setAttribute("data-initial-lng", selectedLng);

      // Save via dedicated API endpoint
      let apiUrl = "/api/location/set/?district=" + encodeURIComponent(selectedDistrict);
      apiUrl += "&locality=" + encodeURIComponent(selectedLocality || "");
      if (selectedLat) apiUrl += "&lat=" + encodeURIComponent(selectedLat);
      if (selectedLng) apiUrl += "&lng=" + encodeURIComponent(selectedLng);

      const isHomePage = window.location.pathname === "/" || window.location.pathname === "";

      fetch(apiUrl, {
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
          continueBtn.disabled = false;
          if (continueBtnText) continueBtnText.textContent = "Save Location";
          if (continueBtnIcon) continueBtnIcon.className = "bi bi-check-circle-fill";
          if (btnTopSaveLocation) {
            btnTopSaveLocation.disabled = false;
            btnTopSaveLocation.innerHTML = '<i class="bi bi-check2"></i> Save';
          }

          closeLocationModal();

          if (isHomePage) {
            if (typeof window.fetchFilteredServices === "function") {
              window.fetchFilteredServices({ 
                district: selectedDistrict, 
                locality: selectedLocality,
                lat: selectedLat,
                lng: selectedLng 
              });
            } else {
              window.location.href = "/?district=" + encodeURIComponent(selectedDistrict) + (selectedLocality ? "&locality=" + encodeURIComponent(selectedLocality) : "") + "#services";
            }
          } else {
            // If on workers page or elsewhere, refresh current page with new district and locality
            const currentPath = window.location.pathname;
            if (currentPath.includes("/workers/")) {
              window.location.href = currentPath + "?district=" + encodeURIComponent(selectedDistrict) + (selectedLocality ? "&locality=" + encodeURIComponent(selectedLocality) : "");
            } else {
              window.location.href = "/?district=" + encodeURIComponent(selectedDistrict) + (selectedLocality ? "&locality=" + encodeURIComponent(selectedLocality) : "") + "#services";
            }
          }
        })
        .catch(function (err) {
          console.error("Error saving location:", err);
          window.location.href = "/?district=" + encodeURIComponent(selectedDistrict) + (selectedLocality ? "&locality=" + encodeURIComponent(selectedLocality) : "") + "#services";
        });
    }

    if (continueBtn) {
      continueBtn.addEventListener("click", applyLocation);
    }

    if (btnTopSaveLocation) {
      btnTopSaveLocation.addEventListener("click", applyLocation);
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

    if (isHomePage && !hasInitialDistrict) {
      setTimeout(function () {
        openLocationModal({ mandatory: true });
      }, 180);
    }

    window.openLocationModal = openLocationModal;
  });
})();
