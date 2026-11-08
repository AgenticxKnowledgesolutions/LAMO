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

        function navigateToService(query, workersUrl, serviceId) {
            const district = getActiveDistrict();
            closeDropdown();

            const trimmed = (query !== undefined ? query : input.value).trim();
            input.value = trimmed;

            // Toggle clear button
            const clearBtn = document.getElementById('clearSearchBtn') || document.getElementById('clearHeroSearchBtn');
            if (clearBtn) {
                clearBtn.style.display = trimmed ? 'inline-flex' : 'none';
            }

            // 1. Direct workers URL provided (e.g. clicked on suggestion or popular chip)
            if (workersUrl && typeof workersUrl === 'string' && workersUrl.startsWith('/services/')) {
                let targetUrl = workersUrl;
                if (!targetUrl.includes('district=')) {
                    const sep = targetUrl.includes('?') ? '&' : '?';
                    targetUrl = `${targetUrl}${sep}district=${encodeURIComponent(district)}`;
                }
                window.location.href = targetUrl;
                return;
            }

            // 2. Service ID provided
            if (serviceId) {
                window.location.href = `/services/${serviceId}/workers/?district=${encodeURIComponent(district)}`;
                return;
            }

            // 3. Check current suggestions list for top or exact match (Idea 2 Direct Navigation)
            if (trimmed && currentSuggestions && currentSuggestions.length > 0) {
                const norm = trimmed.toLowerCase();
                const matched = currentSuggestions.find(s => s.name && s.name.toLowerCase() === norm)
                    || currentSuggestions.find(s => s.name && (s.name.toLowerCase().includes(norm) || norm.includes(s.name.toLowerCase())))
                    || currentSuggestions[0];

                if (matched) {
                    const target = matched.workers_url || matched.url || (matched.id ? `/services/${matched.id}/workers/?district=${encodeURIComponent(district)}` : null);
                    if (target) {
                        window.location.href = target;
                        return;
                    }
                }
            }

            // 4. Generic query without exact service match: go to dedicated service_page
            if (trimmed) {
                window.location.href = `/service_page/?q=${encodeURIComponent(trimmed)}&district=${encodeURIComponent(district)}`;
                return;
            }

            // 5. Default fallback to service_page
            window.location.href = `/service_page/?district=${encodeURIComponent(district)}`;
        }

        // Expose globally for form submit & home.js integration
        window.lamoNavigateToService = navigateToService;

        function renderPopular(popularList) {
            const items = popularList && popularList.length ? popularList : POPULAR_SERVICES_FALLBACK;
            const district = getActiveDistrict();
            let html = `
                <div class="suggestions-scroll-body">
                    <div class="suggestions-header">
                        <span><i class="bi bi-fire text-danger"></i> Popular Services</span>
                        <span class="suggestions-header-badge">Direct to Workers</span>
                    </div>
                    <div class="popular-chips-wrap">
            `;

            items.forEach(item => {
                const q = item.query || item.name;
                const icon = item.icon_class || item.icon || 'bi bi-tools';
                const workersUrl = item.workers_url || item.url || '';
                const serviceId = item.id || item.service_id || '';
                const proCount = item.pro_count_display || '';
                html += `
                    <button type="button" class="popular-chip-btn" data-search-query="${escapeHtml(q)}" data-workers-url="${escapeHtml(workersUrl)}" data-service-id="${escapeHtml(serviceId)}">
                        <i class="${escapeHtml(icon)}"></i>
                        <span>${escapeHtml(item.name)}</span>
                        ${proCount ? `<span class="chip-pro-badge">${escapeHtml(proCount)}</span>` : ''}
                    </button>
                `;
            });

            html += `
                    </div>
                    <div class="suggestions-footer">
                        <span class="suggestions-footer-hint"><i class="bi bi-info-circle-fill text-success"></i> Click any service to view verified professionals</span>
                        <a href="/service_page/?district=${encodeURIComponent(district)}" class="btn-suggestions-browse">Browse all services &rarr;</a>
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
            const district = getActiveDistrict();

            if (!currentSuggestions.length && (!data.popular || !data.popular.length)) {
                dropdown.innerHTML = `
                    <div class="suggestions-scroll-body">
                        <div class="suggestions-empty-state">
                            <i class="bi bi-search"></i>
                            <div class="empty-title">No exact match for "<strong>${escapeHtml(query)}</strong>"</div>
                            <div class="empty-sub">Try searching for Electrician, Plumbing, AC Repair...</div>
                        </div>
                        <div class="suggestions-footer">
                            <span class="suggestions-footer-hint">Search all:</span>
                            <a href="/service_page/?q=${encodeURIComponent(query)}&district=${encodeURIComponent(district)}" class="btn-suggestions-browse">Search "${escapeHtml(query)}" in All Services &rarr;</a>
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
                            <div style="font-size: 11px; opacity: 0.85;">Did you mean this? Click to view professionals</div>
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
                    const workersUrl = item.workers_url || item.url || (item.id ? `/services/${item.id}/workers/?district=${encodeURIComponent(district)}` : '');
                    const serviceId = item.id || '';

                    html += `
                        <div class="suggestion-item" data-index="${index}" data-name="${escapeHtml(item.name)}" data-service-id="${escapeHtml(serviceId)}" data-workers-url="${escapeHtml(workersUrl)}" role="option">
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
                                <span class="suggestion-select-pill"><i class="bi bi-arrow-up-right-circle-fill"></i> View Workers</span>
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
                    const workersUrl = item.workers_url || item.url || '';
                    const serviceId = item.id || item.service_id || '';
                    const proCount = item.pro_count_display || '';
                    html += `
                        <button type="button" class="popular-chip-btn" data-search-query="${escapeHtml(q)}" data-workers-url="${escapeHtml(workersUrl)}" data-service-id="${escapeHtml(serviceId)}">
                            <i class="${escapeHtml(icon)}"></i>
                            <span>${escapeHtml(item.name)}</span>
                            ${proCount ? `<span class="chip-pro-badge">${escapeHtml(proCount)}</span>` : ''}
                        </button>
                    `;
                });
                html += '</div>';
            }

            html += `
                <div class="suggestions-footer">
                    <span class="suggestions-footer-hint"><i class="bi bi-keyboard"></i> Press <strong>Enter</strong> to view top matching workers</span>
                    <a href="/service_page/?q=${encodeURIComponent(query)}&district=${encodeURIComponent(district)}" class="btn-suggestions-browse">All Results in Grid &rarr;</a>
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
            // Popular Chips (Idea 2 Direct Navigation)
            const chips = container.querySelectorAll('.popular-chip-btn');
            chips.forEach(chip => {
                chip.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const q = this.getAttribute('data-search-query') || this.textContent.trim();
                    const workersUrl = this.getAttribute('data-workers-url');
                    const serviceId = this.getAttribute('data-service-id');
                    navigateToService(q, workersUrl, serviceId);
                });
            });

            // Suggestion Items (Idea 2 Direct Navigation to Service Workers)
            const items = container.querySelectorAll('.suggestion-item');
            items.forEach(item => {
                item.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const name = this.getAttribute('data-name') || '';
                    const workersUrl = this.getAttribute('data-workers-url');
                    const serviceId = this.getAttribute('data-service-id');
                    navigateToService(name, workersUrl, serviceId);
                });
            });

            // Typo Banner
            const typoBanners = container.querySelectorAll('.suggestion-typo-banner');
            typoBanners.forEach(banner => {
                banner.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const q = this.getAttribute('data-search-query') || '';
                    input.value = q;
                    fetchSuggestions(q);
                });
            });

            // View in Grid Button
            const browseBtn = container.querySelector('#btnSuggestionsBrowseAll');
            if (browseBtn) {
                browseBtn.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    const district = getActiveDistrict();
                    const q = input.value.trim();
                    if (q) {
                        window.location.href = `/service_page/?q=${encodeURIComponent(q)}&district=${encodeURIComponent(district)}`;
                    } else {
                        window.location.href = `/service_page/?district=${encodeURIComponent(district)}`;
                    }
                });
            }
        }

        // Intercept form submit to ensure Idea 2 Direct Navigation
        const parentForm = input.closest('form');
        if (parentForm) {
            parentForm.addEventListener('submit', function (e) {
                e.preventDefault();
                e.stopPropagation();
                clearTimeout(debounceTimer);
                const q = input.value.trim();
                navigateToService(q);
            });
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
                    const chosen = items[selectedIndex];
                    const chosenName = chosen.getAttribute('data-name') || input.value.trim();
                    const chosenUrl = chosen.getAttribute('data-workers-url');
                    const chosenId = chosen.getAttribute('data-service-id');
                    navigateToService(chosenName, chosenUrl, chosenId);
                } else if (currentSuggestions && currentSuggestions.length > 0) {
                    const topMatch = currentSuggestions[0];
                    navigateToService(topMatch.name, topMatch.workers_url || topMatch.url, topMatch.id);
                } else {
                    const q = input.value.trim();
                    navigateToService(q);
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
