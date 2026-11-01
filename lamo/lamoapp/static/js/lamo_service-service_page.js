/**
 * LAMO Services Page - Interactive Filtering & UX Enhancements
 */
document.addEventListener('DOMContentLoaded', function () {
    const heroSearchForm = document.getElementById('heroLocationForm') || document.getElementById('heroSearchForm');
    const heroDistrictSelect = document.getElementById('heroDistrictSelect');
    const heroSearchInput = document.getElementById('liveServiceSearch') || document.getElementById('heroSearchInput');
    const clearSearchBtn = document.getElementById('clearSearchBtn');
    const serviceSortSelect = document.getElementById('serviceSortSelect');
    const priceSlider = document.getElementById('priceRangeSlider');
    const maxPriceInput = document.getElementById('maxPriceInput');
    const minPriceInput = document.getElementById('minPriceInput');
    const servicesGrid = document.getElementById('servicesReferenceGrid');
    const countDisplay = document.getElementById('servicesCountDisplay');

    // 1. District Selection Auto-Update
    if (heroDistrictSelect) {
        heroDistrictSelect.addEventListener('change', function () {
            if (heroSearchForm) {
                heroSearchForm.submit();
            } else {
                const url = new URL(window.location.href);
                url.searchParams.set('district', this.value);
                window.location.href = url.toString();
            }
        });
    }

    // 2. Clear Search Input Button
    if (clearSearchBtn && heroSearchInput) {
        clearSearchBtn.addEventListener('click', function () {
            heroSearchInput.value = '';
            if (heroSearchForm) {
                heroSearchForm.submit();
            }
        });
    }

    // 3. Sort Dropdown Change
    if (serviceSortSelect) {
        serviceSortSelect.addEventListener('change', function () {
            const url = new URL(window.location.href);
            url.searchParams.set('sort', this.value);
            window.location.href = url.toString();
        });
    }

    // 4. Interactive Price Range Slider
    if (priceSlider && maxPriceInput) {
        priceSlider.addEventListener('input', function () {
            maxPriceInput.value = this.value;
            applyClientFilters();
        });

        priceSlider.addEventListener('change', function () {
            const url = new URL(window.location.href);
            url.searchParams.set('max_price', this.value);
            window.location.href = url.toString();
        });
    }

    if (minPriceInput) {
        minPriceInput.addEventListener('change', function () {
            const url = new URL(window.location.href);
            if (this.value) {
                url.searchParams.set('min_price', this.value);
            } else {
                url.searchParams.delete('min_price');
            }
            window.location.href = url.toString();
        });
    }

    if (maxPriceInput) {
        maxPriceInput.addEventListener('change', function () {
            const url = new URL(window.location.href);
            if (this.value) {
                url.searchParams.set('max_price', this.value);
            } else {
                url.searchParams.delete('max_price');
            }
            window.location.href = url.toString();
        });
    }

    // 5. Client-Side Quick Filter Function (for instantaneous responsiveness)
    function applyClientFilters() {
        if (!servicesGrid) return;
        const maxVal = parseFloat(maxPriceInput ? maxPriceInput.value : 999999) || 999999;
        const minVal = parseFloat(minPriceInput ? minPriceInput.value : 0) || 0;
        const cards = servicesGrid.querySelectorAll('.service-reference-card');
        let visibleCount = 0;

        cards.forEach(card => {
            const price = parseFloat(card.getAttribute('data-price')) || 0;
            const matchesPrice = (price === 0) || (price >= minVal && price <= maxVal);

            if (matchesPrice) {
                card.style.display = 'flex';
                visibleCount++;
            } else {
                card.style.display = 'none';
            }
        });

        if (countDisplay) {
            countDisplay.textContent = visibleCount;
        }
    }

    // 6. Mobile Filter Drawer Open / Close Controls
    const btnToggleMobileFilters = document.getElementById('btnToggleMobileFilters');
    const servicesFilterSidebar = document.getElementById('servicesFilterSidebar');
    const mobileFilterOverlay = document.getElementById('mobileFilterOverlay');
    const btnCloseMobileFilter = document.getElementById('btnCloseMobileFilter');
    const btnApplyMobileFilter = document.getElementById('btnApplyMobileFilter');

    function openMobileFilters() {
        if (servicesFilterSidebar) servicesFilterSidebar.classList.add('mobile-open');
        if (mobileFilterOverlay) mobileFilterOverlay.classList.add('show');
        document.body.style.overflow = 'hidden';
    }

    function closeMobileFilters() {
        if (servicesFilterSidebar) servicesFilterSidebar.classList.remove('mobile-open');
        if (mobileFilterOverlay) mobileFilterOverlay.classList.remove('show');
        document.body.style.overflow = '';
    }

    if (btnToggleMobileFilters) {
        btnToggleMobileFilters.addEventListener('click', function (e) {
            e.preventDefault();
            openMobileFilters();
        });
    }

    if (btnCloseMobileFilter) {
        btnCloseMobileFilter.addEventListener('click', function (e) {
            e.preventDefault();
            closeMobileFilters();
        });
    }

    if (btnApplyMobileFilter) {
        btnApplyMobileFilter.addEventListener('click', function (e) {
            e.preventDefault();
            closeMobileFilters();
        });
    }

    if (mobileFilterOverlay) {
        mobileFilterOverlay.addEventListener('click', closeMobileFilters);
    }

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            closeMobileFilters();
        }
    });
});