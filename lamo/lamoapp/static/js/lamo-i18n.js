/**
 * LAMO Bilingual Language System (English <-> Malayalam)
 * Seamless Kerala local translation engine with instant switching and session persistence.
 */
(function (window, document) {
  'use strict';

  var DICTIONARY = {
    // Navigation & Header
    'nav_home': { en: 'Home', ml: 'ഹോം' },
    'nav_services': { en: 'Services', ml: 'സർവീസുകൾ' },
    'nav_bookings': { en: 'My Bookings', ml: 'എന്റെ ബുക്കിംഗുകൾ' },
    'nav_notifications': { en: 'Notifications', ml: 'അറിയിപ്പുകൾ' },
    'nav_location': { en: 'Location', ml: 'സ്ഥലം' },
    'nav_select_location': { en: 'Select Location', ml: 'സ്ഥലം തിരഞ്ഞെടുക്കുക' },
    'nav_change_district': { en: 'Change District', ml: 'ജില്ല മാറ്റുക' },
    'nav_your_location': { en: 'Your Location', ml: 'നിങ്ങളുടെ സ്ഥലം' },
    'nav_change': { en: 'Change', ml: 'മാറ്റുക' },
    'nav_account': { en: 'Account / Profile', ml: 'അക്കൗണ്ട് / പ്രൊഫൈൽ' },
    'nav_signin': { en: 'Account / Sign In', ml: 'അക്കൗണ്ട് / പ്രവേശിക്കുക' },
    'nav_become_worker': { en: 'Become a Worker', ml: 'തൊഴിലാളിയാകുക' },
    'nav_about': { en: 'About Us', ml: 'ഞങ്ങളെക്കുറിച്ച്' },
    'nav_contact': { en: 'Contact', ml: 'ബന്ധപ്പെടുക' },
    'nav_login': { en: 'Login', ml: 'ലോഗിൻ' },
    'nav_register': { en: 'Register', ml: 'രജിസ്റ്റർ' },
    'nav_logout': { en: 'Logout', ml: 'ലോഗൗട്ട്' },
    'nav_dashboard': { en: 'Dashboard', ml: 'ഡാഷ്‌ബോർഡ്' },
    'nav_back_dashboard': { en: 'Back to dashboard', ml: 'ഡാഷ്‌ബോർഡിലേക്ക്' },

    // Notification Drawer
    'notif_title': { en: 'Notifications', ml: 'അറിയിപ്പുകൾ' },
    'notif_subtitle': { en: 'Stay updated with your jobs and messages', ml: 'ജോലികളും സന്ദേശങ്ങളും തത്സമയം അറിയുക' },
    'notif_all': { en: 'All', ml: 'എല്ലാം' },
    'notif_unread': { en: 'Unread', ml: 'വായിക്കാത്തവ' },
    'notif_messages': { en: 'Messages', ml: 'സന്ദേശങ്ങൾ' },
    'notif_bookings': { en: 'Bookings', ml: 'ബുക്കിംഗുകൾ' },
    'notif_view_all': { en: 'View all notifications', ml: 'എല്ലാ അറിയിപ്പുകളും കാണുക' },
    'notif_empty': { en: 'No notifications', ml: 'പുതിയ അറിയിപ്പുകളില്ല' },
    'notif_caught_up': { en: "You're all caught up.", ml: 'എല്ലാം അപ്‌ഡേറ്റാണ്.' },
    'push_title': { en: 'Live Push Notifications', ml: 'ലൈവ് പുഷ് നോട്ടിഫിക്കേഷൻ' },
    'push_desc': { en: 'Get instant alerts even when browser is closed', ml: 'ബ്രൗസർ തുറക്കാതെയും അറിയിപ്പുകൾ തത്സമയം നേടൂ' },
    'push_enable': { en: 'Enable', ml: 'ഓൺ ചെയ്യുക' },
    'push_active': { en: 'Active', ml: 'സജീവം' },

    // Common Kerala Services
    'Electrician': { en: 'Electrician', ml: 'ഇലക്ട്രീഷ്യൻ' },
    'Plumber': { en: 'Plumber', ml: 'പ്ലംബർ' },
    'Plumbing': { en: 'Plumbing', ml: 'പ്ലംബിംഗ്' },
    'Carpenter': { en: 'Carpenter', ml: 'ആശാരി' },
    'Carpentry': { en: 'Carpentry', ml: 'ആശാരിപ്പണി' },
    'Cleaning': { en: 'Cleaning', ml: 'ക്ലീനിംഗ്' },
    'Home Cleaning': { en: 'Home Cleaning', ml: 'വീട് ക്ലീനിംഗ്' },
    'Painter': { en: 'Painter', ml: 'പെയിന്റർ' },
    'Painting': { en: 'Painting', ml: 'പെയിന്റിംഗ്' },
    'Appliance Repair': { en: 'Appliance Repair', ml: 'ഉപകരണ റിപ്പയർ' },
    'Gardening': { en: 'Gardening', ml: 'തോട്ടപ്പണി' },
    'Masonry': { en: 'Masonry', ml: 'മേസ്തിരിപ്പണി' },
    'Pest Control': { en: 'Pest Control', ml: 'കീടനിയന്ത്രണം' },
    'Mechanic': { en: 'Mechanic', ml: 'മെക്കാനിക്ക്' },
    'Automotive': { en: 'Automotive', ml: 'വാഹന റിപ്പയർ' },

    // Booking Statuses
    'PENDING': { en: 'Pending', ml: 'തീർപ്പുകൽപ്പിക്കാത്തവ' },
    'Pending': { en: 'Pending', ml: 'തീർപ്പുകൽപ്പിക്കാത്തവ' },
    'ACCEPTED': { en: 'Accepted', ml: 'സ്വീകരിച്ചു' },
    'Accepted': { en: 'Accepted', ml: 'സ്വീകരിച്ചു' },
    'IN_PROGRESS': { en: 'In Progress', ml: 'പുരോഗമിക്കുന്നു' },
    'In Progress': { en: 'In Progress', ml: 'പുരോഗമിക്കുന്നു' },
    'COMPLETED': { en: 'Completed', ml: 'പൂർത്തിയായി' },
    'Completed': { en: 'Completed', ml: 'പൂർത്തിയായി' },
    'CANCELLED': { en: 'Cancelled', ml: 'റദ്ദാക്കി' },
    'Cancelled': { en: 'Cancelled', ml: 'റദ്ദാക്കി' },
    'REJECTED': { en: 'Rejected', ml: 'നിരസിച്ചു' },
    'Rejected': { en: 'Rejected', ml: 'നിരസിച്ചു' },

    // Actions & Buttons
    'Call Pro': { en: 'Call Pro', ml: 'വിളിക്കുക' },
    'Call Pro (In-App)': { en: 'Call Pro (In-App)', ml: 'ലമോ വഴി വിളിക്കുക' },
    'Call via LAMO': { en: 'Call via LAMO', ml: 'ലമോ വഴി വിളിക്കുക' },
    'Live Route Map': { en: 'Live Route Map', ml: 'റൂട്ട് മാപ്പ്' },
    'Track Pro': { en: 'Track Pro', ml: 'ട്രാക്ക് ചെയ്യുക' },
    'Book Service': { en: 'Book Service', ml: 'സർവീസ് ബുക്ക് ചെയ്യുക' },
    'Book Now': { en: 'Book Now', ml: 'ഇപ്പോൾ ബുക്ക് ചെയ്യുക' },
    'View Details': { en: 'View Details', ml: 'വിവരങ്ങൾ കാണുക' },
    'Receipt': { en: 'Receipt', ml: 'രസീത്' },
    'Awaiting Worker': { en: 'Awaiting Worker', ml: 'തൊഴിലാളിയെ കാത്തിരിക്കുന്നു' },
    'Verify & Pay': { en: 'Verify & Pay', ml: 'പരിശോധിച്ച് പണമടയ്ക്കുക' },
    'Emergency Pending': { en: 'Emergency Pending', ml: 'അടിയന്തിരം' },
    'Pay After Service': { en: 'Pay After Service', ml: 'ജോലിക്ക് ശേഷം പണമടയ്ക്കുക' },
    'Emergency Booking': { en: 'Emergency Booking', ml: 'അടിയന്തര സർവീസ്' },
    'Emergency': { en: 'Emergency', ml: 'അടിയന്തിരം' },
    'Distance': { en: 'Distance', ml: 'ദൂരം' },
    'Estimated Arrival': { en: 'Estimated Arrival', ml: 'എത്തിച്ചേരുന്ന സമയം' },
    'Open Google Maps': { en: 'Open Google Maps', ml: 'ഗൂഗിൾ മാപ്പ് തുറക്കുക' },
    'Customer Location': { en: 'Customer Location', ml: 'ഉപഭോക്താവിന്റെ സ്ഥലം' },
    'Worker Location': { en: 'Worker Location', ml: 'തൊഴിലാളിയുടെ സ്ഥലം' },
    'Worker on the Way': { en: 'Worker on the Way', ml: 'തൊഴിലാളി യാത്രയിലാണ്' },
    'Live Route & Tracking': { en: 'Live Route & Tracking', ml: 'ലൈവ് റൂട്ടും ട്രാക്കിംഗും' },
    'Close': { en: 'Close', ml: 'അടയ്ക്കുക' }
  };

  // Direct phrase lookup for generic button/badge scan
  var PHRASE_LOOKUP = {};
  Object.keys(DICTIONARY).forEach(function (k) {
    var item = DICTIONARY[k];
    if (item.en && item.ml) {
      PHRASE_LOOKUP[item.en.trim().toLowerCase()] = item.ml;
    }
  });

  var originalTexts = new WeakMap();

  function getActiveLanguage() {
    var docLang = document.documentElement.getAttribute('lang');
    if (docLang === 'ml' || docLang === 'en') return docLang;
    var stored = localStorage.getItem('lamo_lang');
    if (stored === 'ml' || stored === 'en') return stored;
    return 'en';
  }

  function translateKey(key, lang) {
    lang = lang || getActiveLanguage();
    if (DICTIONARY[key]) {
      return DICTIONARY[key][lang] || DICTIONARY[key]['en'] || key;
    }
    var lower = key.trim().toLowerCase();
    if (lang === 'ml' && PHRASE_LOOKUP[lower]) {
      return PHRASE_LOOKUP[lower];
    }
    return key;
  }

  function applyLanguage(lang) {
    if (lang !== 'en' && lang !== 'ml') lang = 'en';
    document.documentElement.setAttribute('lang', lang);
    localStorage.setItem('lamo_lang', lang);

    // Update switcher buttons UI
    var switchers = document.querySelectorAll('.lamo-lang-switcher');
    switchers.forEach(function (sw) {
      var btns = sw.querySelectorAll('.lamo-lang-btn');
      btns.forEach(function (btn) {
        var btnLang = btn.getAttribute('data-lang');
        btn.classList.toggle('active', btnLang === lang);
        btn.setAttribute('aria-pressed', btnLang === lang ? 'true' : 'false');
      });
    });

    // 1. Elements with explicit data-i18n attribute
    var i18nElements = document.querySelectorAll('[data-i18n]');
    i18nElements.forEach(function (el) {
      var key = el.getAttribute('data-i18n');
      if (!originalTexts.has(el)) {
        originalTexts.set(el, el.textContent.trim());
      }
      if (lang === 'en') {
        var orig = originalTexts.get(el);
        if (DICTIONARY[key] && DICTIONARY[key].en) {
          el.textContent = DICTIONARY[key].en;
        } else if (orig) {
          el.textContent = orig;
        }
      } else {
        if (DICTIONARY[key] && DICTIONARY[key].ml) {
          el.textContent = DICTIONARY[key].ml;
        }
      }
    });

    // 2. Elements with data-i18n-placeholder attribute
    var placeholderElements = document.querySelectorAll('[data-i18n-placeholder]');
    placeholderElements.forEach(function (el) {
      var key = el.getAttribute('data-i18n-placeholder');
      if (DICTIONARY[key]) {
        el.setAttribute('placeholder', DICTIONARY[key][lang] || DICTIONARY[key]['en']);
      }
    });

    // 3. Scan common action buttons and status badges for seamless bilingual experience
    var targetSelector = '.btn-call-masking, .btn-tracking-route, .btn-lamo-audio-call, .btn-lamo-track-map, .status-pill, .booking-status-badge, .status-badge, .badge, .btn-action, .receipt-action-btn';
    if (lang === 'ml') {
      var scanTargets = document.querySelectorAll(targetSelector);
      scanTargets.forEach(function (el) {
        if (el.hasAttribute('data-i18n')) return;
        var text = el.textContent.trim();
        var lower = text.toLowerCase();
        if (PHRASE_LOOKUP[lower]) {
          if (!originalTexts.has(el)) originalTexts.set(el, text);
          // Preserve child icons if present
          var icon = el.querySelector('i');
          if (icon) {
            el.innerHTML = '';
            el.appendChild(icon);
            el.appendChild(document.createTextNode(' ' + PHRASE_LOOKUP[lower]));
          } else {
            el.textContent = PHRASE_LOOKUP[lower];
          }
        }
      });
    } else {
      // Revert scanned targets to original English
      var scannedElements = document.querySelectorAll(targetSelector);
      scannedElements.forEach(function (el) {
        if (originalTexts.has(el)) {
          var orig = originalTexts.get(el);
          var icon = el.querySelector('i');
          if (icon) {
            el.innerHTML = '';
            el.appendChild(icon);
            el.appendChild(document.createTextNode(' ' + orig.replace(/^[^\w\s]+/, '').trim()));
          } else {
            el.textContent = orig;
          }
        }
      });
    }

    // Persist to backend session & cookie
    var csrfToken = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
    if (!csrfToken) {
      var tokenInput = document.querySelector('[name=csrfmiddlewaretoken]');
      if (tokenInput) csrfToken = tokenInput.value;
    }

    fetch('/api/language/set/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken
      },
      body: JSON.stringify({ lang: lang })
    }).catch(function (err) {
      console.debug('Language sync to server failed:', err);
    });

    // Broadcast custom event
    try {
      document.dispatchEvent(new CustomEvent('lamo:languageChanged', { detail: { lang: lang } }));
    } catch (e) {}
  }

  function setLanguage(lang) {
    applyLanguage(lang);
  }

  // Bind click handlers to language switchers
  function initLanguageSwitchers() {
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('.lamo-lang-btn');
      if (!btn) return;
      var lang = btn.getAttribute('data-lang');
      if (lang && (lang === 'en' || lang === 'ml')) {
        e.preventDefault();
        setLanguage(lang);
      }
    });

    // Auto-apply current or stored language on load
    var currentLang = getActiveLanguage();
    applyLanguage(currentLang);
  }

  // Cross-tab sync
  window.addEventListener('storage', function (e) {
    if (e.key === 'lamo_lang' && (e.newValue === 'en' || e.newValue === 'ml')) {
      applyLanguage(e.newValue);
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLanguageSwitchers);
  } else {
    initLanguageSwitchers();
  }

  // Public API
  window.LAMO_I18N = {
    setLanguage: setLanguage,
    getLanguage: getActiveLanguage,
    t: translateKey,
    dictionary: DICTIONARY
  };

})(window, document);
