/**
 * LAMO Intelligent Search Suggestions & Auto-Complete Engine
 * Handles live suggestions, fuzzy typo tolerance, keyboard navigation,
 * and popular search chips across all search bars in the app.
 */
(function () {
    'use strict';

    const POPULAR_SERVICES_FALLBACK = [
        { name: 'Electrician', category_label: 'Electrical', icon_class: 'bi bi-lightning-charge-fill', query: 'Electrician' },
        { name: 'Plumbing Repair', category_label: 'Plumbing', icon_class: 'bi bi-wrench-adjustable', query: 'Plumbing' },
        { name: 'AC Repair & Service', category_label: 'AC Repair', icon_class: 'bi bi-snow', query: 'AC Repair' },
        { name: 'Home Cleaning', category_label: 'Cleaning', icon_class: 'bi bi-stars', query: 'Cleaning' },
        { name: 'Carpentry & Woodwork', category_label: 'Carpentry', icon_class: 'bi bi-hammer', query: 'Carpentry' },
        { name: 'House Painting', category_label: 'Painting', icon_class: 'bi bi-paint-bucket', query: 'Painting' },
        { name: 'Appliance Repair', category_label: 'Appliance', icon_class: 'bi bi-cpu', query: 'Appliance' },
        { name: 'Gardening & Lawn', category_label: 'Gardening', icon_class: 'bi bi-flower1', query: 'Gardening' }
    ];

    function initSearchSuggestions() {
        const searchInputs = document.querySelectorAll('#liveServiceSearch, #heroServiceSearch, .search-query-wrap input[type="text"]');
        if (!searchInputs.length) return;

        searchInputs.forEach(input => {
            if (input.dataset.suggestionsInitialized) return;
            input.dataset.suggestionsInitialized = 'true';
            setupInputSuggestions(input);
        });
    }

    function setupInputSuggestions(input) {
        const capsuleBar = input.closest('.search-capsule-bar');
        const container = capsuleBar || input.closest('.search-query-wrap') || input.parentElement;
        if (!container) return;

        // Create or find dropdown element
        let dropdown = container.querySelector('.search-suggestions-dropdown');
        if (!dropdown) {
            dropdown = document.createElement('div');
            dropdown.className = 'search-suggestions-dropdown';
            dropdown.setAttribute('role', 'listbox');
            dropdown.setAttribute('aria-label', 'Search Suggestions');
            container.appendChild(dropdown);
        }

        let debounceTimer = null;
        let selectedIndex = -1;
        let currentSuggestions = [];

        function getActiveDistrict() {
            const districtSelect = document.getElementById('heroDistrictSelect') || document.querySelector('select[name="district"]');
            if (districtSelect && districtSelect.value) return districtSelect.value;
            const navDistrict = document.getElementById('navCurrentDistrict');
            if (navDistrict && navDistrict.textContent.trim()) return navDistrict.textContent.trim();
            return 'Kollam';
        }

        function closeDropdown() {
            dropdown.classList.remove('is-open');
            selectedIndex = -1;
        }

        function openDropdown() {
            dropdown.classList.add('is-open');
        }

        function selectAndSubmit(query) {
            const trimmed = (query !== undefined ? query : input.value).trim();
            input.value = trimmed;
            closeDropdown();

            // Toggle clear button
            const clearBtn = document.getElementById('clearSearchBtn') || document.getElementById('clearHeroSearchBtn');
            if (clearBtn) {
                clearBtn.style.display = trimmed ? 'inline-flex' : 'none';
            }

            // 1. If on homepage with triggerHeroSearch
            if (typeof window.triggerHeroSearch === 'function') {
                window.triggerHeroSearch(trimmed);
                return;
            }

            // 2. If on homepage with fetchFilteredServices and scrollToServicesSection
            if (typeof window.fetchFilteredServices === 'function') {
                if (typeof window.scrollToServicesSection === 'function') {
                    window.scrollToServicesSection(trimmed);
                }
                window.fetchFilteredServices({ query: trimmed, scrollToResults: true, showDropdown: false });
                return;
            }

            // 3. Fallback form submission or redirect
            const form = input.closest('form');
            if (form) {
                form.submit();
            } else {
                const district = getActiveDistrict();
                window.location.href = `/?search=${encodeURIComponent(trimmed)}&district=${encodeURIComponent(district)}#services`;
            }
        }

        function renderPopular(popularList) {
            const items = popularList && popularList.length ? popularList : POPULAR_SERVICES_FALLBACK;
            let html = `
                <div class="suggestions-scroll-body">
                    <div class="suggestions-header">
                        <span><i class="bi bi-fire text-danger"></i> Popular Services</span>
                        <span class="suggestions-header-badge">1-Tap Quick Search</span>
                    </div>
                    <div class="popular-chips-wrap">
            `;

            items.forEach(item => {
                const q = item.query || item.name;
                const icon = item.icon_class || item.icon || 'bi bi-tools';
                html += `
                    <button type="button" class="popular-chip-btn" data-search-query="${escapeHtml(q)}">
                        <i class="${escapeHtml(icon)}"></i>
                        <span>${escapeHtml(item.name)}</span>
                    </button>
                `;
            });

            html += `
                    </div>
                    <div class="suggestions-footer">
                        <span class="suggestions-footer-hint"><i class="bi bi-keyboard"></i> Type to filter or press <strong>Enter</strong></span>
                        <button type="button" class="btn-suggestions-browse" id="btnSuggestionsBrowseAll">Browse all in grid &darr;</button>
                    </div>
                </div>
            `;

            dropdown.innerHTML = html;
            attachEvents(dropdown);
            openDropdown();
        }

        function renderSuggestions(data, query) {
            currentSuggestions = data.suggestions || [];
            selectedIndex = -1;

            if (!currentSuggestions.length && (!data.popular || !data.popular.length)) {
                dropdown.innerHTML = `
                    <div class="suggestions-scroll-body">
                        <div class="suggestions-empty-state">
                            <i class="bi bi-search"></i>
                            <div class="empty-title">No exact match for "<strong>${escapeHtml(query)}</strong>"</div>
                            <div class="empty-sub">Try searching for Electrician, Plumbing, AC Repair...</div>
                        </div>
                        <div class="suggestions-footer">
                            <span class="suggestions-footer-hint">Search anyway:</span>
                            <button type="button" class="btn-suggestions-browse" id="btnSuggestionsBrowseAll">Search "${escapeHtml(query)}" &darr;</button>
                        </div>
                    </div>
                `;
                attachEvents(dropdown);
                openDropdown();
                return;
            }

            let html = '<div class="suggestions-scroll-body">';

            // If typo was corrected
            if (data.has_typo && data.corrected_query) {
                html += `
                    <div class="suggestion-typo-banner" data-search-query="${escapeHtml(data.corrected_query)}">
                        <i class="bi bi-magic"></i>
                        <div>
                            Showing matches for <strong class="corrected-query-btn">${escapeHtml(data.corrected_query)}</strong>
                            <div style="font-size: 11px; opacity: 0.85;">Did you mean this? Click to search</div>
                        </div>
                    </div>
                `;
            }

            if (currentSuggestions.length) {
                html += `
                    <div class="suggestions-header">
                        <span><i class="bi bi-lightning-charge text-success"></i> Matching Services</span>
                        <span class="suggestions-header-badge count-badge">${currentSuggestions.length} found</span>
                    </div>
                    <div class="suggestions-list-group">
                `;

                currentSuggestions.forEach((item, index) => {
                    const iconBg = item.badge_bg || '#f1f5f9';
                    const iconColor = item.badge_color || '#087f5b';
                    const iconClass = item.icon_class || 'bi bi-tools';

                    html += `
                        <div class="suggestion-item" data-index="${index}" data-name="${escapeHtml(item.name)}" role="option">
                            <div class="suggestion-item-main">
                                <div class="suggestion-icon-wrap" style="background: ${escapeHtml(iconBg)}; color: ${escapeHtml(iconColor)};">
                                    <i class="${escapeHtml(iconClass)}"></i>
                                </div>
                                <div class="suggestion-info">
                                    <div class="suggestion-title">${highlightMatch(item.name, query)}</div>
                                    <div class="suggestion-meta-row">
                                        <span class="suggestion-category-tag">${escapeHtml(item.category_label || '')}</span>
                                        ${item.pro_count_display ? `<span class="suggestion-workers-pill"><i class="bi bi-patch-check-fill"></i> ${escapeHtml(item.pro_count_display)}</span>` : ''}
                                    </div>
                                </div>
                            </div>
                            <div class="suggestion-action">
                                <span class="suggestion-select-pill">Select &rarr;</span>
                            </div>
                        </div>
                    `;
                });
                html += '</div>';
            }

            if (data.popular && data.popular.length) {
                html += `
                    <div class="suggestions-header" style="margin-top: 10px;">
                        <span><i class="bi bi-fire text-danger"></i> Also Explore</span>
                    </div>
                    <div class="popular-chips-wrap">
                `;
                data.popular.forEach(item => {
                    const q = item.query || item.name;
                    const icon = item.icon_class || item.icon || 'bi bi-tools';
                    html += `
                        <button type="button" class="popular-chip-btn" data-search-query="${escapeHtml(q)}">
                            <i class="${escapeHtml(icon)}"></i>
                            <span>${escapeHtml(item.name)}</span>
                        </button>
                    `;
                });
                html += '</div>';
            }

            html += `
                <div class="suggestions-footer">
                    <span class="suggestions-footer-hint"><i class="bi bi-keyboard"></i> Press <strong>Enter</strong> to explore results</span>
                    <button type="button" class="btn-suggestions-browse" id="btnSuggestionsBrowseAll">View in Grid &darr;</button>
                </div>
            `;

            html += '</div>';
            dropdown.innerHTML = html;
            attachEvents(dropdown);
            openDropdown();
        }

        function fetchSuggestions(query) {
            const trimmed = query.trim();
            if (!trimmed) {
                renderPopular();
                return;
            }

            const district = getActiveDistrict();
            const apiUrl = `/api/search-suggestions/?q=${encodeURIComponent(trimmed)}&district=${encodeURIComponent(district)}`;

            fetch(apiUrl, {
                headers: { 'Accept': 'application/json' }
            })
                .then(res => res.json())
                .then(data => {
                    if (data && data.status === 'success') {
                        renderSuggestions(data, trimmed);
                    } else {
                        renderPopular();
                    }
                })
                .catch(err => {
                    console.warn('LAMO search suggestions fetch failed:', err);
                    renderPopular();
                });
        }

        function attachEvents(container) {
            // Popular Chips
            const chips = container.querySelectorAll('.popular-chip-btn');
            chips.forEach(chip => {
                chip.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const q = this.getAttribute('data-search-query') || this.textContent.trim();
                    selectAndSubmit(q);
                });
            });

            // Suggestion Items
            const items = container.querySelectorAll('.suggestion-item');
            items.forEach(item => {
                item.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const name = this.getAttribute('data-name') || '';
                    selectAndSubmit(name);
                });
            });

            // Typo Banner
            const typoBanners = container.querySelectorAll('.suggestion-typo-banner');
            typoBanners.forEach(banner => {
                banner.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const q = this.getAttribute('data-search-query') || '';
                    selectAndSubmit(q);
                });
            });

            // View in Grid Button
            const browseBtn = container.querySelector('#btnSuggestionsBrowseAll');
            if (browseBtn) {
                browseBtn.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    selectAndSubmit(input.value.trim());
                });
            }
        }

        // Input Event Listeners
        input.addEventListener('focus', function () {
            if (!this.value.trim()) {
                renderPopular();
            } else {
                fetchSuggestions(this.value);
            }
        });

        input.addEventListener('input', function () {
            const val = this.value;
            clearTimeout(debounceTimer);
            if (!val.trim()) {
                renderPopular();
            } else {
                debounceTimer = setTimeout(() => {
                    fetchSuggestions(val);
                }, 180);
            }
        });

        input.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                closeDropdown();
                return;
            }

            const isOpen = dropdown.classList.contains('is-open');
            const items = dropdown.querySelectorAll('.suggestion-item');

            if (e.key === 'ArrowDown') {
                if (!isOpen) {
                    renderPopular();
                    return;
                }
                if (items.length > 0) {
                    e.preventDefault();
                    selectedIndex = (selectedIndex + 1) % items.length;
                    updateSelection(items, selectedIndex);
                }
            } else if (e.key === 'ArrowUp') {
                if (isOpen && items.length > 0) {
                    e.preventDefault();
                    selectedIndex = (selectedIndex - 1 + items.length) % items.length;
                    updateSelection(items, selectedIndex);
                }
            } else if (e.key === 'Enter') {
                e.preventDefault();
                e.stopPropagation();
                clearTimeout(debounceTimer);
                if (isOpen && selectedIndex >= 0 && items.length > 0 && items[selectedIndex]) {
                    const chosen = items[selectedIndex].getAttribute('data-name') || input.value.trim();
                    selectAndSubmit(chosen);
                } else {
                    const q = input.value.trim();
                    selectAndSubmit(q);
                }
            }
        });

        // Close when clicking outside
        document.addEventListener('click', function (e) {
            if (!container.contains(e.target) && !input.contains(e.target)) {
                closeDropdown();
            }
        });
    }

    function updateSelection(items, index) {
        items.forEach((it, i) => {
            if (i === index) {
                it.classList.add('is-selected');
                it.scrollIntoView({ block: 'nearest' });
            } else {
                it.classList.remove('is-selected');
            }
        });
    }

    function highlightMatch(text, query) {
        if (!query) return escapeHtml(text);
        const regex = new RegExp(`(${escapeRegex(query)})`, 'gi');
        return escapeHtml(text).replace(regex, '<span class="suggestion-match-highlight">$1</span>');
    }

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function escapeRegex(str) {
        return str.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&');
    }

    // Auto-initialize on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSearchSuggestions);
    } else {
        initSearchSuggestions();
    }

    window.initLamoSearchSuggestions = initSearchSuggestions;
})();
