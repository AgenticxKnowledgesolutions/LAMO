/**
 * LAMO Home Page Modern Controller
 * -------------------------------------------------------------
 * Features:
 * 1. Geolocation to nearest Kerala District.
 * 2. Real-time AJAX service filtering by District, Search Query & Category.
 * 3. Dynamic DOM updates for service cards with actual DB data (no fake ratings/prices).
 * 4. Interactive "More Services" category expansion drawer.
 * 5. Single horizontal track continuous auto-scrolling carousel (2-second interval, smooth transition, hover pause, touch swipe, manual nav).
 * 6. Navbar glassmorphism scroll transition.
 */

(function () {
  "use strict";

  // Coordinates of 14 Kerala Districts for nearest-district mapping
  const KERALA_DISTRICT_COORDS = [
    { name: "Thiruvananthapuram", lat: 8.5241, lng: 76.9366 },
    { name: "Kollam", lat: 8.8932, lng: 76.6141 },
    { name: "Pathanamthitta", lat: 9.2648, lng: 76.787 },
    { name: "Alappuzha", lat: 9.4981, lng: 76.3388 },
    { name: "Kottayam", lat: 9.5916, lng: 76.5222 },
    { name: "Idukki", lat: 9.9189, lng: 77.1025 },
    { name: "Ernakulam", lat: 9.9816, lng: 76.2999 },
    { name: "Thrissur", lat: 10.5276, lng: 76.2144 },
    { name: "Palakkad", lat: 10.7867, lng: 76.6548 },
    { name: "Malappuram", lat: 11.0732, lng: 76.074 },
    { name: "Kozhikode", lat: 11.2588, lng: 75.7804 },
    { name: "Wayanad", lat: 11.6854, lng: 76.132 },
    { name: "Kannur", lat: 11.8745, lng: 75.3704 },
    { name: "Kasaragod", lat: 12.5102, lng: 74.9852 },
  ];

  function getDistanceSq(lat1, lng1, lat2, lng2) {
    const dLat = lat1 - lat2;
    const dLng = lng1 - lng2;
    return dLat * dLat + dLng * dLng;
  }

  function findClosestDistrict(userLat, userLng) {
    let closest = KERALA_DISTRICT_COORDS[0].name;
    let minDistance = Infinity;

    for (let i = 0; i < KERALA_DISTRICT_COORDS.length; i++) {
      const d = getDistanceSq(
        userLat,
        userLng,
        KERALA_DISTRICT_COORDS[i].lat,
        KERALA_DISTRICT_COORDS[i].lng
      );
      if (d < minDistance) {
        minDistance = d;
        closest = KERALA_DISTRICT_COORDS[i].name;
      }
    }
    return closest;
  }

  function escapeHtml(text) {
    if (!text) return "";
    const map = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    };
    return text.toString().replace(/[&<>"']/g, function (m) {
      return map[m];
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // DOM Elements
    const heroForm = document.getElementById("heroLocationForm");
    const districtSelect = document.getElementById("heroDistrictSelect");
    const searchInput = document.getElementById("liveServiceSearch") || document.getElementById("heroServiceSearch");
    const clearSearchBtn = document.getElementById("clearSearchBtn") || document.getElementById("clearHeroSearchBtn");
    const btnUseLocation = document.getElementById("btnHeroUseLocation");

    const servicesGrid = document.getElementById("servicesGrid");
    const servicesWrapper = document.querySelector(".services-carousel-wrapper");
    const btnPrevService = document.getElementById("btnPrevService");
    const btnNextService = document.getElementById("btnNextService");

    const activeDistrictTitle = document.getElementById("activeDistrictTitle");
    const servicesCountBadge = document.getElementById("servicesCountBadge");
    const emptyDistrictState = document.getElementById("emptyDistrictState") || document.getElementById("noServicesDistrictMessage");
    const noMatchServicesMessage = document.getElementById("noMatchServicesMessage");
    const btnSelectKollam = document.getElementById("btnSelectKollam");
    const btnResetSearch = document.getElementById("btnResetSearch");

    const btnMoreCategories = document.getElementById("btnMoreCategories");
    const moreCategoriesContainer = document.getElementById("moreCategoriesContainer");
    const btnCloseMoreCategories = document.getElementById("btnCloseMoreCategories");

    let currentCategory = "all";
    let isFetching = false;
    let carouselTimer = null;
    let isUserInteractingWithCarousel = false;

    // Detect user reduced motion preference
    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // -------------------------------------------------------------
    // 1. INFINITE KINETIC STREAM CONTROLLER
    // Continuous ribbon with faded edges. Slows on hover,
    // lifts card with green glow, drag or use arrows to move it.
    // -------------------------------------------------------------
    const kineticStreamWrapper = document.getElementById("servicesCarouselWrapper") || document.getElementById("kineticStreamWrapper");
    let animFrameId = null;
    let streamPos = 0;
    const normalSpeed = 0.70; // baseline smooth stream speed
    const hoverSpeed = 0.0;   // pauses smoothly on hover
    let currentSpeed = normalSpeed;
    let inertiaVelocity = 0;
    let singleSetWidth = 0;

    let isHovered = false;
    let isDragging = false;
    let hasDragged = false;
    let dragStartX = 0;
    let dragStartScroll = 0;
    let lastPointerX = 0;
    let lastPointerTime = 0;

    function stopInfiniteKineticStream() {
      if (animFrameId) {
        cancelAnimationFrame(animFrameId);
        animFrameId = null;
      }
    }

    function setupInfiniteKineticStream() {
      stopInfiniteKineticStream();
      if (!servicesGrid) return;

      // Remove any previously cloned cards
      servicesGrid.querySelectorAll("[data-clone='true']").forEach(function (el) {
        el.remove();
      });

      const cards = Array.from(servicesGrid.querySelectorAll(".service-card-modern:not([data-clone='true'])"));
      if (cards.length < 2) {
        if (btnPrevService) btnPrevService.style.display = "none";
        if (btnNextService) btnNextService.style.display = "none";
        singleSetWidth = 0;
        return;
      }

      requestAnimationFrame(function () {
        if (!servicesGrid) return;
        const freshCards = Array.from(servicesGrid.querySelectorAll(".service-card-modern:not([data-clone='true'])"));
        if (freshCards.length < 2) return;

        let totalWidth = 0;
        freshCards.forEach(function (c) {
          totalWidth += c.offsetWidth + 16;
        });
        singleSetWidth = totalWidth;

        const wrapperWidth = servicesGrid.clientWidth || window.innerWidth;
        if (singleSetWidth > 0) {
          const cloneMultiplier = Math.max(1, Math.ceil(wrapperWidth / singleSetWidth));
          for (let m = 0; m < cloneMultiplier; m++) {
            freshCards.forEach(function (card) {
              const clone = card.cloneNode(true);
              clone.setAttribute("data-clone", "true");
              clone.setAttribute("aria-hidden", "true");
              clone.tabIndex = -1;
              servicesGrid.appendChild(clone);
            });
          }

          if (btnPrevService) btnPrevService.style.display = "inline-flex";
          if (btnNextService) btnNextService.style.display = "inline-flex";
          startInfiniteKineticStream();
        }
      });
    }

    function startInfiniteKineticStream() {
      stopInfiniteKineticStream();
      if (prefersReducedMotion) return;

      function streamLoop() {
        if (!servicesGrid || singleSetWidth <= 0) {
          animFrameId = requestAnimationFrame(streamLoop);
          return;
        }

        if (!isDragging) {
          // Slows on hover smoothly
          const target = isHovered ? hoverSpeed : normalSpeed;
          currentSpeed += (target - currentSpeed) * 0.08;

          // Inertia damping
          inertiaVelocity *= 0.94;
          if (Math.abs(inertiaVelocity) < 0.01) {
            inertiaVelocity = 0;
          }

          streamPos += (currentSpeed + inertiaVelocity);

          // Infinite seamless loop wrap
          if (streamPos >= singleSetWidth) {
            streamPos -= singleSetWidth;
          } else if (streamPos < 0) {
            streamPos += singleSetWidth;
          }

          servicesGrid.scrollLeft = streamPos;
        } else {
          streamPos = servicesGrid.scrollLeft;
        }

        animFrameId = requestAnimationFrame(streamLoop);
      }

      animFrameId = requestAnimationFrame(streamLoop);
    }

    if (servicesGrid) {
      // Hover detection on ribbon: Slows on hover
      const hoverTarget = kineticStreamWrapper || servicesGrid;
      hoverTarget.addEventListener("mouseenter", function () {
        isHovered = true;
      });
      hoverTarget.addEventListener("mouseleave", function () {
        isHovered = false;
      });

      // Pointer Drag (Mouse)
      servicesGrid.addEventListener("mousedown", function (e) {
        if (e.button !== 0 || singleSetWidth <= 0) return;
        isDragging = true;
        hasDragged = false;
        dragStartX = e.pageX;
        dragStartScroll = servicesGrid.scrollLeft;
        lastPointerX = e.pageX;
        lastPointerTime = performance.now();
        inertiaVelocity = 0;
        servicesGrid.classList.add("is-dragging");
      });

      window.addEventListener("mousemove", function (e) {
        if (!isDragging) return;
        const dx = e.pageX - dragStartX;
        if (Math.abs(dx) > 6) {
          hasDragged = true;
        }

        const now = performance.now();
        const dt = Math.max(1, now - lastPointerTime);
        const instantDx = e.pageX - lastPointerX;
        inertiaVelocity = -instantDx * (16 / dt);
        lastPointerX = e.pageX;
        lastPointerTime = now;

        let newScroll = dragStartScroll - dx;
        if (singleSetWidth > 0) {
          while (newScroll >= singleSetWidth) newScroll -= singleSetWidth;
          while (newScroll < 0) newScroll += singleSetWidth;
        }
        streamPos = newScroll;
        servicesGrid.scrollLeft = streamPos;
      });

      window.addEventListener("mouseup", function () {
        if (isDragging) {
          isDragging = false;
          servicesGrid.classList.remove("is-dragging");
          inertiaVelocity = Math.max(-18, Math.min(18, inertiaVelocity));
          setTimeout(function () {
            hasDragged = false;
          }, 120);
        }
      });

      // Suppress click on card if user was dragging
      servicesGrid.addEventListener("click", function (e) {
        if (hasDragged) {
          e.preventDefault();
          e.stopPropagation();
        }
      }, true);

      // Touch Drag (Mobile / Tablets)
      let touchStartX = 0;
      let touchStartScroll = 0;
      let lastTouchX = 0;
      let lastTouchTime = 0;

      servicesGrid.addEventListener("touchstart", function (e) {
        if (!e.touches.length) return;
        isDragging = true;
        hasDragged = false;
        isHovered = true;
        touchStartX = e.touches[0].pageX;
        touchStartScroll = servicesGrid.scrollLeft;
        lastTouchX = e.touches[0].pageX;
        lastTouchTime = performance.now();
        inertiaVelocity = 0;
      }, { passive: true });

      servicesGrid.addEventListener("touchmove", function (e) {
        if (!isDragging || !e.touches.length) return;
        const currentX = e.touches[0].pageX;
        const dx = currentX - touchStartX;
        if (Math.abs(dx) > 6) {
          hasDragged = true;
        }

        const now = performance.now();
        const dt = Math.max(1, now - lastTouchTime);
        const instantDx = currentX - lastTouchX;
        inertiaVelocity = -instantDx * (16 / dt);
        lastTouchX = currentX;
        lastTouchTime = now;

        let newScroll = touchStartScroll - dx;
        if (singleSetWidth > 0) {
          while (newScroll >= singleSetWidth) newScroll -= singleSetWidth;
          while (newScroll < 0) newScroll += singleSetWidth;
        }
        streamPos = newScroll;
        servicesGrid.scrollLeft = streamPos;
      }, { passive: true });

      servicesGrid.addEventListener("touchend", function () {
        isDragging = false;
        inertiaVelocity = Math.max(-18, Math.min(18, inertiaVelocity));
        setTimeout(function () {
          isHovered = false;
          hasDragged = false;
        }, 1200);
      }, { passive: true });

      // Arrow navigation (impulse surges)
      if (btnPrevService) {
        btnPrevService.addEventListener("click", function (e) {
          e.preventDefault();
          isHovered = true;
          inertiaVelocity -= 18;
          setTimeout(function () { isHovered = false; }, 800);
        });
      }

      if (btnNextService) {
        btnNextService.addEventListener("click", function (e) {
          e.preventDefault();
          isHovered = true;
          inertiaVelocity += 18;
          setTimeout(function () { isHovered = false; }, 800);
        });
      }

      // Initialize Stream
      setupInfiniteKineticStream();
      window.addEventListener("load", function () {
        setTimeout(setupInfiniteKineticStream, 150);
      });
      window.addEventListener("resize", function () {
        clearTimeout(window.__carouselResizeTimer);
        window.__carouselResizeTimer = setTimeout(setupInfiniteKineticStream, 200);
      });
    }

    // -------------------------------------------------------------
    // 2. DYNAMIC CARD BUILDER (Real Database Data)
    // -------------------------------------------------------------
    // 2. DYNAMIC CARD BUILDER (Real Database Data)
    // -------------------------------------------------------------
    function renderServiceCards(services, selectedDistrict) {
      if (!servicesGrid) return;

      if (!services || services.length === 0) {
        servicesGrid.innerHTML = "";
        servicesGrid.style.display = "none";
        stopInfiniteKineticStream();
        if (btnPrevService) btnPrevService.style.display = "none";
        if (btnNextService) btnNextService.style.display = "none";
        return;
      }

      servicesGrid.style.display = "flex";

      // Strict de-duplication by service.id
      const seenIds = new Set();
      const uniqueServices = [];
      services.forEach(function (s) {
        if (!seenIds.has(s.id)) {
          seenIds.add(s.id);
          uniqueServices.push(s);
        }
      });

      let html = "";
      uniqueServices.forEach(function (s) {
        const workerCount = s.worker_count !== undefined ? s.worker_count : 0;

        // Overlay icon based on category/name
        let iconHtml = '<i class="bi bi-tools text-success"></i>';
        const nLower = (s.name || "").toLowerCase();
        if (nLower.includes("clean")) iconHtml = '<i class="bi bi-stars text-success"></i>';
        else if (nLower.includes("electr")) iconHtml = '<i class="bi bi-lightbulb-fill text-warning"></i>';
        else if (nLower.includes("plumb")) iconHtml = '<i class="bi bi-droplet-fill text-primary"></i>';
        else if (nLower.includes("ac")) iconHtml = '<i class="bi bi-snow text-info"></i>';
        else if (nLower.includes("paint")) iconHtml = '<i class="bi bi-paint-bucket text-danger"></i>';
        else if (nLower.includes("carpent")) iconHtml = '<i class="bi bi-hammer text-brown"></i>';
        else if (nLower.includes("appliance")) iconHtml = '<i class="bi bi-gear-fill text-secondary"></i>';
        else if (nLower.includes("garden")) iconHtml = '<i class="bi bi-tree-fill text-success"></i>';

        // Rating HTML - strictly backend driven
        let ratingHtml = "";
        if (s.rating && s.rating > 0) {
          ratingHtml = `
            <i class="bi bi-star-fill star-gold"></i>
            <span class="rating-num">${s.rating.toFixed(1)}</span>
            ${s.review_count ? `<span class="rating-reviews-count" style="font-size: 11px; color: var(--text-muted, #64748b);">(${s.review_count})</span>` : ""}
          `;
        } else {
          ratingHtml = `<span class="badge-new-service">Verified</span>`;
        }

        let prosText = "";
        if (workerCount > 0) {
          prosText = `(${workerCount} pro${workerCount > 1 ? "s" : ""})`;
        } else {
          const currentDistrict = (districtSelect && districtSelect.value) ? districtSelect.value : "";
          prosText = currentDistrict ? "(No pros in area)" : "(Pros on call)";
        }

        // Image with category fallback
        let imageSrc = s.image_url;
        if (!imageSrc) {
          if (nLower.includes("plumb")) imageSrc = "/static/img/plumber.webp";
          else if (nLower.includes("electr")) imageSrc = "/static/img/electrician.webp";
          else if (nLower.includes("clean")) imageSrc = "/static/img/cleaning.png";
          else if (nLower.includes("paint")) imageSrc = "/static/img/painter.webp";
          else if (nLower.includes("ac")) imageSrc = "/static/img/ac-repair.webp";
          else if (nLower.includes("carpent")) imageSrc = "/static/img/carpenter.webp";
          else if (nLower.includes("appliance")) imageSrc = "/static/img/appliance.webp";
          else imageSrc = "/static/img/general.png";
        }
        let imageHtml = `<img src="${imageSrc}" alt="${escapeHtml(s.name)}" loading="lazy" onerror="this.onerror=null;this.src='/static/img/general.png';">`;

        html += `
          <a href="${s.url}" 
             class="service-card-modern" 
             data-id="${s.id}"
             data-name="${escapeHtml(s.name).toLowerCase()}" 
             data-desc="${escapeHtml(s.description || '').toLowerCase()}"
             data-category="${escapeHtml(s.category || '')}">

            <div class="service-card-thumb">
              <div class="service-cat-overlay-icon">
                ${iconHtml}
              </div>
              ${imageHtml}
            </div>

            <div class="service-card-body">
              <h3 class="service-card-title">${escapeHtml(s.name)}</h3>
              <div class="service-card-rating">
                ${ratingHtml}
                <span class="rating-pros-count">${prosText}</span>
              </div>
              <div class="service-card-action">
                <span>View Workers</span>
                <i class="bi bi-arrow-right"></i>
              </div>
            </div>

          </a>
        `;
      });

      servicesGrid.innerHTML = html;
      servicesGrid.scrollLeft = 0;
      setupInfiniteKineticStream();
    }

    // -------------------------------------------------------------
    // 2B. INSTANT AUTO-SCROLL CONTROLLER
    // -------------------------------------------------------------
    function scrollToServicesSection(query) {
      const servicesSection = document.getElementById("services");
      if (!servicesSection) return;

      // Instant, direct smooth scroll with offset for header navbar
      const navbarHeight = 70;
      const targetPos = servicesSection.getBoundingClientRect().top + window.pageYOffset - navbarHeight;
      window.scrollTo({
        top: Math.max(0, targetPos),
        behavior: "smooth"
      });

      // Highlight target service card if query is present
      if (query && servicesGrid) {
        setTimeout(function () {
          const cards = servicesGrid.querySelectorAll(".service-card-modern");
          const q = query.trim().toLowerCase();
          cards.forEach(function (card) {
            const titleEl = card.querySelector(".service-card-title");
            if (titleEl && titleEl.textContent.toLowerCase().includes(q)) {
              card.classList.add("search-highlight-target");
              setTimeout(function () {
                card.classList.remove("search-highlight-target");
              }, 2600);
            }
          });
        }, 320);
      }
    }
    window.scrollToServicesSection = scrollToServicesSection;

    function triggerHeroSearch(query) {
      const q = (query !== undefined ? query : (searchInput ? searchInput.value : "")).trim();
      if (searchInput) searchInput.value = q;
      if (clearSearchBtn) clearSearchBtn.style.display = q.length > 0 ? "inline-flex" : "none";

      // Close search suggestions dropdown if open
      const drop = document.querySelector(".search-suggestions-dropdown");
      if (drop) drop.classList.remove("is-open");

      // Idea 2: Direct Navigation to Service Workers / All Services if non-empty query
      if (q && typeof window.lamoNavigateToService === "function") {
        window.lamoNavigateToService(q);
        return;
      }

      // 1. INSTANT SMOOTH SCROLL (0ms delay, starts scrolling immediately!)
      scrollToServicesSection(q);

      // 2. Concurrently fetch and render matching cards
      fetchFilteredServices({
        district: districtSelect ? districtSelect.value : "",
        query: q,
        scrollToResults: false,
        showDropdown: false
      });
    }
    window.triggerHeroSearch = triggerHeroSearch;

    // -------------------------------------------------------------
    // 3. LIVE BACKEND AJAX FETCHING
    // -------------------------------------------------------------
    function fetchFilteredServices(options) {
      window.fetchFilteredServices = fetchFilteredServices;
      const opts = options || {};
      const district = opts.district !== undefined ? opts.district : (districtSelect ? districtSelect.value : "");
      const query = opts.query !== undefined ? opts.query : (searchInput ? searchInput.value.trim() : "");
      const category = opts.category !== undefined ? opts.category : currentCategory;
      const updateUrl = opts.updateUrl !== false;
      const shouldScrollToResults = !!opts.scrollToResults;
      const shouldShowDropdown = opts.showDropdown !== undefined ? opts.showDropdown : true;

      if (isFetching) return;
      isFetching = true;

      // Show clear button if query is present
      if (clearSearchBtn) {
        clearSearchBtn.style.display = query.length > 0 ? "inline-flex" : "none";
      }

      // Add a subtle opacity transition to services
      if (servicesGrid) {
        servicesGrid.style.opacity = "0.5";
      }

      const params = new URLSearchParams();
      if (district) params.set("district", district);
      if (query) params.set("search", query);
      if (category && category !== "all" && category !== "more") params.set("category", category);
      params.set("ajax", "1");

      const ajaxUrl = "/?" + params.toString();

      fetch(ajaxUrl, {
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
          isFetching = false;
          if (servicesGrid) servicesGrid.style.opacity = "1";

          if (data && data.status === "success") {
            const selectedDistrict = data.selected_district || district;
            const count = data.services_count || 0;

            // 1. Update Heading and Badge
            if (activeDistrictTitle) {
              activeDistrictTitle.textContent = selectedDistrict || "All Kerala";
            }
            if (servicesCountBadge) {
              servicesCountBadge.textContent = `${count} Available`;
            }

            // Sync navbar and modal state
            if (selectedDistrict) {
              const navCurrent = document.getElementById("navCurrentDistrict");
              if (navCurrent) navCurrent.textContent = selectedDistrict;
              const mobileNavCurrent = document.getElementById("mobileNavCurrentDistrict");
              if (mobileNavCurrent) mobileNavCurrent.textContent = selectedDistrict;
              const modalOverlay = document.getElementById("locationModalOverlay");
              if (modalOverlay) modalOverlay.setAttribute("data-initial-district", selectedDistrict);
              if (districtSelect && districtSelect.value !== selectedDistrict) {
                districtSelect.value = selectedDistrict;
              }
            }

            // 2. Update district name in all empty-state notices
            document.querySelectorAll(".no-services-district-name").forEach(function (el) {
              el.textContent = selectedDistrict;
            });

            // 3. Update Empty State displays
            if (count === 0) {
              if (servicesGrid) {
                servicesGrid.innerHTML = "";
                servicesGrid.style.display = "none";
              }
              if (query) {
                if (noMatchServicesMessage) {
                  noMatchServicesMessage.style.display = "block";
                  const qHeading = noMatchServicesMessage.querySelector("h4");
                  if (qHeading) {
                    qHeading.textContent = selectedDistrict
                      ? `No matching services found in ${selectedDistrict}`
                      : "No matching services found";
                  }
                }
                if (emptyDistrictState) emptyDistrictState.style.display = "none";
              } else {
                if (emptyDistrictState) {
                  emptyDistrictState.style.display = "block";
                  const dHeading = emptyDistrictState.querySelector("h4");
                  if (dHeading) {
                    dHeading.textContent = selectedDistrict
                      ? `No services available in ${selectedDistrict} yet. Try another district.`
                      : "No services match your criteria.";
                  }
                }
                if (noMatchServicesMessage) noMatchServicesMessage.style.display = "none";
              }
            } else {
              if (emptyDistrictState) emptyDistrictState.style.display = "none";
              if (noMatchServicesMessage) noMatchServicesMessage.style.display = "none";
              // 4. Render actual cards
              latestFetchedServices = data.services || [];
              renderServiceCards(data.services, selectedDistrict);
            }

            // Smooth Auto-scroll to results if requested
            if (shouldScrollToResults) {
              scrollToServicesSection(query);
            }

            // 5. Update browser history URL cleanly without reload
            if (updateUrl) {
              const cleanParams = new URLSearchParams();
              if (district) cleanParams.set("district", district);
              if (query) cleanParams.set("search", query);
              if (category && category !== "all" && category !== "more") {
                cleanParams.set("category", category);
              }
              const newUrl = window.location.pathname + (cleanParams.toString() ? "?" + cleanParams.toString() : "");
              window.history.pushState({ district, query, category }, "", newUrl);
            }
          }
        })
        .catch(function (err) {
          isFetching = false;
          if (servicesGrid) servicesGrid.style.opacity = "1";
          console.error("Error fetching services:", err);
        });
    }

    // -------------------------------------------------------------
    // 4. DISTRICT & SEARCH EVENT HANDLERS
    // -------------------------------------------------------------
    if (districtSelect) {
      districtSelect.addEventListener("change", function () {
        fetchFilteredServices({ district: districtSelect.value });
      });
    }

    if (searchInput) {
      searchInput.addEventListener("input", function () {
        const val = searchInput.value.trim();
        if (clearSearchBtn) {
          clearSearchBtn.style.display = val.length > 0 ? "inline-flex" : "none";
        }
        // Option 1: Typing strictly does NOT auto-filter the background page.
        // Live suggestions are managed smoothly by search-suggestions.js.
      });

      searchInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          const q = searchInput.value.trim();
          if (typeof window.lamoNavigateToService === "function") {
            window.lamoNavigateToService(q);
            return;
          }
          triggerHeroSearch(q);
        }
      });
    }

    if (heroForm) {
      heroForm.addEventListener("submit", function (e) {
        e.preventDefault();
        const q = searchInput ? searchInput.value.trim() : "";
        if (typeof window.lamoNavigateToService === "function") {
          window.lamoNavigateToService(q);
          return;
        }
        triggerHeroSearch(q);
      });
    }

    if (clearSearchBtn) {
      clearSearchBtn.addEventListener("click", function () {
        if (searchInput) {
          searchInput.value = "";
          searchInput.focus();
        }
        clearSearchBtn.style.display = "none";
        fetchFilteredServices({ query: "", category: currentCategory || "all" });
      });
    }

    if (btnResetSearch) {
      btnResetSearch.addEventListener("click", function () {
        if (searchInput) {
          searchInput.value = "";
          searchInput.focus();
        }
        if (clearSearchBtn) {
          clearSearchBtn.style.display = "none";
        }
        setActiveCategoryPill("all");
        fetchFilteredServices({ query: "", category: "all" });
      });
    }

    if (btnSelectKollam && districtSelect) {
      btnSelectKollam.addEventListener("click", function () {
        districtSelect.value = "Kollam";
        if (searchInput) searchInput.value = "";
        if (clearSearchBtn) clearSearchBtn.style.display = "none";
        setActiveCategoryPill("all");
        fetchFilteredServices({ district: "Kollam", query: "", category: "all" });
      });
    }

    // -------------------------------------------------------------
    // 5. GEOLOCATION TO KERALA DISTRICT
    // -------------------------------------------------------------
    if (btnUseLocation && districtSelect) {
      btnUseLocation.addEventListener("click", function () {
        if (!navigator.geolocation) {
          alert("Geolocation is not supported by your browser. Please select your district from the dropdown.");
          return;
        }

        const originalHtml = btnUseLocation.innerHTML;
        btnUseLocation.disabled = true;
        btnUseLocation.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Detecting...';

        navigator.geolocation.getCurrentPosition(
          function (position) {
            btnUseLocation.disabled = false;
            btnUseLocation.innerHTML = originalHtml;

            const userLat = position.coords.latitude;
            const userLng = position.coords.longitude;
            const detectedDistrict = findClosestDistrict(userLat, userLng);

            let matched = false;
            for (let i = 0; i < districtSelect.options.length; i++) {
              if (districtSelect.options[i].value.toLowerCase() === detectedDistrict.toLowerCase()) {
                districtSelect.selectedIndex = i;
                matched = true;
                break;
              }
            }

            if (matched) {
              if (typeof window.showToast === "function") {
                window.showToast("success", "Location Detected", "Showing verified services in " + detectedDistrict);
              }
              fetchFilteredServices({ district: detectedDistrict });
            }
          },
          function (error) {
            btnUseLocation.disabled = false;
            btnUseLocation.innerHTML = originalHtml;
            let msg = "Could not detect location. Please select your district manually.";
            if (error.code === error.PERMISSION_DENIED) {
              msg = "Location access was denied. Please select your district manually.";
            }
            if (typeof window.showToast === "function") {
              window.showToast("info", "Location", msg);
            } else {
              alert(msg);
            }
          },
          { timeout: 8000, maximumAge: 60000 }
        );
      });
    }

    // -------------------------------------------------------------
    // 6. CATEGORY FILTERING & "MORE SERVICES" EXPANSION
    // -------------------------------------------------------------
    const primaryPills = document.querySelectorAll(".cat-ribbon-item:not(#btnMoreCategories), .cat-item-btn:not(#btnMoreCategories)");
    const extraPills = document.querySelectorAll(".more-cat-pill");
    const hiddenCategoryInput = document.getElementById("hiddenCategoryInput");

    function setActiveCategoryPill(category) {
      currentCategory = category || "all";
      if (hiddenCategoryInput) {
        hiddenCategoryInput.value = currentCategory;
      }

      primaryPills.forEach(function (p) {
        if (p.getAttribute("data-category") === currentCategory) {
          p.classList.add("active");
        } else {
          p.classList.remove("active");
        }
      });

      extraPills.forEach(function (p) {
        if (p.getAttribute("data-category") === currentCategory) {
          p.classList.add("active");
        } else {
          p.classList.remove("active");
        }
      });
    }

    primaryPills.forEach(function (pill) {
      pill.addEventListener("click", function (e) {
        e.preventDefault();
        const cat = this.getAttribute("data-category") || "all";
        setActiveCategoryPill(cat);
        fetchFilteredServices({ category: cat });

        const target = document.getElementById("services");
        if (target) {
          target.scrollIntoView({ behavior: "smooth" });
        }
      });
    });

    extraPills.forEach(function (pill) {
      pill.addEventListener("click", function (e) {
        e.preventDefault();
        const cat = this.getAttribute("data-category") || "all";
        setActiveCategoryPill(cat);
        fetchFilteredServices({ category: cat });

        const target = document.getElementById("services");
        if (target) {
          target.scrollIntoView({ behavior: "smooth" });
        }
      });
    });

    // "More Services" Drawer Toggle
    if (btnMoreCategories && moreCategoriesContainer) {
      btnMoreCategories.addEventListener("click", function (e) {
        e.preventDefault();
        const isOpen = moreCategoriesContainer.style.display !== "none";
        if (isOpen) {
          moreCategoriesContainer.style.display = "none";
          btnMoreCategories.classList.remove("active");
        } else {
          moreCategoriesContainer.style.display = "block";
          btnMoreCategories.classList.add("active");
          moreCategoriesContainer.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      });
    }

    if (btnCloseMoreCategories && moreCategoriesContainer && btnMoreCategories) {
      btnCloseMoreCategories.addEventListener("click", function () {
        moreCategoriesContainer.style.display = "none";
        btnMoreCategories.classList.remove("active");
      });
    }

    // Quick Search Tags in Hero section
    const quickTags = document.querySelectorAll(".hero-quick-tag");
    quickTags.forEach(function (tag) {
      tag.addEventListener("click", function (e) {
        e.preventDefault();
        const searchText = this.textContent.trim();
        if (searchInput) {
          searchInput.value = searchText;
        }
        if (clearSearchBtn) {
          clearSearchBtn.style.display = "inline-flex";
        }
        setActiveCategoryPill("all");
        fetchFilteredServices({ query: searchText, category: "all" });

        const target = document.getElementById("services");
        if (target) {
          target.scrollIntoView({ behavior: "smooth" });
        }
      });
    });

    // Handle Browser Back / Forward buttons (popstate)
    window.addEventListener("popstate", function () {
      const urlParams = new URLSearchParams(window.location.search);
      const currentSelected = districtSelect ? districtSelect.value : "";
      const district = urlParams.get("district") || currentSelected || "Kollam";
      const query = urlParams.get("search") || "";
      const category = urlParams.get("category") || "all";

      if (districtSelect && district) districtSelect.value = district;
      if (searchInput) searchInput.value = query;
      setActiveCategoryPill(category);

      fetchFilteredServices({
        district: district,
        query: query,
        category: category,
        updateUrl: false,
      });
    });

    // -------------------------------------------------------------
    // 7. NAVBAR GLASSMORPHISM & LOGO POLISH
    // -------------------------------------------------------------
    const navLogo = document.querySelector(".lamo-navbar-logo");
    if (navLogo && (!navLogo.src || navLogo.src.includes("logoo.png"))) {
      navLogo.src = "/static/img/lamo_logo_dark.png";
    }

    const navbar = document.querySelector(".navbar");
    if (navbar) {
      function handleNavbarScroll() {
        if (window.scrollY > 30) {
          navbar.classList.add("navbar-scrolled");
          navbar.classList.add("scrolled");
        } else {
          navbar.classList.remove("navbar-scrolled");
          navbar.classList.remove("scrolled");
        }
      }
      window.addEventListener("scroll", handleNavbarScroll, { passive: true });
      handleNavbarScroll();
    }

    // -------------------------------------------------------------
    // 8. SCROLL-TRIGGERED SECTION ANIMATIONS (IntersectionObserver)
    // -------------------------------------------------------------
    const revealElements = document.querySelectorAll(".scroll-reveal, .scroll-reveal-scale, .stagger-children");

    if ("IntersectionObserver" in window) {
      const revealObserver = new IntersectionObserver(
        function (entries, observer) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              entry.target.classList.add("is-visible");
              const innerStaggers = entry.target.querySelectorAll(".stagger-children");
              innerStaggers.forEach(function (stagger) {
                stagger.classList.add("is-visible");
              });
              observer.unobserve(entry.target);
            }
          });
        },
        {
          root: null,
          threshold: 0.1,
          rootMargin: "0px 0px -40px 0px",
        }
      );

      revealElements.forEach(function (el) {
        revealObserver.observe(el);
      });
    } else {
      revealElements.forEach(function (el) {
        el.classList.add("is-visible");
      });
    }

    // Expose for external controls such as the location modal
    window.fetchFilteredServices = fetchFilteredServices;
  });
})();
