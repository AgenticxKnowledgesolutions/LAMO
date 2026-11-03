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

    // Hero Section
    'hero_title_1': { en: 'Trusted Home Services,', ml: 'വിശ്വസനീയമായ വീട്ടുസേവനങ്ങൾ,' },
    'hero_welcome': { en: 'Welcome back,', ml: 'സ്വാഗതം,' },
    'hero_doorstep': { en: 'Right at Your Doorstep', ml: 'നിങ്ങളുടെ വീട്ടുപടിക്കൽ' },
    'hero_subtext_prefix': { en: 'Find verified professionals for all your home needs in', ml: 'വീട്ടുസേവനങ്ങൾക്കായി പരിശോധിച്ചുറപ്പിച്ച വിദഗ്ധരെ കണ്ടെത്തൂ -' },
    'hero_search_placeholder': { en: 'What service do you need? (e.g. Electrician)', ml: 'ഏത് സർവീസാണ് ആവശ്യം? (ഉദാ: ഇലക്ട്രീഷ്യൻ)' },
    'service_search_placeholder': { en: 'Search (e.g. Electrician, Plumbing...)', ml: 'തിരയുക (ഉദാ: ഇലക്ട്രീഷ്യൻ, പ്ലംബിംഗ്...)' },
    'btn_find_services': { en: 'Find Services', ml: 'സർവീസ് കണ്ടെത്തുക' },
    'all_kerala': { en: 'All Kerala', ml: 'കേരളം മുഴുവൻ' },

    // Trust Badges
    'badge_verified': { en: 'Verified Professionals', ml: 'പരിശോധിച്ചുറപ്പിച്ച വിദഗ്ധർ' },
    'badge_easy_booking': { en: 'Easy Booking', ml: 'എളുപ്പത്തിൽ ബുക്കിംഗ്' },
    'badge_transparent_pricing': { en: 'Transparent Pricing', ml: 'സുതാര്യമായ നിരക്കുകൾ' },
    'badge_safe_secure': { en: 'Safe & Secure', ml: 'സുരക്ഷിതവും വിശ്വസനീയവും' },
    'badge_support': { en: 'Support When You Need It', ml: 'ആവശ്യസമയത്തെ സഹായം' },
    'badge_local_experts': { en: 'Local Experts', ml: 'പ്രാദേശിക വിദഗ്ധർ' },
    'badge_pin_secured': { en: 'PIN-Secured Doorstep Assistance', ml: 'പിൻ സുരക്ഷിതമായ വീട്ടുസേവനം' },
    'badge_skilled_people': { en: 'Skilled People,', ml: 'വിദഗ്ധ തൊഴിലാളികൾ,' },
    'badge_happier_homes': { en: 'Happier Homes', ml: 'സന്തോഷമുള്ള ഭവനങ്ങൾ' },
    'badge_better_tomorrow': { en: 'for a Better Tomorrow', ml: 'നല്ലൊരു നാളേക്കായി' },

    // Services Catalog & Categories
    'cat_all': { en: 'All', ml: 'എല്ലാം' },
    'cat_electrical': { en: 'Electrical', ml: 'ഇലക്ട്രിക്കൽ' },
    'cat_plumbing': { en: 'Plumbing', ml: 'പ്ലംബിംഗ്' },
    'cat_carpentry': { en: 'Carpentry', ml: 'ആശാരിപ്പണി' },
    'cat_cleaning': { en: 'Cleaning', ml: 'ക്ലീനിംഗ്' },
    'cat_painting': { en: 'Painting', ml: 'പെയിന്റിംഗ്' },
    'cat_appliance': { en: 'Appliance', ml: 'ഉപകരണങ്ങൾ' },
    'cat_appliance_repair': { en: 'Appliance Repair', ml: 'ഉപകരണ റിപ്പയർ' },
    'cat_gardening': { en: 'Gardening', ml: 'തോട്ടപ്പണി' },
    'cat_masonry': { en: 'Masonry', ml: 'മേസ്തിരിപ്പണി' },
    'cat_pest_control': { en: 'Pest Control', ml: 'കീടനിയന്ത്രണം' },
    'cat_automotive': { en: 'Automotive', ml: 'വാഹന റിപ്പയർ' },
    'cat_view_all': { en: 'View All Services', ml: 'എല്ലാ സർവീസുകളും' },
    'filter_categories': { en: 'Filter & Categories', ml: 'ഫിൽട്ടറും വിഭാഗങ്ങളും' },
    'popular_services': { en: 'Popular Services', ml: 'പ്രധാന സർവീസുകൾ' },
    'how_it_works': { en: 'How It Works', ml: 'എങ്ങനെ പ്രവർത്തിക്കുന്നു' },
    'why_choose_lamo': { en: 'Why Choose LAMO', ml: 'എന്തുകൊണ്ട് ലമോ?' },

    // 14 Kerala Districts
    'Thiruvananthapuram': { en: 'Thiruvananthapuram', ml: 'തിരുവനന്തപുരം' },
    'Kollam': { en: 'Kollam', ml: 'കൊല്ലം' },
    'Pathanamthitta': { en: 'Pathanamthitta', ml: 'പത്തനംതിട്ട' },
    'Alappuzha': { en: 'Alappuzha', ml: 'ആലപ്പുഴ' },
    'Kottayam': { en: 'Kottayam', ml: 'കോട്ടയം' },
    'Idukki': { en: 'Idukki', ml: 'ഇടുക്കി' },
    'Ernakulam': { en: 'Ernakulam', ml: 'എറണാകുളം' },
    'Thrissur': { en: 'Thrissur', ml: 'തൃശൂർ' },
    'Palakkad': { en: 'Palakkad', ml: 'പാലക്കാട്' },
    'Malappuram': { en: 'Malappuram', ml: 'മലപ്പുറം' },
    'Kozhikode': { en: 'Kozhikode', ml: 'കോഴിക്കോട്' },
    'Wayanad': { en: 'Wayanad', ml: 'വയനാട്' },
    'Kannur': { en: 'Kannur', ml: 'കണ്ണൂർ' },
    'Kasargod': { en: 'Kasargod', ml: 'കാസർഗോഡ്' },

    // Location Popup
    'loc_modal_title': { en: 'Choose Your Location', ml: 'സ്ഥലം തിരഞ്ഞെടുക്കുക' },
    'loc_modal_subtitle': { en: 'Find trusted home services available in your area.', ml: 'നിങ്ങളുടെ പ്രദേശത്തെ വിശ്വസനീയമായ സേവനങ്ങൾ കണ്ടെത്തൂ.' },
    'loc_gps_main': { en: 'Use My Current Location', ml: 'എന്റെ നിലവിലെ സ്ഥലം ഉപയോഗിക്കുക' },
    'loc_gps_hint': { en: 'Auto-detect nearest district & locality via GPS', ml: 'ജിപിഎസ് വഴി ഏറ്റവും അടുത്തുള്ള സ്ഥലം കണ്ടെത്തുക' },
    'loc_search_district': { en: 'Search district (e.g. Kollam, Ernakulam...)', ml: 'ജില്ല തിരയുക (ഉദാ: കൊല്ലം, എറണാകുളം...)' },
    'loc_select_district': { en: 'SELECT YOUR DISTRICT', ml: 'നിങ്ങളുടെ ജില്ല തിരഞ്ഞെടുക്കുക' },
    'loc_change_district': { en: 'Change District', ml: 'ജില്ല മാറ്റുക' },
    'loc_save_location': { en: 'Save Location', ml: 'സ്ഥലം സേവ് ചെയ്യുക' },
    'loc_save': { en: 'Save', ml: 'സേവ് ചെയ്യുക' },

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
    'EXPIRED': { en: 'Expired', ml: 'കാലഹരണപ്പെട്ടു' },
    'Expired': { en: 'Expired', ml: 'കാലഹരണപ്പെട്ടു' },

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
    'Close': { en: 'Close', ml: 'അടയ്ക്കുക' },
    'Doorstep Security PIN': { en: 'Doorstep Security PIN', ml: 'സുരക്ഷാ പിൻ' },
    'Track & PIN': { en: 'Track & PIN', ml: 'ട്രാക്ക് & പിൻ' },
    'Active Ongoing Booking': { en: 'Active Ongoing Booking', ml: 'സജീവ ബുക്കിംഗ്' },

    // Footer
    'footer_quick_links': { en: 'Quick Links', ml: 'പ്രധാന ലിങ്കുകൾ' },
    'footer_for_customers': { en: 'For Customers', ml: 'ഉപഭോക്താക്കൾക്ക്' },
    'footer_for_workers': { en: 'For Workers', ml: 'തൊഴിലാളികൾക്ക്' },
    'footer_contact_info': { en: 'Contact Info', ml: 'ബന്ധപ്പെടാനുള്ള വിവരങ്ങൾ' },
    'footer_privacy': { en: 'Privacy Policy', ml: 'സ്വകാര്യതാ നയം' },
    'footer_terms': { en: 'Terms & Conditions', ml: 'നിബന്ധനകളും വ്യവസ്ഥകളും' },
    'footer_admin': { en: 'Admin Portal', ml: 'അഡ്മിൻ പോർട്ടൽ' },
    'footer_rights': { en: 'All rights reserved.', ml: 'എല്ലാ അവകാശങ്ങളും നിക്ഷിപ്തം.' },
    'Join as a Worker': { en: 'Join as a Worker', ml: 'തൊഴിലാളിയായി ചേരൂ' },
    'Worker Dashboard': { en: 'Worker Dashboard', ml: 'വർക്കർ ഡാഷ്‌ബോർഡ്' },
    'Worker Login': { en: 'Worker Login', ml: 'വർക്കർ ലോഗിൻ' },
    'Worker Benefits': { en: 'Worker Benefits', ml: 'തൊഴിലാളി ആനുകൂല്യങ്ങൾ' },
    'Book a Service': { en: 'Book a Service', ml: 'സർവീസ് ബുക്ക് ചെയ്യുക' },
    'Browse Services': { en: 'Browse Services', ml: 'സർവീസുകൾ കാണുക' },
    'Manage Bookings': { en: 'Manage Bookings', ml: 'ബുക്കിംഗുകൾ കാണുക' },
    'Invoices & Receipts': { en: 'Invoices & Receipts', ml: 'ഇൻവോയ്സും രസീതും' },
    'Back to Home': { en: 'Back to Home', ml: 'ഹോമിലേക്ക് മടങ്ങുക' }
  };

  // Build normalized lookup table for generic phrase matching
  var PHRASE_LOOKUP = {};
  Object.keys(DICTIONARY).forEach(function (k) {
    var item = DICTIONARY[k];
    if (item.en && item.ml) {
      PHRASE_LOOKUP[item.en.trim().toLowerCase()] = item.ml;
    }
  });

  var originalTexts = new WeakMap();
  var originalPlaceholders = new WeakMap();

  function getCookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : null;
  }

  function getActiveLanguage() {
    var stored = null;
    try {
      stored = localStorage.getItem('lamo_lang');
    } catch (e) {}
    if (stored === 'ml' || stored === 'en') return stored;

    var cookie = getCookie('lamo_lang');
    if (cookie === 'ml' || cookie === 'en') return cookie;

    var docLang = document.documentElement.getAttribute('lang');
    if (docLang === 'ml' || docLang === 'en') return docLang;

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

    // 1. Root DOM element & client storage
    document.documentElement.setAttribute('lang', lang);
    try {
      localStorage.setItem('lamo_lang', lang);
    } catch (e) {}

    // 2. Immediate cookie write with path=/ & SameSite=Lax
    document.cookie = 'lamo_lang=' + encodeURIComponent(lang) + ';path=/;max-age=31536000;SameSite=Lax';

    // 3. Update switcher buttons UI
    var switchers = document.querySelectorAll('.lamo-lang-switcher');
    switchers.forEach(function (sw) {
      var btns = sw.querySelectorAll('.lamo-lang-btn');
      btns.forEach(function (btn) {
        var btnLang = btn.getAttribute('data-lang');
        btn.classList.toggle('active', btnLang === lang);
        btn.setAttribute('aria-pressed', btnLang === lang ? 'true' : 'false');
      });
    });

    // 4. Translate elements with explicit [data-i18n]
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

    // 5. Translate elements with [data-i18n-placeholder]
    var placeholderElements = document.querySelectorAll('[data-i18n-placeholder]');
    placeholderElements.forEach(function (el) {
      var key = el.getAttribute('data-i18n-placeholder');
      if (!originalPlaceholders.has(el)) {
        originalPlaceholders.set(el, el.getAttribute('placeholder') || '');
      }
      if (lang === 'en') {
        var origP = originalPlaceholders.get(el);
        if (DICTIONARY[key] && DICTIONARY[key].en) {
          el.setAttribute('placeholder', DICTIONARY[key].en);
        } else if (origP) {
          el.setAttribute('placeholder', origP);
        }
      } else {
        if (DICTIONARY[key] && DICTIONARY[key].ml) {
          el.setAttribute('placeholder', DICTIONARY[key].ml);
        }
      }
    });

    // 6. Intelligent phrase scanning across UI buttons, badges, categories, headers
    var scanSelector = [
      '.btn-call-masking', '.btn-tracking-route', '.btn-lamo-audio-call', '.btn-lamo-track-map',
      '.status-pill', '.booking-status-badge', '.status-badge', '.badge', '.btn-action',
      '.receipt-action-btn', '.btn-find-services span', '.btn-continue-location span',
      '.cat-card-title', '.mobile-cat-chip', '.trust-badge-item span', '.hero-script-tagline span',
      '.floating-card-title', '.floating-card-sub', '.loc-gps-main-text', '.loc-gps-hint-text',
      '.loc-chip-name', '.btn-loc-back-districts', '.footer-heading', '.footer-links a',
      '.services-hero-kicker', '.breadcrumb-current', '.back-link', '.loc-section-label-strip span'
    ].join(', ');

    var scanned = document.querySelectorAll(scanSelector);
    scanned.forEach(function (el) {
      if (el.hasAttribute('data-i18n')) return;
      var text = el.textContent.trim();
      var lower = text.toLowerCase();

      if (lang === 'ml') {
        if (PHRASE_LOOKUP[lower]) {
          if (!originalTexts.has(el)) originalTexts.set(el, text);
          var icon = el.querySelector('i, svg');
          if (icon) {
            el.innerHTML = '';
            el.appendChild(icon);
            el.appendChild(document.createTextNode(' ' + PHRASE_LOOKUP[lower]));
          } else {
            el.textContent = PHRASE_LOOKUP[lower];
          }
        }
      } else {
        if (originalTexts.has(el)) {
          var origText = originalTexts.get(el);
          var iconEn = el.querySelector('i, svg');
          if (iconEn) {
            el.innerHTML = '';
            el.appendChild(iconEn);
            el.appendChild(document.createTextNode(' ' + origText.replace(/^[^\w\s]+/, '').trim()));
          } else {
            el.textContent = origText;
          }
        }
      }
    });

    // 7. Input placeholders scan
    var inputs = document.querySelectorAll('input[placeholder]');
    inputs.forEach(function (inp) {
      if (inp.hasAttribute('data-i18n-placeholder')) return;
      var pText = inp.getAttribute('placeholder').trim();
      var pLower = pText.toLowerCase();

      if (lang === 'ml') {
        if (PHRASE_LOOKUP[pLower]) {
          if (!originalPlaceholders.has(inp)) originalPlaceholders.set(inp, pText);
          inp.setAttribute('placeholder', PHRASE_LOOKUP[pLower]);
        }
      } else {
        if (originalPlaceholders.has(inp)) {
          inp.setAttribute('placeholder', originalPlaceholders.get(inp));
        }
      }
    });

    // 8. Persist to backend session asynchronously
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

    // 9. Broadcast custom event
    try {
      document.dispatchEvent(new CustomEvent('lamo:languageChanged', { detail: { lang: lang } }));
    } catch (e) {}
  }

  function setLanguage(lang) {
    applyLanguage(lang);
  }

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

    // Auto-apply stored or active language
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

  window.LAMO_I18N = {
    setLanguage: setLanguage,
    getLanguage: getActiveLanguage,
    t: translateKey,
    dictionary: DICTIONARY
  };

})(window, document);
